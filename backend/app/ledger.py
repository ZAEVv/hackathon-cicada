"""Permissioned witness ledger: three nodes that co-sign the audit chain head.

This is a PROTOTYPE of a distributed-ledger anchor for the pre-clearance audit
chain, not a production ledger.  Three witness nodes (``witness-1``,
``witness-2``, ``witness-3``) each keep their own SQLite database and sign
with their own HMAC-SHA256 key.  Two transports:

* **Network mode** (``LEDGER_WITNESS_URLS`` set; the Docker Compose stack):
  every node is a separate HTTP service (``python -m backend.app.witness_node``,
  see ``witness_node.py``) in its own container with its own volume.  The API
  talks to the nodes in parallel with short timeouts; every request is
  authenticated with a per-node HMAC (derived from the node key, with a
  timestamp and a single-use nonce), and every reply carries a MAC bound to the
  request nonce, so a node reply that was altered or replayed in transit is
  rejected.  Unauthenticated writes are refused by the node.
* **In-process mode** (``LEDGER_WITNESS_URLS`` unset; tests and local dev): the
  same three nodes run inside the API process, each with its own file next to
  the main database (``<db>.witness-<id>.sqlite3``).

In a real deployment every node would be operated by a different party on its
own infrastructure and sign with an asymmetric key (e.g. Ed25519) so that
verifiers hold only public keys; the standard library has no asymmetric
signatures, so this prototype uses symmetric HMAC and the verifier knows every
key.

Protocol
--------
After the main database transaction that appended an audit entry has
committed, every witness is asked to append ``(shipment_id, sequence,
entry_hash, previous_hash, at)``.  A witness accepts only an append-only
continuation of ITS OWN last record for that shipment (``sequence == last + 1``
and ``previous_hash`` equal to its stored head, genesis for the first entry);
otherwise it refuses and records nothing.  It returns an HMAC over the record.
An entry is *anchored* when at least ``QUORUM`` (2 of 3) witnesses accepted it
with a valid signature.  A failing, unreachable or missing witness never breaks
the local state change: the entry simply stays "not anchored".

Verification compares the local chain with the witness records: a hash that a
quorum attested must match the local entry at that sequence, and the local
chain must be at least as long as the quorum-attested head.  Records whose
signature or own chain linkage does not verify are ignored (the node is
reported as ``invalid_signature``).

Configuration
-------------
* ``LEDGER_WITNESS_KEYS="witness-1=<key>,witness-2=<key>,witness-3=<key>"``
  (any subset, each key at least 16 characters) overrides the deterministic,
  clearly labelled demo keys (NOT secret).  Keys are never logged or returned.
* ``LEDGER_WITNESS_URLS`` -- either three URLs in node order
  (``http://witness-1:8100,http://witness-2:8100,http://witness-3:8100``) or
  ``node=url`` pairs naming all three nodes.
* ``LEDGER_WITNESS_TIMEOUT`` -- per-request timeout in seconds (default 1.5).
"""
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import quote, urlsplit

NODES = (("witness-1", "Customs"), ("witness-2", "Port"), ("witness-3", "Carrier"))
QUORUM = 2
KEYS_ENV = "LEDGER_WITNESS_KEYS"
URLS_ENV = "LEDGER_WITNESS_URLS"
TIMEOUT_ENV = "LEDGER_WITNESS_TIMEOUT"
DEFAULT_TIMEOUT = 1.5
MIN_KEY_LENGTH = 16
GENESIS_HASH = "0" * 64
DISCLAIMER = ("Simulated permissioned ledger: the three witness nodes run in-process on one host and disk "
              "in this demo; prototype only, not a production distributed ledger or a customs system.")
NETWORK_DISCLAIMER = ("Permissioned ledger prototype: the three witness nodes run as separate network services, "
                      "each with its own database and key; in this deployment one operator runs all of them and "
                      "keys are symmetric (HMAC). Not a production distributed ledger or a customs system.")

# Request authentication (shared with witness_node.py).
AUTH_CONTEXT = b"sct-witness-request-auth-v1"
HEADER_TIMESTAMP = "X-Witness-Timestamp"
HEADER_NONCE = "X-Witness-Nonce"
HEADER_AUTH = "X-Witness-Auth"
HEADER_REPLY = "X-Witness-Reply-Auth"
MAX_CLOCK_SKEW = 120
MAX_REPLY_BYTES = 4 * 1024 * 1024

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


# --- configuration -------------------------------------------------------------------

def witness_path(db_path, node_id):
    """Path of a node's own file, or None for in-memory / URI databases (ledger disabled)."""
    try:
        path = os.fspath(db_path)
    except TypeError:
        return None
    if not isinstance(path, str) or path in ("", ":memory:") or path.lower().startswith("file:"):
        return None
    return f"{path}.witness-{node_id}.sqlite3"


