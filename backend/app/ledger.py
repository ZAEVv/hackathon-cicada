"""Simulated permissioned ledger: witness nodes that co-sign the audit chain head.

This is a PROTOTYPE of a distributed-ledger anchor for the pre-clearance audit
chain, not a network and not a production ledger.  Three witness nodes
(``customs-ru``, ``customs-ir``, ``port-operator``) run IN-PROCESS, each with
its own SQLite file next to the main database (``<db>.witness-<id>.sqlite3``)
and its own HMAC-SHA256 key.  In a real deployment every node would be operated
by a different party on its own infrastructure and sign with an asymmetric key
(e.g. Ed25519) so that verifiers hold only public keys; the standard library has
no asymmetric signatures, so this demo uses symmetric HMAC and the verifier
knows every key.

Protocol
--------
After the main database transaction that appended an audit entry has
committed, every witness is asked to append ``(shipment_id, sequence,
entry_hash, previous_hash, at)``.  A witness accepts only an append-only
continuation of ITS OWN last record for that shipment (``sequence == last + 1``
and ``previous_hash`` equal to its stored head, genesis for the first entry);
otherwise it refuses and records nothing.  It returns an HMAC over the record.
An entry is *anchored* when at least ``QUORUM`` (2 of 3) witnesses accepted it.
A failing or missing witness never breaks the local state change: the entry
simply stays "not anchored".

Verification compares the local chain with the witness records: a hash that a
quorum attested must match the local entry at that sequence, and the local
chain must be at least as long as the quorum-attested head.  Records whose
signature or own chain linkage does not verify are ignored.

Keys: deterministic, clearly labelled demo keys by default (NOT secret).  They
can be overridden with ``LEDGER_WITNESS_KEYS="customs-ru=<key>,customs-ir=<key>,
port-operator=<key>"`` (any subset, each key at least 16 characters).  Keys are
never logged or returned.
"""
import contextlib
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from collections import Counter
from urllib.parse import quote

NODES = (("customs-ru", "Customs RU"), ("customs-ir", "Customs IR"), ("port-operator", "Port operator"))
QUORUM = 2
KEYS_ENV = "LEDGER_WITNESS_KEYS"
MIN_KEY_LENGTH = 16
GENESIS_HASH = "0" * 64
DISCLAIMER = ("Simulated permissioned ledger: the three witness nodes run in-process on one host and disk "
              "in this demo; prototype only, not a production distributed ledger or a customs system.")

_log = logging.getLogger("sct.ledger")
_SCHEMA = """CREATE TABLE IF NOT EXISTS attestations (
    shipment_id TEXT NOT NULL, sequence INTEGER NOT NULL, entry_hash TEXT NOT NULL,
    previous_hash TEXT NOT NULL, at TEXT NOT NULL, signature TEXT NOT NULL,
    PRIMARY KEY (shipment_id, sequence))"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _equal(left, right):
    return (isinstance(left, str) and isinstance(right, str) and
            hmac.compare_digest(left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass")))


def witness_path(db_path, node_id):
    """Path of a node's own file, or None for in-memory / URI databases (ledger disabled)."""
    try:
        path = os.fspath(db_path)
    except TypeError:
        return None
    if not isinstance(path, str) or path in ("", ":memory:") or path.lower().startswith("file:"):
        return None
    return f"{path}.witness-{node_id}.sqlite3"


def enabled(db_path):
    return witness_path(db_path, NODES[0][0]) is not None


def _keys():
    keys = {node: f"demo-witness-key/{node}/NOT-A-SECRET".encode("ascii") for node, _ in NODES}
    raw = os.environ.get(KEYS_ENV)
    if raw is not None and raw.strip():
        for part in raw.split(","):
            node, separator, value = part.partition("=")
            if not separator or node.strip() not in keys or len(value) < MIN_KEY_LENGTH:
                raise RuntimeError("LEDGER_WITNESS_KEYS is malformed")
            keys[node.strip()] = value.encode("utf-8", "surrogatepass")
    return keys


def _record(node_id, shipment_id, sequence, entry_hash, previous_hash, at):
    return {"node_id": node_id, "shipment_id": shipment_id, "sequence": sequence,
            "entry_hash": entry_hash, "previous_hash": previous_hash, "at": at}


