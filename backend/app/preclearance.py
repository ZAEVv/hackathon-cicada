"""Persistent, simulated pre-clearance workflow (not a customs integration).

Audit chain
-----------
Every state change appends one entry to a per-shipment chain.  Each entry stores
``previous_hash`` (the previous entry's ``hash``, or 64 zeros) and ``hash``, a
64-character lowercase hex digest over the canonical JSON of the entry without
its ``hash`` field.

* Unkeyed mode (default; ``AUDIT_HMAC_KEY`` unset or blank): ``hash`` is plain
  SHA-256, byte-for-byte as in earlier versions, so existing databases keep
  verifying.  This detects accidental corruption and naive edits only: anyone
  who can write the SQLite file can rewrite history and recompute a valid chain.
* Keyed mode (``AUDIT_HMAC_KEY`` set to a non-blank value of at least
  ``MIN_AUDIT_KEY_LENGTH`` characters): ``hash`` is HMAC-SHA256(key, canonical
  JSON), same shape and field name.  Without the key a database writer cannot
  forge, edit, reorder or extend entries without verification failing.
  Turning the key on, rotating it, or turning it off invalidates every chain
  written under the previous setting (``verify_audit`` reports the first entry
  as invalid).  Start with a fresh database when enabling or rotating the key.
  The key is read from the environment at call time and is never logged or
  included in any error message.  A non-blank key shorter than the minimum is
  treated as a misconfiguration: ``init_db`` (and every other call) raises
  ``RuntimeError`` instead of silently running unkeyed or with a weak key,
  because an operator who set a key expects authenticity and must not get a
  false sense of it.

The ``disclaimer`` returned by ``verify_audit`` is deliberately unchanged in
both modes (frozen wire contract).  Even keyed, verification only proves that
entries were written by a holder of the key.

Witness anchoring (simulated distributed ledger, see ``ledger.py``)
-------------------------------------------------------------------
After each state-change transaction commits, the new entry is offered to three
witness nodes, each with its own SQLite file and key, which co-sign it only as
an append-only continuation of their own records (quorum 2 of 3).  A witness
failure never fails the state change; the entry is then reported as "not
anchored".  ``verify_audit`` additionally compares the local chain with what a
quorum of witnesses attested and reports the result under ``ledger``.

Now detected (when a quorum of witnesses holds records for the shipment):

* A history rewritten with recomputed hashes, in unkeyed mode or by a holder
  of ``AUDIT_HMAC_KEY`` (``ledger.reason == "hash_mismatch"``).
* Truncating the tail and rolling the declaration back to a genuine earlier
  snapshot (``"truncated"``), and restoring an older copy of the main
  database alone.
* Deleting the declaration together with its whole chain (``"chain_deleted"``).

Still NOT detected / limitations of this prototype:

* All nodes run in the same process on the same host and disk: anyone who can
  rewrite the main database can usually rewrite or delete the witness files
  too, and the default witness keys are public demo values.  Real
  independence needs nodes operated by different parties.
* Entries appended while fewer than two witnesses accept them are only
  locally protected (``ledger.status == "not_anchored"``); with fewer than
  two readable witnesses the ledger cannot judge (``"no_quorum"``).
* Witnesses attest hashes, not content: in unkeyed mode the local checks plus
  the attested hash are what binds content.
* In-memory / URI databases have no witnesses (``"disabled"``).

Messages of exceptions raised here reach API clients (``KeyError`` -> 404,
``ValueError`` -> 409, ``PermissionError`` -> 403), so they are static strings
that never contain user input, paths, SQL, stored data or the key.  All SQL is
parameterised.
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections import Counter

from . import ledger

AUDIT_KEY_ENV = "AUDIT_HMAC_KEY"
MIN_AUDIT_KEY_LENGTH = 16
GENESIS_HASH = "0" * 64

# Defence-in-depth limits, independent of the HTTP layer.
MAX_DECLARATION_VERSIONS = 500
MAX_AUDIT_ENTRIES = 5000  # per shipment; repeated validation adds entries without a new version
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_REVIEW_COMMENT_LENGTH = 1000
MAX_PAYLOAD_BYTES = 64 * 1024

_DECLARATION_KEYS = frozenset({"id", "shipment_id", "version", "status", "payload", "validation",
                               "content_hash", "updated_at", "demo_only"})
_ENTRY_KEYS = frozenset({"sequence", "declaration_id", "action", "actor", "at", "payload",
                         "previous_hash", "hash"})
_OPTIONAL_DECLARATION_KEYS = frozenset({"clearance"})
_STATUSES = frozenset({"draft", "validation_failed", "ready", "submitted", "auto_accepted", "accepted",
                       "needs_revision"})
_HEX64 = re.compile(r"[0-9a-f]{64}")

_UNREADABLE_DECLARATION = "Stored declaration is unreadable"
_UNREADABLE_AUDIT = "Stored audit trail is unreadable"
_UNREADABLE_SUBMISSION = "Stored submission record is unreadable"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    """Plain SHA-256 content fingerprint (declaration ``content_hash``)."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _audit_key():
    raw = os.environ.get(AUDIT_KEY_ENV)
    if raw is None or not raw.strip():
        return None
    if len(raw) < MIN_AUDIT_KEY_LENGTH:
        raise RuntimeError("AUDIT_HMAC_KEY must be at least 16 characters when set")
    return raw.encode("utf-8", "surrogatepass")