def _remote_urls():
    """``{node_id: base_url}`` in network mode, None in in-process mode. Fails closed if malformed."""
    raw = os.environ.get(URLS_ENV)
    if raw is None or not raw.strip():
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    node_ids = [node for node, _ in NODES]
    if all("=" in part and "://" not in part.partition("=")[0] for part in parts):
        urls = {}
        for part in parts:
            node, _, url = part.partition("=")
            if node.strip() not in node_ids or node.strip() in urls:
                raise RuntimeError("LEDGER_WITNESS_URLS is malformed")
            urls[node.strip()] = url.strip()
    else:
        urls = dict(zip(node_ids, parts))
        if len(parts) != len(node_ids):
            raise RuntimeError("LEDGER_WITNESS_URLS is malformed")
    if set(urls) != set(node_ids):
        raise RuntimeError("LEDGER_WITNESS_URLS is malformed")
    for node, url in urls.items():
        split = urlsplit(url)
        if split.scheme not in ("http", "https") or not split.hostname or split.query or split.fragment \
                or split.username or split.password:
            raise RuntimeError("LEDGER_WITNESS_URLS is malformed")
        urls[node] = url.rstrip("/")
    return urls


def network_mode():
    return _remote_urls() is not None


def _timeout():
    try:
        value = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT
    return min(max(value, 0.1), 10.0)


def enabled(db_path):
    try:
        if network_mode():
            return True
    except RuntimeError:
        return True  # configured but malformed: report it, never fall back silently
    return witness_path(db_path, NODES[0][0]) is not None


def disclaimer():
    try:
        return NETWORK_DISCLAIMER if network_mode() else DISCLAIMER
    except RuntimeError:
        return NETWORK_DISCLAIMER


def _keys():
    keys = {node: f"dev-witness-key/{node}/NOT-A-SECRET".encode("ascii") for node, _ in NODES}
    raw = os.environ.get(KEYS_ENV)
    if raw is not None and raw.strip():
        for part in raw.split(","):
            node, separator, value = part.partition("=")
            if not separator or node.strip() not in keys or len(value) < MIN_KEY_LENGTH:
                raise RuntimeError("LEDGER_WITNESS_KEYS is malformed")
            keys[node.strip()] = value.encode("utf-8", "surrogatepass")
    return keys


def node_key(node_id):
    return _keys()[node_id]


def auth_key(key):
    """Request-authentication key, separate from the record-signing key."""
    return hmac.new(key, AUTH_CONTEXT, hashlib.sha256).digest()