def _sign(key, record):
    return hmac.new(key, _json(record).encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def _create_private(path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return
    os.close(descriptor)


def _open_writable(path):
    _create_private(path)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute(_SCHEMA)
    except BaseException:
        connection.close()
        raise
    return connection


def init_witnesses(db_path):
    """Create the node files (app start-up). Failures leave the node unavailable."""
    if not enabled(db_path):
        return
    _keys()  # fail closed on a malformed override
    for node, _ in NODES:
        try:
            _open_writable(witness_path(db_path, node)).close()
        except (sqlite3.Error, OSError):
            _log.warning("Witness node %s could not be initialised", node)


def witness_append(db_path, node_id, shipment_id, sequence, entry_hash, previous_hash, at):
    """One node's independent append. Returns its signature, or None when it refuses."""
    key = _keys()[node_id]
    with contextlib.closing(_open_writable(witness_path(db_path, node_id))) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT sequence, entry_hash FROM attestations WHERE shipment_id = ? "
                             "ORDER BY sequence DESC LIMIT 1", (shipment_id,)).fetchone()
            last_sequence, head = (row[0], row[1]) if row else (0, GENESIS_HASH)
            if type(sequence) is not int or sequence != last_sequence + 1 or not _equal(previous_hash, head):
                db.execute("ROLLBACK")
                return None  # not an append-only continuation of this node's own view
            record = _record(node_id, shipment_id, sequence, entry_hash, previous_hash, at)
            signature = _sign(key, record)
            db.execute("INSERT INTO attestations VALUES (?, ?, ?, ?, ?, ?)",
                       (shipment_id, sequence, entry_hash, previous_hash, at, signature))
            db.execute("COMMIT")
            return signature
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                db.execute("ROLLBACK")
            raise


def anchor(db_path, shipment_id, entry):
    """Ask every witness to co-sign a committed audit entry. Never raises on node failure."""
    if not enabled(db_path):
        return {"anchored": False, "accepted": []}
    try:
        keys = _keys()
    except RuntimeError:
        _log.warning("Audit entry not anchored: witness keys misconfigured")
        return {"anchored": False, "accepted": []}
    accepted = []
    for node, _ in NODES:
        args = (shipment_id, entry["sequence"], entry["hash"], entry["previous_hash"], entry["at"])
        try:
            signature = witness_append(db_path, node, *args)
        except (sqlite3.Error, OSError):
            signature = None
        if signature is not None and _equal(signature, _sign(keys[node], _record(node, *args))):
            accepted.append(node)
    anchored = len(accepted) >= QUORUM
    if not anchored:
        _log.warning("Audit entry not anchored: %d of %d witnesses accepted", len(accepted), len(NODES))
    return {"anchored": anchored, "accepted": accepted}


def _read_node(db_path, node_id, key, shipment_id):
    """Signature-checked records of one node, or None when the node is unavailable."""
    path = witness_path(db_path, node_id)
    if not os.path.isfile(path):
        return None
    try:
        uri = "file:" + quote(os.path.abspath(path)) + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
            rows = db.execute("SELECT sequence, entry_hash, previous_hash, at, signature FROM attestations "
                              "WHERE shipment_id = ? ORDER BY sequence", (shipment_id,)).fetchall()
    except sqlite3.Error:
        return None
    hashes, previous, valid = [], GENESIS_HASH, True
    for expected, (sequence, entry_hash, previous_hash, at, signature) in enumerate(rows, 1):
        if (type(sequence) is not int or sequence != expected or
                not all(isinstance(v, str) for v in (entry_hash, previous_hash, at, signature)) or
                not _equal(previous_hash, previous) or
                not _equal(signature, _sign(key, _record(node_id, shipment_id, sequence, entry_hash,
                                                         previous_hash, at)))):
            valid = False
            break
        hashes.append(entry_hash)
        previous = entry_hash
    last = rows[-1] if rows else None
    return {"signature_valid": valid, "hashes": hashes if valid else [],
            "last_sequence": last[0] if last and type(last[0]) is int else 0,
            "head_hash": last[1] if last and isinstance(last[1], str) else None}


def compare(db_path, shipment_id, local_hashes):
    """Compare the local chain (hash per sequence, None if unreadable) with the witnesses."""
    local = list(local_hashes)
    if not enabled(db_path):
        return {"status": "disabled", "reason": "ledger_disabled", "anchored": False, "quorum": QUORUM,
                "nodes_total": len(NODES), "nodes_available": 0, "nodes_in_sync": 0,
                "attested_sequence": 0, "local_sequence": len(local), "first_invalid_sequence": None,
                "nodes": [{"node_id": node, "label": label, "available": False, "last_sequence": None,
                           "head_hash": None, "signature_valid": None, "in_sync": False, "state": "unavailable"}
                          for node, label in NODES]}
    keys = _keys()
    nodes, votes, usable = [], {}, 0
    for node, label in NODES:
        info = _read_node(db_path, node, keys[node], shipment_id)
        if info is None:
            nodes.append({"node_id": node, "label": label, "available": False, "last_sequence": None,
                          "head_hash": None, "signature_valid": None, "in_sync": False, "state": "unavailable"})
            continue
        status = {"node_id": node, "label": label, "available": True, "last_sequence": info["last_sequence"],
                  "head_hash": info["head_hash"], "signature_valid": info["signature_valid"], "in_sync": False}
        if not info["signature_valid"]:
            status["state"] = "invalid_signature"
        else:
            usable += 1
            hashes = info["hashes"]
            for sequence, entry_hash in enumerate(hashes, 1):
                votes.setdefault(sequence, Counter())[entry_hash] += 1
            prefix = len(hashes) <= len(local) and all(_equal(local[i], h) for i, h in enumerate(hashes))
            status["in_sync"] = prefix and len(hashes) == len(local)
            status["state"] = "in_sync" if status["in_sync"] else ("behind" if prefix else "diverged")
        nodes.append(status)

    attested, quorum_hashes = 0, {}
    while attested + 1 in votes:
        entry_hash, count = votes[attested + 1].most_common(1)[0]
        if count < QUORUM:
            break
        attested += 1
        quorum_hashes[attested] = entry_hash

    first_invalid, reason = None, None
    if usable < QUORUM:
        status_name, reason = "no_quorum", "quorum_unavailable"
    else:
        for sequence in range(1, min(attested, len(local)) + 1):
            if not _equal(local[sequence - 1], quorum_hashes[sequence]):
                first_invalid, reason = sequence, "hash_mismatch"
                break
        if first_invalid is None and len(local) < attested:
            first_invalid = len(local) + 1
            reason = "chain_deleted" if not local else "truncated"
        if first_invalid is not None:
            status_name = "conflict"
        elif len(local) > attested:
            status_name, reason = "not_anchored", "unanchored_entries"
        else:
            status_name = "anchored"
    return {"status": status_name, "reason": reason, "anchored": status_name == "anchored", "quorum": QUORUM,
            "nodes_total": len(NODES), "nodes_available": sum(n["available"] for n in nodes),
            "nodes_in_sync": sum(n["in_sync"] for n in nodes), "attested_sequence": attested,
            "local_sequence": len(local), "first_invalid_sequence": first_invalid, "nodes": nodes}