def _entry_digest(entry, key):
    data = _json(entry).encode("utf-8")
    if key is None:
        return hashlib.sha256(data).hexdigest()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _digest_equal(left, right):
    return (isinstance(left, str) and isinstance(right, str) and
            hmac.compare_digest(left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass")))


def _is_int(value):
    return type(value) is int


def _no_duplicate_keys(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate key")
        result[name] = value
    return result


def _loads(text):
    if not isinstance(text, str):
        raise ValueError("not text")
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


def _well_formed_declaration(value):
    return (isinstance(value, dict) and
            _DECLARATION_KEYS <= value.keys() <= _DECLARATION_KEYS | _OPTIONAL_DECLARATION_KEYS and
            isinstance(value["id"], str) and isinstance(value["shipment_id"], str) and
            _is_int(value["version"]) and value["version"] >= 1 and
            isinstance(value["status"], str) and value["status"] in _STATUSES and
            isinstance(value["payload"], dict) and isinstance(value["validation"], dict) and
            isinstance(value["content_hash"], str) and isinstance(value["updated_at"], str))


def _parse_declaration(text, message=_UNREADABLE_DECLARATION):
    try:
        value = _loads(text)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(message) from None
    if not _well_formed_declaration(value):
        raise ValueError(message)
    return value


def _parse_entry(text):
    try:
        entry = _loads(text)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_UNREADABLE_AUDIT) from None
    if not (isinstance(entry, dict) and entry.keys() == _ENTRY_KEYS and
            _is_int(entry["sequence"]) and isinstance(entry["payload"], dict) and
            all(isinstance(entry[name], str) for name in ("declaration_id", "action", "actor", "at")) and
            all(isinstance(entry[name], str) and _HEX64.fullmatch(entry[name])
                for name in ("previous_hash", "hash"))):
        raise ValueError(_UNREADABLE_AUDIT)
    return entry


def _restrict_new_db_file(db_path):
    """Best effort: create a new database file readable by its owner only."""
    try:
        path = os.fspath(db_path)
    except TypeError:
        return
    if not isinstance(path, str) or path in ("", ":memory:") or path.lower().startswith("file:"):
        return
    try:
        # O_EXCL never follows or reuses an existing file or symlink.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return  # already exists or cannot be created here; sqlite reports real errors
    os.close(descriptor)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _connect(db_path):
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema = OFF")
    except BaseException:
        connection.close()
        raise
    return connection


@contextlib.contextmanager
def _session(db_path, begin=None):
    """Commit on success, roll back on any exception, always close."""
    connection = _connect(db_path)
    try:
        if begin:
            connection.execute(begin)
        yield connection
        connection.commit()
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def init_db(db_path: str) -> None:
    _audit_key()  # fail closed on a misconfigured key before touching storage
    _restrict_new_db_file(db_path)
    with _session(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS declarations (
            shipment_id TEXT PRIMARY KEY, declaration_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_entries (
            shipment_id TEXT NOT NULL, sequence INTEGER NOT NULL, entry_json TEXT NOT NULL,
            PRIMARY KEY (shipment_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS submission_keys (
            actor_id TEXT NOT NULL, shipment_id TEXT NOT NULL, key TEXT NOT NULL,
            version INTEGER NOT NULL, content_hash TEXT NOT NULL, response_json TEXT NOT NULL,
            PRIMARY KEY (actor_id, shipment_id, key)
        );
        """)


def _role(actor, role):
    if actor.get("role") != role or not actor.get("id"):
        raise PermissionError(f"{role} role required")


def _get(db, shipment_id):
    row = db.execute("SELECT declaration_json FROM declarations WHERE shipment_id = ?", (shipment_id,)).fetchone()
    return _parse_declaration(row[0]) if row else None


def _required(db, shipment_id):
    declaration = _get(db, shipment_id)
    if declaration is None:
        raise KeyError("Declaration not found")
    return declaration


def _store(db, declaration, action, actor, now, key, extra=None):
    shipment_id = declaration["shipment_id"]
    previous = db.execute("SELECT sequence, entry_json FROM audit_entries WHERE shipment_id = ? ORDER BY sequence DESC LIMIT 1", (shipment_id,)).fetchone()
    if previous is not None:
        if not _is_int(previous["sequence"]):
            raise ValueError(_UNREADABLE_AUDIT)
        if previous["sequence"] >= MAX_AUDIT_ENTRIES:
            raise ValueError("Audit limit reached for this declaration")
    payload = {name: declaration[name] for name in ("version", "status", "content_hash")}
    if extra:
        payload.update(extra)
    entry = {"sequence": previous["sequence"] + 1 if previous else 1,
             "declaration_id": declaration["id"], "action": action, "actor": actor["id"],
             "at": now, "payload": payload,
             "previous_hash": _parse_entry(previous["entry_json"])["hash"] if previous else GENESIS_HASH}
    entry["hash"] = _entry_digest(entry, key)
    db.execute("INSERT INTO declarations VALUES (?, ?) ON CONFLICT(shipment_id) DO UPDATE SET declaration_json = excluded.declaration_json", (shipment_id, _json(declaration)))
    db.execute("INSERT INTO audit_entries VALUES (?, ?, ?)", (shipment_id, entry["sequence"], _json(entry)))
    return entry


def _anchor(db_path, shipment_id, entry):
    """Offer a committed entry to the witnesses; never fails the local state change."""
    if entry is not None:
        ledger.anchor(db_path, shipment_id, entry)


def get_declaration(db_path: str, shipment_id: str) -> dict | None:
    with _session(db_path) as db:
        return _get(db, shipment_id)


def save_declaration(db_path: str, shipment: dict, payload: dict, actor: dict, now: str) -> dict:
    _role(actor, "broker")
    try:
        size = len(_json(payload).encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Declaration payload contains invalid text") from None
    if size > MAX_PAYLOAD_BYTES:
        raise ValueError("Declaration payload is too large")
    key = _audit_key()
    with _session(db_path, "BEGIN IMMEDIATE") as db:
        old = _get(db, shipment["id"])
        if old and old["status"] == "submitted":
            raise ValueError("Submitted declaration must be reviewed before editing")
        if old and old["version"] >= MAX_DECLARATION_VERSIONS:
            raise ValueError("Revision limit reached for this declaration")
        declaration = {"id": old["id"] if old else "declaration-" + shipment["id"],
                       "shipment_id": shipment["id"], "version": old["version"] + 1 if old else 1,
                       "status": "draft", "payload": payload,
                       "validation": {"valid": False, "issues": []}, "content_hash": _hash(payload),
                       "updated_at": now, "demo_only": True}
        entry = _store(db, declaration, "save", actor, now, key)
    _anchor(db_path, declaration["shipment_id"], entry)
    return declaration


def _validate(payload, shipment):
    issues = []
    def issue(field, code, message):
        issues.append({"field": field, "code": code, "message": message})
    for field in ("shipper", "consignee", "description", "origin_country", "hs_code", "container_id"):
        if not payload[field].strip():
            issue(field, "required", "Field must not be blank")
    if payload["origin_country"].strip() and not re.fullmatch(r"[A-Za-z]{2}", payload["origin_country"]):
        issue("origin_country", "invalid_format", "Use a two-letter country code")
    if payload["hs_code"].strip() and not re.fullmatch(r"[0-9]{6,10}", payload["hs_code"]):
        issue("hs_code", "invalid_format", "Use 6–10 digits")
    documents = payload["documents"]
    counts = Counter(doc["kind"] for doc in documents)
    for kind in ("invoice", "packing_list", "transport"):
        if counts[kind] == 0:
            issue("documents", "missing_document", f"Missing {kind}")
        elif counts[kind] > 1:
            issue("documents", "duplicate_document", f"Duplicate {kind}")
    if payload["container_id"] != shipment["container_id"]:
        issue("container_id", "container_mismatch", "Container differs from shipment")
    masses = [("mass_kg", payload["mass_kg"]), ("shipment.mass_kg", shipment["mass_kg"])]
    for i, doc in enumerate(documents):
        prefix = f"documents.{i}"
        for field in ("number", "container_id"):
            if not doc[field].strip():
                issue(f"{prefix}.{field}", "required", "Field must not be blank")
        if doc["container_id"] != shipment["container_id"] or doc["container_id"] != payload["container_id"]:
            issue(f"{prefix}.container_id", "container_mismatch", "Container IDs must match")
        masses.append((f"{prefix}.mass_kg", doc["mass_kg"]))
    if max(mass for _, mass in masses) - min(mass for _, mass in masses) > 1:
        issue("mass_kg", "mass_mismatch", "All masses must be within 1 kg")
    return {"valid": not issues, "issues": issues}


RULESET_ID = "rules-v1"
# Illustrative demo parameters only; not real customs lists.
MANUAL_CONTROL_HS_PREFIXES = ("93", "2844", "3601", "3602", "3603", "3604")
TRUSTED_SHIPPERS = ("ООО «Волга Экспорт»", "ТОО «Степь Экспорт»", "Persian Caspian Export")
ALLOWED_ORIGINS = ("RU", "KZ", "IR", "AZ", "TM", "UZ")
VALUE_PER_KG_BAND_USD = (0.05, 500.0)
MASS_TOLERANCE_KG = 1


def _consistent(payload, shipment):
    return _validate(payload, shipment)["valid"]


def _hs_not_controlled(payload, shipment):
    return not payload["hs_code"].strip().startswith(MANUAL_CONTROL_HS_PREFIXES)


def _value_per_kg(payload, shipment):
    mass = payload["mass_kg"]
    return mass > 0 and VALUE_PER_KG_BAND_USD[0] <= payload["value_usd"] / mass <= VALUE_PER_KG_BAND_USD[1]


def _trusted_shipper(payload, shipment):
    return payload["shipper"].strip() in TRUSTED_SHIPPERS


def _origin_allowed(payload, shipment):
    return payload["origin_country"].strip().upper() in ALLOWED_ORIGINS


# (id, description, parameters, check, failure detail). Details are static: never echo input.
_RULES = (
    ("R1", "Documents are consistent with the declaration (container ids, masses)",
     {"mass_tolerance_kg": MASS_TOLERANCE_KG}, _consistent, "Documents are inconsistent"),
    ("R2", "HS code is not on the manual-control list",
     {"manual_control_hs_prefixes": list(MANUAL_CONTROL_HS_PREFIXES)}, _hs_not_controlled,
     "HS code requires manual control"),
    ("R3", "Declared value per kg is within a plausible band",
     {"usd_per_kg_min": VALUE_PER_KG_BAND_USD[0], "usd_per_kg_max": VALUE_PER_KG_BAND_USD[1]},
     _value_per_kg, "Value per kg is outside the plausible band"),
    ("R4", "Shipper is on the trusted-trader (AEO-like) list",
     {"trusted_shippers": list(TRUSTED_SHIPPERS)}, _trusted_shipper, "Shipper is not a trusted trader"),
    ("R5", "Origin country is on the allowed list",
     {"allowed_origins": list(ALLOWED_ORIGINS)}, _origin_allowed, "Origin country is not on the allowed list"),
)


def ruleset_definition():
    return {"id": RULESET_ID,
            "rules": [{"id": rid, "description": desc, "parameters": params}
                      for rid, desc, params, _, _ in _RULES]}


def ruleset_hash():
    return _hash(ruleset_definition())


def ruleset():
    return {**ruleset_definition(), "ruleset_hash": ruleset_hash()}


def evaluate_ruleset(payload, shipment=None):
    """Pure, deterministic evaluation; ``shipment`` defaults to the declaration's own facts."""
    if shipment is None:
        shipment = {"container_id": payload["container_id"], "mass_kg": payload["mass_kg"]}
    results = []
    for rid, _, _, check, failure in _RULES:
        passed = bool(check(payload, shipment))
        results.append({"rule_id": rid, "passed": passed, "detail": "OK" if passed else failure})
    return {"ruleset_id": RULESET_ID, "ruleset_hash": ruleset_hash(),
            "decision": "auto_accepted" if all(r["passed"] for r in results) else "manual_review",
            "results": results}


def validate_declaration(db_path: str, shipment: dict, actor: dict, now: str) -> dict:
    _role(actor, "broker")
    key = _audit_key()
    with _session(db_path, "BEGIN IMMEDIATE") as db:
        declaration = _required(db, shipment["id"])
        if declaration["status"] in ("submitted", "auto_accepted", "accepted", "needs_revision"):
            raise ValueError("Save a draft before validating")
        try:
            declaration["validation"] = _validate(declaration["payload"], shipment)
        except (KeyError, TypeError, AttributeError):
            # A stored payload that no longer has the input shape (tampered row).
            raise ValueError(_UNREADABLE_DECLARATION) from None
        declaration["status"] = "ready" if declaration["validation"]["valid"] else "validation_failed"
        declaration["updated_at"] = now
        entry = _store(db, declaration, "validate", actor, now, key, {"validation": declaration["validation"]})
    _anchor(db_path, declaration["shipment_id"], entry)
    return declaration


def _parse_replay(replay, shipment_id):
    response = _parse_declaration(replay["response_json"], _UNREADABLE_SUBMISSION)
    try:
        consistent = (response["shipment_id"] == shipment_id and response["status"] in ("submitted", "auto_accepted") and
                      response["version"] == replay["version"] and
                      _digest_equal(response["content_hash"], replay["content_hash"]) and
                      _digest_equal(_hash(response["payload"]), response["content_hash"]))
    except ValueError:  # e.g. unencodable text in a tampered payload
        consistent = False
    if not consistent:
        raise ValueError(_UNREADABLE_SUBMISSION)
    return response


def submit_declaration(db_path: str, shipment_id: str, idempotency_key: str, actor: dict, now: str,
                       shipment: dict | None = None) -> dict:
    _role(actor, "broker")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("Idempotency key is required")
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError("Idempotency key is too long")
    key = _audit_key()
    with _session(db_path, "BEGIN IMMEDIATE") as db:
        declaration = _required(db, shipment_id)
        # The replay lookup deliberately precedes the status check: a retried
        # request must get its original response even after a later review.
        replay = db.execute("SELECT version, content_hash, response_json FROM submission_keys WHERE actor_id = ? AND shipment_id = ? AND key = ?", (actor["id"], shipment_id, idempotency_key)).fetchone()
        if replay:
            if replay["version"] != declaration["version"] or not _digest_equal(replay["content_hash"], declaration["content_hash"]):
                raise ValueError("Idempotency key conflicts with revised declaration")
            return _parse_replay(replay, shipment_id)
        if declaration["status"] != "ready":
            raise ValueError("Declaration must be ready")
        declaration["status"] = "submitted"
        declaration["updated_at"] = now
        # Deterministic "smart contract" decision, recorded inside the chained submit entry.
        try:
            clearance = evaluate_ruleset(declaration["payload"], shipment)
        except (KeyError, TypeError, AttributeError):
            raise ValueError(_UNREADABLE_DECLARATION) from None
        declaration["clearance"] = clearance
        if clearance["decision"] == "auto_accepted":
            declaration["status"] = "auto_accepted"
        entry = _store(db, declaration, "submit", actor, now, key, {"clearance": clearance})
        db.execute("INSERT INTO submission_keys VALUES (?, ?, ?, ?, ?, ?)", (actor["id"], shipment_id, idempotency_key, declaration["version"], declaration["content_hash"], _json(declaration)))
    _anchor(db_path, shipment_id, entry)
    return declaration


def review_declaration(db_path: str, shipment_id: str, decision: str, comment: str, actor: dict, now: str) -> dict:
    _role(actor, "reviewer")
    if decision not in ("accepted", "needs_revision"):
        raise ValueError("Invalid review decision")
    if not isinstance(comment, str):
        raise ValueError("Invalid review comment")
    if len(comment) > MAX_REVIEW_COMMENT_LENGTH:
        raise ValueError("Review comment is too long")
    try:
        comment.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Invalid review comment") from None
    key = _audit_key()
    with _session(db_path, "BEGIN IMMEDIATE") as db:
        declaration = _required(db, shipment_id)
        if declaration["status"] != "submitted":
            raise ValueError("Only submitted declarations can be reviewed")
        declaration["status"] = decision
        declaration["updated_at"] = now
        entry = _store(db, declaration, "review", actor, now, key, {"decision": decision, "comment": comment})
    _anchor(db_path, shipment_id, entry)
    return declaration


def get_audit(db_path: str, shipment_id: str) -> list[dict]:
    with _session(db_path) as db:
        rows = db.execute("SELECT entry_json FROM audit_entries WHERE shipment_id = ? ORDER BY sequence", (shipment_id,)).fetchall()
    return [_parse_entry(row[0]) for row in rows]


def _declaration_matches(declaration, latest, expected_validation, shipment_id, expected_clearance=None):
    try:
        return (declaration["id"] == "declaration-" + shipment_id and
                declaration["shipment_id"] == shipment_id and declaration["demo_only"] is True and
                _digest_equal(_hash(declaration["payload"]), declaration["content_hash"]) and
                all(_json(latest["payload"].get(name)) == _json(declaration[name])
                    for name in ("content_hash", "version", "status")) and
                latest["at"] == declaration["updated_at"] and
                _json(declaration["validation"]) == _json(expected_validation) and
                _json(declaration.get("clearance")) == _json(expected_clearance))
    except ValueError:  # unencodable text in a tampered row
        return False


def verify_audit(db_path: str, shipment_id: str) -> dict:
    key = _audit_key()
    declaration_readable = True
    with _session(db_path, "BEGIN") as db:  # one consistent read snapshot
        try:
            declaration = _get(db, shipment_id)
        except ValueError:
            declaration, declaration_readable = None, False
        rows = db.execute("SELECT sequence, entry_json FROM audit_entries WHERE shipment_id = ? ORDER BY sequence", (shipment_id,)).fetchall()
    expected_id = "declaration-" + shipment_id
    previous = GENESIS_HASH
    checked = 0
    invalid = None
    latest = None
    expected_validation = None
    expected_clearance = None
    for expected, row in enumerate(rows, 1):
        checked += 1
        try:
            entry = _parse_entry(row["entry_json"])
        except ValueError:
            invalid = expected
            break
        canonical = _json(entry) == row["entry_json"]  # rejects re-encoded / padded rows
        digest = entry.pop("hash")
        if (not canonical or not _is_int(row["sequence"]) or row["sequence"] != expected or
                entry["sequence"] != expected or not _digest_equal(entry["previous_hash"], previous) or
                not _digest_equal(_entry_digest(entry, key), digest) or
                (declaration is None and declaration_readable) or entry["declaration_id"] != expected_id):
            invalid = expected
            break
        if entry["action"] == "save":
            expected_validation = {"valid": False, "issues": []}
            expected_clearance = None
        elif entry["action"] == "submit":
            expected_clearance = entry["payload"].get("clearance")
        elif entry["action"] == "validate":
            expected_validation = entry["payload"].get("validation")
        previous = digest
        latest = entry
    if invalid is None and (declaration is not None or not declaration_readable):
        if (not declaration_readable or latest is None or
                not _declaration_matches(declaration, latest, expected_validation, shipment_id,
                                         expected_clearance)):
            invalid = len(rows) if rows else 1
    anchor = ledger.compare(db_path, shipment_id, _local_hashes(rows))
    if invalid is None and anchor["first_invalid_sequence"] is not None:
        invalid = anchor["first_invalid_sequence"]  # local chain contradicts the witness quorum
    return {"valid": invalid is None, "entries_checked": checked,
            "first_invalid_sequence": invalid,
            "disclaimer": "Hash-chain integrity only; not proof of authenticity or customs approval.",
            "ledger": {name: anchor[name] for name in (
                "status", "reason", "anchored", "quorum", "nodes_total", "nodes_available",
                "nodes_in_sync", "attested_sequence", "local_sequence")}}


def _local_hashes(rows):
    hashes = []
    for row in rows:
        try:
            hashes.append(_parse_entry(row["entry_json"])["hash"])
        except ValueError:
            hashes.append(None)
    return hashes


def ledger_status(db_path: str, shipment_id: str) -> dict:
    """Per-witness view of a shipment's chain head (simulated ledger, see ``ledger.py``)."""
    with _session(db_path) as db:
        rows = db.execute("SELECT sequence, entry_json FROM audit_entries WHERE shipment_id = ? ORDER BY sequence", (shipment_id,)).fetchall()
    report = ledger.compare(db_path, shipment_id, _local_hashes(rows))
    report.pop("first_invalid_sequence")
    return {"shipment_id": shipment_id, **report, "disclaimer": ledger.DISCLAIMER}