def request_mac(key, method, path, timestamp, nonce, body):
    message = "\n".join((method.upper(), path, timestamp, nonce, hashlib.sha256(body).hexdigest()))
    return hmac.new(auth_key(key), message.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def reply_mac(key, nonce, status, body):
    message = f"reply\n{nonce}\n{status}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(auth_key(key), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _record(node_id, shipment_id, sequence, entry_hash, previous_hash, at):
    return {"node_id": node_id, "shipment_id": shipment_id, "sequence": sequence,
            "entry_hash": entry_hash, "previous_hash": previous_hash, "at": at}


def _sign(key, record):
    return hmac.new(key, _json(record).encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


# --- node storage (used in-process and by witness_node.py) ---------------------------

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


def init_store(path):
    _open_writable(path).close()


def append_record(path, node_id, key, shipment_id, sequence, entry_hash, previous_hash, at):
    """One node's independent, append-only write. Returns its signature, or None when it refuses."""
    with contextlib.closing(_open_writable(path)) as db:
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


def read_records(path, shipment_id):
    """Raw rows ``(sequence, entry_hash, previous_hash, at, signature)``; None if the store is missing."""
    if not os.path.isfile(path):
        return None
    uri = "file:" + quote(os.path.abspath(path)) + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        return db.execute("SELECT sequence, entry_hash, previous_hash, at, signature FROM attestations "
                          "WHERE shipment_id = ? ORDER BY sequence", (shipment_id,)).fetchall()


def _verify_rows(node_id, key, shipment_id, rows):
    """Signature- and linkage-checked view of one node's records."""
    hashes, previous, valid = [], GENESIS_HASH, True
    for expected, row in enumerate(rows, 1):
        if not isinstance(row, (list, tuple)) or len(row) != 5:
            valid = False
            break
        sequence, entry_hash, previous_hash, at, signature = row
        if (type(sequence) is not int or sequence != expected or
                not all(isinstance(v, str) for v in (entry_hash, previous_hash, at, signature)) or
                not _equal(previous_hash, previous) or
                not _equal(signature, _sign(key, _record(node_id, shipment_id, sequence, entry_hash,
                                                         previous_hash, at)))):
            valid = False
            break
        hashes.append(entry_hash)
        previous = entry_hash
    last = rows[-1] if rows and isinstance(rows[-1], (list, tuple)) and len(rows[-1]) == 5 else None
    return {"signature_valid": valid, "hashes": hashes if valid else [],
            "last_sequence": last[0] if last and type(last[0]) is int else 0,
            "head_hash": last[1] if last and isinstance(last[1], str) else None}


# --- in-process transport ------------------------------------------------------------

def witness_append(db_path, node_id, shipment_id, sequence, entry_hash, previous_hash, at):
    """In-process node append. Returns its signature, or None when it refuses."""
    return append_record(witness_path(db_path, node_id), node_id, _keys()[node_id],
                         shipment_id, sequence, entry_hash, previous_hash, at)


def _read_node(db_path, node_id, key, shipment_id):
    """Signature-checked records of one in-process node, or None when the node is unavailable."""
    try:
        rows = read_records(witness_path(db_path, node_id), shipment_id)
    except sqlite3.Error:
        return None
    if rows is None:
        return None
    return _verify_rows(node_id, key, shipment_id, rows)


# --- network transport ---------------------------------------------------------------

class WitnessUnavailable(Exception):
    """The node did not answer usefully (timeout, connection error, HTTP error)."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class WitnessReplyInvalid(Exception):
    """The node answered, but its reply failed authentication or was malformed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# No proxies (the nodes are on a private network) and no redirects.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
_executor_lock = threading.Lock()
_executor = None


def _pool():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix="witness")
        return _executor


def _call(node_id, base_url, key, method, path, payload=None, timeout=None):
    """Authenticated request to one node. Returns ``(status, json_body)``."""
    body = b"" if payload is None else _json(payload).encode("utf-8")
    timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
    headers = {HEADER_TIMESTAMP: timestamp, HEADER_NONCE: nonce,
               HEADER_AUTH: request_mac(key, method, path, timestamp, nonce, body),
               "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body if payload is not None else None,
                                     headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=timeout or _timeout())
    except urllib.error.HTTPError as error:
        response = error
    except TimeoutError:
        raise WitnessUnavailable("timeout") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", None)
        kind = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else "unreachable"
        raise WitnessUnavailable(kind) from None
    with contextlib.closing(response):
        status = response.status if hasattr(response, "status") else response.code
        try:
            raw = response.read(MAX_REPLY_BYTES + 1)
        except (OSError, ValueError):
            raise WitnessUnavailable("unreachable") from None
        signature = response.headers.get(HEADER_REPLY) if response.headers is not None else None
    if status == 401:
        raise WitnessUnavailable("rejected")  # the node refused our credentials (key mismatch)
    if len(raw) > MAX_REPLY_BYTES or not _equal(signature, reply_mac(key, nonce, status, raw)):
        raise WitnessReplyInvalid()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        raise WitnessReplyInvalid() from None
    if status >= 500:
        raise WitnessUnavailable("unreachable")
    if not isinstance(data, dict) or data.get("node_id") != node_id:
        raise WitnessReplyInvalid()
    return status, data


def _remote_append(node_id, base_url, key, record):
    """Signature returned by a remote node, or None when it refused or failed."""
    status, data = _call(node_id, base_url, key, "POST", "/attest", {
        name: record[name] for name in ("shipment_id", "sequence", "entry_hash", "previous_hash", "at")})
    if status != 200:
        return None
    signature = data.get("signature")
    return signature if isinstance(signature, str) else None


def _remote_records(node_id, base_url, key, shipment_id):
    status, data = _call(node_id, base_url, key, "GET", "/records/" + quote(shipment_id, safe=""))
    records = data.get("records")
    if status != 200 or not isinstance(records, list):
        raise WitnessReplyInvalid()
    rows = []
    for item in records:
        if not isinstance(item, dict):
            raise WitnessReplyInvalid()
        rows.append(tuple(item.get(name) for name in ("sequence", "entry_hash", "previous_hash", "at",
                                                      "signature")))
    return rows


def _fan_out(task, urls):
    """Run ``task(node_id, url)`` for every node in parallel under one overall deadline."""
    pool = _pool()
    futures = {node: pool.submit(task, node, urls[node]) for node, _ in NODES}
    deadline = time.monotonic() + _timeout() * 2 + 0.5
    results = {}
    for node, future in futures.items():
        try:
            results[node] = ("ok", future.result(timeout=max(0.0, deadline - time.monotonic())))
        except FutureTimeout:
            results[node] = ("unavailable", "timeout")
        except WitnessUnavailable as error:
            results[node] = ("unavailable", error.kind)
        except WitnessReplyInvalid:
            results[node] = ("invalid", "bad_reply")
        except Exception:  # noqa: BLE001 - a broken node never breaks the API
            _log.warning("Witness node %s request failed unexpectedly", node)
            results[node] = ("unavailable", "unreachable")
    return results


def check_nodes():
    """Health of every remote node (start-up log only)."""
    urls = _remote_urls()
    if urls is None:
        return {}

    def probe(node, url):
        request = urllib.request.Request(url + "/health", headers={"Accept": "application/json"})
        try:
            with contextlib.closing(_opener.open(request, timeout=_timeout())) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    results = {}
    for node, (kind, value) in _fan_out(probe, urls).items():
        results[node] = kind == "ok" and value is True
    return results


# --- protocol ------------------------------------------------------------------------

def init_witnesses(db_path):
    """Create the node files / check the remote nodes (app start-up). Never blocks on a node."""
    urls = _remote_urls()  # fail closed on a malformed URL list
    _keys()  # fail closed on a malformed key override
    if urls is not None:
        for node, healthy in check_nodes().items():
            if not healthy:
                _log.warning("Witness node %s is not reachable at start-up", node)
        return
    if not enabled(db_path):
        return
    for node, _ in NODES:
        try:
            init_store(witness_path(db_path, node))
        except (sqlite3.Error, OSError):
            _log.warning("Witness node %s could not be initialised", node)


def anchor(db_path, shipment_id, entry):
    """Ask every witness to co-sign a committed audit entry. Never raises on node failure."""
    if not enabled(db_path):
        return {"anchored": False, "accepted": []}
    try:
        keys, urls = _keys(), _remote_urls()
    except RuntimeError:
        _log.warning("Audit entry not anchored: witness configuration malformed")
        return {"anchored": False, "accepted": []}
    args = (shipment_id, entry["sequence"], entry["hash"], entry["previous_hash"], entry["at"])
    signatures = {}
    if urls is None:
        for node, _ in NODES:
            try:
                signatures[node] = witness_append(db_path, node, *args)
            except (sqlite3.Error, OSError):
                signatures[node] = None
    else:
        record = dict(zip(("shipment_id", "sequence", "entry_hash", "previous_hash", "at"), args))
        for node, (kind, value) in _fan_out(
                lambda node, url: _remote_append(node, url, keys[node], record), urls).items():
            signatures[node] = value if kind == "ok" else None
    accepted = [node for node, _ in NODES
                if signatures.get(node) is not None and
                _equal(signatures[node], _sign(keys[node], _record(node, *args)))]
    anchored = len(accepted) >= QUORUM
    if not anchored:
        _log.warning("Audit entry not anchored: %d of %d witnesses accepted", len(accepted), len(NODES))
    return {"anchored": anchored, "accepted": accepted}


def _unavailable_node(node, label, error=None):
    return {"node_id": node, "label": label, "available": False, "last_sequence": None, "head_hash": None,
            "signature_valid": None, "in_sync": False, "state": "unavailable", "error": error}


def _node_views(db_path, shipment_id, keys, urls):
    """``{node_id: (info | None, error)}`` for every node."""
    if urls is None:
        return {node: (_read_node(db_path, node, keys[node], shipment_id), None) for node, _ in NODES}
    views = {}
    results = _fan_out(lambda node, url: _remote_records(node, url, keys[node], shipment_id), urls)
    for node, (kind, value) in results.items():
        if kind == "ok":
            views[node] = (_verify_rows(node, keys[node], shipment_id, value), None)
        elif kind == "invalid":
            views[node] = ({"signature_valid": False, "hashes": [], "last_sequence": 0, "head_hash": None},
                           "bad_reply")
        else:
            views[node] = (None, value)
    return views


def compare(db_path, shipment_id, local_hashes):
    """Compare the local chain (hash per sequence, None if unreadable) with the witnesses."""
    local = list(local_hashes)
    if not enabled(db_path):
        return {"status": "disabled", "reason": "ledger_disabled", "anchored": False, "quorum": QUORUM,
                "nodes_total": len(NODES), "nodes_available": 0, "nodes_in_sync": 0,
                "attested_sequence": 0, "local_sequence": len(local), "first_invalid_sequence": None,
                "mode": "in_process", "nodes": [_unavailable_node(node, label) for node, label in NODES]}
    keys, urls = _keys(), _remote_urls()
    views = _node_views(db_path, shipment_id, keys, urls)
    nodes, votes, usable = [], {}, 0
    for node, label in NODES:
        info, error = views[node]
        if info is None:
            nodes.append(_unavailable_node(node, label, error))
            continue
        status = {"node_id": node, "label": label, "available": True, "last_sequence": info["last_sequence"],
                  "head_hash": info["head_hash"], "signature_valid": info["signature_valid"], "in_sync": False,
                  "error": error}
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
            "local_sequence": len(local), "first_invalid_sequence": first_invalid,
            "mode": "in_process" if urls is None else "network", "nodes": nodes}
