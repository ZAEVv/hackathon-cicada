import contextlib
import copy
import functools
import hashlib
import hmac
import json
import os
import sqlite3

import pytest

from backend.app import preclearance as pc
from backend.app.schemas import AuditEntry, AuditVerification, Declaration


NOW = "2026-11-12T08:00:00Z"
BROKER = {"id": "dev-broker", "role": "broker"}
REVIEWER = {"id": "dev-reviewer", "role": "reviewer"}
SHIPMENT = {"id": "SCT-001", "mass_kg": 20000, "container_id": "C1"}
PAYLOAD = {"shipper": "Exporter", "consignee": "Importer", "description": "Parts",
           "mass_kg": 20000, "value_usd": 100, "origin_country": "RU", "hs_code": "123456",
           "container_id": "C1", "documents": [
               {"kind": kind, "number": kind, "container_id": "C1", "mass_kg": 20000}
               for kind in ("invoice", "packing_list", "transport")]}


KEY1 = "audit-key-one-0123456789abcdef"
KEY2 = "audit-key-two-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_ambient_audit_key(monkeypatch):
    # Tests opt into keyed mode explicitly; an exported key must not leak in.
    monkeypatch.delenv(pc.AUDIT_KEY_ENV, raising=False)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.sqlite")
    pc.init_db(path)
    return path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def set_key(monkeypatch, key):
    if key is None:
        monkeypatch.delenv(pc.AUDIT_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(pc.AUDIT_KEY_ENV, key)


def raw_execute(db, sql, params=()):
    with contextlib.closing(sqlite3.connect(db)) as conn:
        with conn:
            return conn.execute(sql, params).fetchall()


def raw_entries(db, shipment_id=SHIPMENT["id"]):
    return [json.loads(text) for (text,) in raw_execute(
        db, "SELECT entry_json FROM audit_entries WHERE shipment_id = ? ORDER BY sequence", (shipment_id,))]


def write_entry_text(db, sequence, text):
    raw_execute(db, "UPDATE audit_entries SET entry_json = ? WHERE sequence = ?", (text, sequence))


def write_declaration(db, value):
    text = value if isinstance(value, str) else json.dumps(value)
    raw_execute(db, "UPDATE declarations SET declaration_json = ?", (text,))


def read_declaration(db):
    return json.loads(raw_execute(db, "SELECT declaration_json FROM declarations")[0][0])


def sha256_digest(entry):
    return hashlib.sha256(canonical(entry).encode("utf-8")).hexdigest()


def hmac_digest(key):
    def digest(entry):
        return hmac.new(key.encode("utf-8"), canonical(entry).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def lifecycle(db):
    ready(db)
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    pc.review_declaration(db, SHIPMENT["id"], "needs_revision", "Fix", REVIEWER, NOW)


def ready(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    return pc.validate_declaration(db, SHIPMENT, BROKER, NOW)


def test_roles_and_missing(db):
    with pytest.raises(PermissionError):
        pc.save_declaration(db, SHIPMENT, PAYLOAD, REVIEWER, NOW)
    with pytest.raises(PermissionError):
        pc.review_declaration(db, SHIPMENT["id"], "accepted", "", BROKER, NOW)
    with pytest.raises(KeyError):
        pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    assert pc.get_declaration(db, SHIPMENT["id"]) is None


def test_invalid_documents(db):
    payload = copy.deepcopy(PAYLOAD)
    payload.update(shipper=" ", origin_country="Russia", hs_code="abc")
    payload["documents"][0].update(number="", mass_kg=19998, container_id="wrong")
    payload["documents"][1] = copy.deepcopy(payload["documents"][0])
    saved = pc.save_declaration(db, SHIPMENT, payload, BROKER, NOW)
    assert saved["status"] == "draft"
    result = pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    assert result["status"] == "validation_failed"
    assert {i["code"] for i in result["validation"]["issues"]} == {
        "required", "invalid_format", "missing_document", "duplicate_document", "mass_mismatch", "container_mismatch"}
    with pytest.raises(ValueError):
        pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)


def test_replay_review_and_revision(db):
    Declaration(**ready(db))
    submitted = pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    with pytest.raises(ValueError):
        pc.save_declaration(db, SHIPMENT, PAYLOAD, BROKER, NOW)
    accepted = pc.review_declaration(db, SHIPMENT["id"], "accepted", "demo", REVIEWER, NOW)
    assert accepted["status"] == "accepted"
    count = len(pc.get_audit(db, SHIPMENT["id"]))
    assert pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW) == submitted
    assert len(pc.get_audit(db, SHIPMENT["id"])) == count
    revised = pc.save_declaration(db, SHIPMENT, PAYLOAD, BROKER, NOW)
    assert revised["version"] == 2 and revised["status"] == "draft"
    assert revised["validation"] == {"valid": False, "issues": []}
    with pytest.raises(ValueError):
        pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    for entry in pc.get_audit(db, SHIPMENT["id"]):
        AuditEntry(**entry)
    assert AuditVerification(**pc.verify_audit(db, SHIPMENT["id"])).valid


def test_needs_revision_requires_new_version_before_resubmission(db):
    initial = ready(db)
    key = f'{SHIPMENT["id"]}-v{initial["version"]}'
    submitted = pc.submit_declaration(db, SHIPMENT["id"], key, BROKER, NOW)
    reviewed = pc.review_declaration(db, SHIPMENT["id"], "needs_revision", "Fix it", REVIEWER, NOW)
    audit_count = len(pc.get_audit(db, SHIPMENT["id"]))
    assert pc.submit_declaration(db, SHIPMENT["id"], key, BROKER, NOW) == submitted
    with pytest.raises(ValueError, match="Save a draft"):
        pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    assert pc.get_declaration(db, SHIPMENT["id"]) == reviewed
    assert len(pc.get_audit(db, SHIPMENT["id"])) == audit_count

    saved = pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    assert saved["version"] == initial["version"] + 1
    validated = pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    with pytest.raises(ValueError, match="conflicts"):
        pc.submit_declaration(db, SHIPMENT["id"], key, BROKER, NOW)
    new_key = f'{SHIPMENT["id"]}-v{validated["version"]}'
    resubmitted = pc.submit_declaration(db, SHIPMENT["id"], new_key, BROKER, NOW)
    assert resubmitted["status"] == "submitted"
    assert pc.get_declaration(db, SHIPMENT["id"]) == resubmitted
    assert pc.review_declaration(db, SHIPMENT["id"], "accepted", "", REVIEWER, NOW)["status"] == "accepted"
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]


@pytest.mark.parametrize("field,value", [
    ("status", "accepted"), ("version", 99), ("updated_at", "2099-01-01T00:00:00Z"),
])
def test_audit_detects_independent_metadata_tampering(db, field, value):
    ready(db)
    with sqlite3.connect(db) as conn:
        declaration = json.loads(conn.execute("SELECT declaration_json FROM declarations").fetchone()[0])
        declaration[field] = value
        conn.execute("UPDATE declarations SET declaration_json = ?", (json.dumps(declaration),))
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_audit_detects_deleted_trailing_state_event(db):
    ready(db)
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM audit_entries WHERE sequence = (SELECT MAX(sequence) FROM audit_entries)")
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_empty_audit_is_valid(db):
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] and result["entries_checked"] == 0


@pytest.mark.parametrize("target", ["payload", "entry"])
def test_tamper_detection(db, target):
    ready(db)
    with sqlite3.connect(db) as conn:
        if target == "payload":
            row = conn.execute("SELECT declaration_json FROM declarations").fetchone()
            declaration = json.loads(row[0])
            declaration["payload"]["shipper"] = "Tampered"
            conn.execute("UPDATE declarations SET declaration_json = ?", (json.dumps(declaration),))
        else:
            row = conn.execute("SELECT entry_json FROM audit_entries WHERE sequence = 1").fetchone()
            entry = json.loads(row[0])
            entry["actor"] = "Tampered"
            conn.execute("UPDATE audit_entries SET entry_json = ? WHERE sequence = 1", (json.dumps(entry),))
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]


# ---------------------------------------------------------------------------
# Keyed (HMAC) audit chain
# ---------------------------------------------------------------------------


def rewrite_chain_from(db, start, mutate, digest):
    """Attacker with DB write access: edit entry ``start`` and re-link the rest."""
    entries = raw_entries(db)
    previous = entries[start - 2]["hash"] if start > 1 else "0" * 64
    for entry in entries[start - 1:]:
        if entry["sequence"] == start:
            mutate(entry)
        entry.pop("hash")
        entry["previous_hash"] = previous
        entry["hash"] = previous = digest(entry)
        write_entry_text(db, entry["sequence"], canonical(entry))


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_unkeyed_chain_is_byte_compatible_plain_sha256(db, monkeypatch, blank):
    if blank is not None:
        monkeypatch.setenv(pc.AUDIT_KEY_ENV, blank)
    lifecycle(db)
    texts = [text for (text,) in raw_execute(db, "SELECT entry_json FROM audit_entries ORDER BY sequence")]
    previous = "0" * 64
    for text in texts:
        entry = json.loads(text)
        assert text == canonical(entry)
        digest = entry.pop("hash")
        assert digest == sha256_digest(entry)
        assert entry["previous_hash"] == previous
        previous = digest
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_keyed_chain_uses_hmac_and_verifies(db, monkeypatch):
    set_key(monkeypatch, KEY1)
    lifecycle(db)
    entries = pc.get_audit(db, SHIPMENT["id"])
    assert len(entries) == 4
    for entry in entries:
        AuditEntry(**entry)
        body = {k: v for k, v in entry.items() if k != "hash"}
        assert len(entry["hash"]) == 64
        assert entry["hash"] == hmac_digest(KEY1)(body)
        assert entry["hash"] != sha256_digest(body)
    result = pc.verify_audit(db, SHIPMENT["id"])
    AuditVerification(**result)
    assert result.pop("ledger")["status"] == "anchored"  # additive field (witness ledger)
    assert result == {"valid": True, "entries_checked": 4, "first_invalid_sequence": None,
                      "disclaimer": "Hash-chain integrity only; not proof of authenticity or customs approval."}


@pytest.mark.parametrize("write_key,verify_key,valid", [
    (None, None, True), (KEY1, KEY1, True),
    (None, KEY1, False), (KEY1, None, False), (KEY1, KEY2, False), (KEY2, KEY1, False),
])
def test_keyed_unkeyed_matrix(db, monkeypatch, write_key, verify_key, valid):
    set_key(monkeypatch, write_key)
    lifecycle(db)
    set_key(monkeypatch, verify_key)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] is valid
    if not valid:
        assert result["first_invalid_sequence"] == 1 and result["entries_checked"] == 1


@pytest.mark.parametrize("early,late", [(KEY1, None), (None, KEY1), (KEY1, KEY2)])
def test_entry_written_under_other_key_is_first_invalid(db, monkeypatch, early, late):
    set_key(monkeypatch, early)
    ready(db)  # entries 1-2
    set_key(monkeypatch, late)
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)  # entry 3
    set_key(monkeypatch, early)
    assert pc.verify_audit(db, SHIPMENT["id"])["first_invalid_sequence"] == 3
    set_key(monkeypatch, late)
    assert pc.verify_audit(db, SHIPMENT["id"])["first_invalid_sequence"] == 1


def test_rewritten_history_is_detected_by_keys_and_witnesses(tmp_path, monkeypatch):
    def mutate(entry):
        entry["actor"] = "someone-else"

    # Unkeyed: a DB writer can re-link the local chain; only the witness
    # quorum (simulated ledger) catches it.
    db = str(tmp_path / "unkeyed.sqlite")
    pc.init_db(db)
    lifecycle(db)
    rewrite_chain_from(db, 2, mutate, sha256_digest)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 2
    assert result["ledger"]["reason"] == "hash_mismatch"

    # Keyed: without the key the same rewrite fails at the edited entry,
    # whether recomputed as plain SHA-256 or with a guessed key.
    db = str(tmp_path / "keyed.sqlite")
    pc.init_db(db)
    set_key(monkeypatch, KEY1)
    lifecycle(db)
    for forged_digest in (sha256_digest, hmac_digest(KEY2)):
        rewrite_chain_from(db, 2, mutate, forged_digest)
        result = pc.verify_audit(db, SHIPMENT["id"])
        assert not result["valid"] and result["first_invalid_sequence"] == 2
    # Even the key holder cannot rewrite history the witnesses attested.
    rewrite_chain_from(db, 2, mutate, hmac_digest(KEY1))
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 2
    assert result["ledger"]["reason"] == "hash_mismatch"


@pytest.mark.parametrize("short", ["x", "short-key", "a" * 15])
def test_short_key_fails_closed_without_revealing_key(tmp_path, monkeypatch, short):
    monkeypatch.setenv(pc.AUDIT_KEY_ENV, short)
    path = tmp_path / "short.sqlite"
    with pytest.raises(RuntimeError) as excinfo:
        pc.init_db(str(path))
    assert short not in str(excinfo.value)
    assert not path.exists()
    with pytest.raises(RuntimeError):
        pc.verify_audit(str(path), SHIPMENT["id"])
    monkeypatch.setenv(pc.AUDIT_KEY_ENV, "a" * 16)
    pc.init_db(str(path))


def test_digest_comparisons_use_compare_digest(db, monkeypatch):
    lifecycle(db)
    calls = []
    real = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(pc.hmac, "compare_digest", spy)
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]
    assert len(calls) >= 2 * 4  # previous_hash and hash for every entry


# ---------------------------------------------------------------------------
# verify_audit bypasses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, KEY1])
def test_chain_moved_to_another_shipment_is_detected(db, monkeypatch, key):
    set_key(monkeypatch, key)
    ready(db)
    raw_execute(db, "UPDATE declarations SET shipment_id = 'SCT-002'")
    raw_execute(db, "UPDATE audit_entries SET shipment_id = 'SCT-002'")
    result = pc.verify_audit(db, "SCT-002")
    assert not result["valid"] and result["first_invalid_sequence"] == 1


@pytest.mark.parametrize("field,value", [
    ("validation", {"valid": True, "issues": []}), ("demo_only", False),
    ("shipment_id", "SCT-002"), ("id", "declaration-SCT-002"), ("version", True),
])
def test_declaration_fields_outside_old_checks_are_bound(db, field, value):
    payload = copy.deepcopy(PAYLOAD)
    payload["hs_code"] = "abc"
    pc.save_declaration(db, SHIPMENT, payload, BROKER, NOW)
    assert pc.validate_declaration(db, SHIPMENT, BROKER, NOW)["status"] == "validation_failed"
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]
    declaration = read_declaration(db)
    declaration[field] = value
    write_declaration(db, declaration)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 2


def test_validation_expectation_tracks_save_and_validate(db):
    lifecycle(db)
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)  # resets validation
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]
    declaration = read_declaration(db)
    declaration["validation"] = {"valid": True, "issues": []}
    write_declaration(db, declaration)
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]


@pytest.mark.parametrize("text", [
    "{not json", "[]", "null", "42",
    '{"duplicate": 1, "duplicate": 2}',
])
@pytest.mark.parametrize("with_chain", [True, False])
def test_corrupt_declaration_row_is_reported_not_raised(db, text, with_chain):
    ready(db)
    if not with_chain:
        raw_execute(db, "DELETE FROM audit_entries")
    write_declaration(db, text)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"]
    assert result["first_invalid_sequence"] == (2 if with_chain else 1)
    for call in (lambda: pc.get_declaration(db, SHIPMENT["id"]),
                 lambda: pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW),
                 lambda: pc.validate_declaration(db, SHIPMENT, BROKER, NOW),
                 lambda: pc.submit_declaration(db, SHIPMENT["id"], "k", BROKER, NOW)):
        with pytest.raises(ValueError) as excinfo:
            call()
        assert str(excinfo.value) == "Stored declaration is unreadable"


def test_declaration_with_extra_or_duplicate_keys_is_rejected(db):
    ready(db)
    declaration = read_declaration(db)
    write_declaration(db, dict(declaration, approved_by="someone"))
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]
    with pytest.raises(ValueError, match="^Stored declaration is unreadable$"):
        pc.get_declaration(db, SHIPMENT["id"])
    # Duplicate key whose last value equals the genuine one: old parser accepted it.
    text = canonical(declaration)
    write_declaration(db, '{"status":"accepted",' + text[1:])
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]


def _single_entry_variant(entry, variant):
    entry = dict(entry)
    digest = entry.pop("hash")
    if variant == "sequence_float":
        entry["sequence"] = 1.0
    elif variant == "sequence_bool":
        entry["sequence"] = True
    elif variant == "sequence_str":
        entry["sequence"] = "1"
    elif variant == "extra_key":
        entry["note"] = "added"
    elif variant == "payload_list":
        entry["payload"] = []
    elif variant == "duplicate_key":
        text = canonical(dict(entry, hash=digest))
        return '{"actor":"attacker",' + text[1:]
    elif variant == "padded":
        return json.dumps(dict(entry, hash=digest), indent=2)
    elif variant == "null":
        return "null"
    elif variant == "list":
        return "[]"
    elif variant == "garbage":
        return "{oops"
    entry["hash"] = sha256_digest(entry)  # attacker recomputes the unkeyed hash
    return canonical(entry)


@pytest.mark.parametrize("variant", [
    "sequence_float", "sequence_bool", "sequence_str", "extra_key", "payload_list",
    "duplicate_key", "padded", "null", "list", "garbage",
])
def test_malformed_or_type_confused_entry_is_invalid_not_raised(db, variant):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)  # single entry chain
    write_entry_text(db, 1, _single_entry_variant(raw_entries(db)[0], variant))
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 1
    if variant != "padded":  # padded JSON is structurally fine for display
        with pytest.raises(ValueError, match="^Stored audit trail is unreadable$"):
            pc.get_audit(db, SHIPMENT["id"])
        with pytest.raises(ValueError, match="^Stored audit trail is unreadable$"):
            pc.validate_declaration(db, SHIPMENT, BROKER, NOW)


def test_deleted_whole_chain_with_declaration_present_is_invalid(db):
    ready(db)
    raw_execute(db, "DELETE FROM audit_entries")
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result.pop("ledger")["reason"] == "chain_deleted"
    assert result == {
        "valid": False, "entries_checked": 0, "first_invalid_sequence": 1,
        "disclaimer": "Hash-chain integrity only; not proof of authenticity or customs approval."}


def test_deleted_middle_entry_is_invalid(db):
    lifecycle(db)
    raw_execute(db, "DELETE FROM audit_entries WHERE sequence = 2")
    assert pc.verify_audit(db, SHIPMENT["id"])["first_invalid_sequence"] == 2


def test_tail_truncation_with_declaration_rollback(db, monkeypatch):
    # Local checks alone cannot see removing the tail AND restoring a genuine
    # earlier declaration snapshot, even in keyed mode; the witness quorum can.
    set_key(monkeypatch, KEY1)
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    snapshot = raw_execute(db, "SELECT declaration_json FROM declarations")[0][0]
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    raw_execute(db, "DELETE FROM audit_entries WHERE sequence = 2")
    write_declaration(db, snapshot)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 2
    assert result["ledger"]["reason"] == "truncated"
    # Remaining limitation: the simulated nodes share this disk, so a writer
    # who also truncates every witness file is not detected.
    for node, _ in pc.ledger.NODES:
        raw_execute(pc.ledger.witness_path(db, node), "DELETE FROM attestations WHERE sequence = 2")
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]


# ---------------------------------------------------------------------------
# SQLite persistence hardening
# ---------------------------------------------------------------------------


def test_connections_are_closed_on_success_and_error(db, monkeypatch):
    opened = []
    real_connect = pc._connect

    def recording(path):
        connection = real_connect(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(pc, "_connect", recording)
    calls = [
        lambda: pc.get_declaration(db, SHIPMENT["id"]),
        lambda: pc.validate_declaration(db, SHIPMENT, BROKER, NOW),  # KeyError
        lambda: pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW),
        lambda: pc.submit_declaration(db, SHIPMENT["id"], "k", BROKER, NOW),  # ValueError
        lambda: pc.validate_declaration(db, SHIPMENT, BROKER, NOW),
        lambda: pc.submit_declaration(db, SHIPMENT["id"], "k", BROKER, NOW),
        lambda: pc.submit_declaration(db, SHIPMENT["id"], "k", BROKER, NOW),  # replay
        lambda: pc.review_declaration(db, SHIPMENT["id"], "accepted", "", REVIEWER, NOW),
        lambda: pc.review_declaration(db, SHIPMENT["id"], "accepted", "", REVIEWER, NOW),  # ValueError
        lambda: pc.get_audit(db, SHIPMENT["id"]),
        lambda: pc.verify_audit(db, SHIPMENT["id"]),
        lambda: pc.init_db(db),
    ]
    for call in calls:
        with contextlib.suppress(KeyError, ValueError):
            call()
    assert len(opened) == len(calls)
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    # No handle is left open: on Windows an open handle would block the rename.
    moved = db + ".moved"
    os.replace(db, moved)
    os.replace(moved, db)


def test_failed_submit_rolls_back_audit_and_idempotency_key(db, monkeypatch):
    ready(db)
    before = raw_execute(db, "SELECT sequence, entry_json FROM audit_entries ORDER BY sequence")
    declaration_before = read_declaration(db)
    real_store = pc._store

    def store_then_fail(*args, **kwargs):
        real_store(*args, **kwargs)
        raise ValueError("Declaration must be ready")

    monkeypatch.setattr(pc, "_store", store_then_fail)
    with pytest.raises(ValueError):
        pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    monkeypatch.undo()
    assert raw_execute(db, "SELECT sequence, entry_json FROM audit_entries ORDER BY sequence") == before
    assert read_declaration(db) == declaration_before
    assert raw_execute(db, "SELECT COUNT(*) FROM submission_keys") == [(0,)]
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]
    assert pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)["status"] == "submitted"


def test_commit_failure_rolls_back_every_write(db, monkeypatch):
    ready(db)
    before = raw_execute(db, "SELECT sequence, entry_json FROM audit_entries ORDER BY sequence")

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(pc.sqlite3, "connect", functools.partial(sqlite3.connect, factory=FailingCommit))
        with pytest.raises(sqlite3.OperationalError):
            pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    assert raw_execute(db, "SELECT sequence, entry_json FROM audit_entries ORDER BY sequence") == before
    assert read_declaration(db)["status"] == "ready"
    assert raw_execute(db, "SELECT COUNT(*) FROM submission_keys") == [(0,)]


def test_new_db_file_is_private_and_special_paths_are_ignored(tmp_path, monkeypatch):
    path = tmp_path / "fresh.sqlite"
    pc.init_db(str(path))
    assert path.exists()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        existing = tmp_path / "existing.sqlite"
        existing.touch()
        os.chmod(existing, 0o644)
        pc.init_db(str(existing))
        assert existing.stat().st_mode & 0o777 == 0o644  # only new files are restricted
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    pc.init_db(":memory:")
    assert list(workdir.iterdir()) == []


def test_trusted_schema_is_disabled(db):
    connection = pc._connect(db)
    try:
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    finally:
        connection.close()


def test_revision_limit(db, monkeypatch):
    monkeypatch.setattr(pc, "MAX_DECLARATION_VERSIONS", 3)
    for version in (1, 2, 3):
        assert pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)["version"] == version
    with pytest.raises(ValueError, match="^Revision limit reached for this declaration$"):
        pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    assert len(pc.get_audit(db, SHIPMENT["id"])) == 3
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_default_limits_match_http_layer():
    assert pc.MAX_DECLARATION_VERSIONS == 500
    assert pc.MAX_IDEMPOTENCY_KEY_LENGTH == 128
    assert pc.MAX_REVIEW_COMMENT_LENGTH == 1000
    assert pc.MAX_PAYLOAD_BYTES == 64 * 1024
    assert pc.MIN_AUDIT_KEY_LENGTH == 16


def test_audit_entry_limit_bounds_repeated_validation(db, monkeypatch):
    monkeypatch.setattr(pc, "MAX_AUDIT_ENTRIES", 3)
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    with pytest.raises(ValueError, match="^Audit limit reached for this declaration$"):
        pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    assert len(pc.get_audit(db, SHIPMENT["id"])) == 3
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_input_length_guards(db):
    big = copy.deepcopy(PAYLOAD)
    big["description"] = "x" * (64 * 1024)
    with pytest.raises(ValueError, match="^Declaration payload is too large$"):
        pc.save_declaration(db, SHIPMENT, big, BROKER, NOW)
    surrogate = copy.deepcopy(PAYLOAD)
    surrogate["shipper"] = "bad\ud800"
    with pytest.raises(ValueError, match="^Declaration payload contains invalid text$"):
        pc.save_declaration(db, SHIPMENT, surrogate, BROKER, NOW)
    assert pc.get_declaration(db, SHIPMENT["id"]) is None
    assert raw_execute(db, "SELECT COUNT(*) FROM audit_entries") == [(0,)]

    ready(db)
    for bad_key in ("k" * 129,):
        with pytest.raises(ValueError, match="^Idempotency key is too long$"):
            pc.submit_declaration(db, SHIPMENT["id"], bad_key, BROKER, NOW)
    for missing in ("", "   ", None):
        with pytest.raises(ValueError, match="^Idempotency key is required$"):
            pc.submit_declaration(db, SHIPMENT["id"], missing, BROKER, NOW)
    assert raw_execute(db, "SELECT COUNT(*) FROM submission_keys") == [(0,)]
    assert pc.submit_declaration(db, SHIPMENT["id"], "k" * 128, BROKER, NOW)["status"] == "submitted"

    with pytest.raises(ValueError, match="^Review comment is too long$"):
        pc.review_declaration(db, SHIPMENT["id"], "accepted", "c" * 1001, REVIEWER, NOW)
    for bad_comment in (None, 5, "bad\udfff"):
        with pytest.raises(ValueError, match="^Invalid review comment$"):
            pc.review_declaration(db, SHIPMENT["id"], "accepted", bad_comment, REVIEWER, NOW)
    assert pc.get_declaration(db, SHIPMENT["id"])["status"] == "submitted"
    reviewed = pc.review_declaration(db, SHIPMENT["id"], "accepted", "c" * 1000, REVIEWER, NOW)
    assert reviewed["status"] == "accepted"
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_missing_declaration_error_is_static(db):
    with pytest.raises(KeyError) as excinfo:
        pc.submit_declaration(db, "attacker<script>", "k", BROKER, NOW)
    assert "attacker" not in str(excinfo.value)


def test_validate_with_tampered_payload_shape_is_static_error(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    declaration = read_declaration(db)
    del declaration["payload"]["shipper"]
    write_declaration(db, declaration)
    with pytest.raises(ValueError, match="^Stored declaration is unreadable$"):
        pc.validate_declaration(db, SHIPMENT, BROKER, NOW)


@pytest.mark.parametrize("tamper", ["garbage", "null", "status", "payload", "extra_key", "version"])
def test_tampered_submission_record_is_never_replayed(db, tamper):
    ready(db)
    submitted = pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    if tamper == "garbage":
        text = "{oops"
    elif tamper == "null":
        text = "null"
    else:
        response = copy.deepcopy(submitted)
        if tamper == "status":
            response["status"] = "accepted"
        elif tamper == "payload":
            response["payload"]["shipper"] = "Injected"
        elif tamper == "extra_key":
            response["injected"] = "<script>"
        elif tamper == "version":
            response["version"] = 7
        text = json.dumps(response)
    raw_execute(db, "UPDATE submission_keys SET response_json = ?", (text,))
    with pytest.raises(ValueError) as excinfo:
        pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    assert str(excinfo.value) == "Stored submission record is unreadable"


def test_replay_after_review_returns_original_submission(db):
    # Idempotency semantics: the same key returns the original response even
    # after the declaration moved on; clients read current state via GET.
    ready(db)
    submitted = pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    pc.review_declaration(db, SHIPMENT["id"], "accepted", "ok", REVIEWER, NOW)
    replay = pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    assert replay == submitted and replay["status"] == "submitted"
    Declaration(**replay)


TRUSTED = "ООО «Волга Экспорт»"


def _trusted_ready(db):
    payload = copy.deepcopy(PAYLOAD)
    payload.update(shipper=TRUSTED, value_usd=45000)
    pc.save_declaration(db, SHIPMENT, payload, BROKER, NOW)
    return pc.validate_declaration(db, SHIPMENT, BROKER, NOW)


def test_ruleset_hash_is_stable_and_covers_parameters(monkeypatch):
    first = pc.ruleset_hash()
    assert first == pc.ruleset_hash() and len(first) == 64
    monkeypatch.setattr(pc, "TRUSTED_SHIPPERS", ("Someone Else",))
    monkeypatch.setattr(pc, "_RULES", tuple(
        (rid, desc, {"trusted_shippers": ["Someone Else"]} if rid == "R4" else params, check, fail)
        for rid, desc, params, check, fail in pc._RULES))
    assert pc.ruleset_hash() != first


def test_evaluate_ruleset_is_deterministic_and_never_echoes_input():
    payload = copy.deepcopy(PAYLOAD)
    payload["shipper"] = "SECRET-MARKER"
    result = pc.evaluate_ruleset(payload, SHIPMENT)
    assert result == pc.evaluate_ruleset(payload, SHIPMENT)
    assert result["decision"] == "manual_review" and "SECRET-MARKER" not in json.dumps(result)
    payload["mass_kg"] = 0
    assert not next(r for r in pc.evaluate_ruleset(payload, SHIPMENT)["results"] if r["rule_id"] == "R3")["passed"]


def test_auto_accept_audit_valid_replay_and_tamper_detection(db):
    _trusted_ready(db)
    submitted = pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW, SHIPMENT)
    assert submitted["status"] == "auto_accepted" and submitted["clearance"]["decision"] == "auto_accepted"
    Declaration(**submitted)
    audit = pc.get_audit(db, SHIPMENT["id"])
    assert audit[-1]["action"] == "submit" and audit[-1]["payload"]["status"] == "auto_accepted"
    assert audit[-1]["payload"]["clearance"]["ruleset_hash"] == pc.ruleset_hash()
    for entry in audit:
        AuditEntry(**entry)
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]
    assert pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW, SHIPMENT) == submitted
    assert len(pc.get_audit(db, SHIPMENT["id"])) == len(audit)
    with pytest.raises(ValueError):
        pc.review_declaration(db, SHIPMENT["id"], "accepted", "", REVIEWER, NOW)
    # Editing the stored clearance on the declaration row is detected.
    declaration = read_declaration(db)
    declaration["clearance"]["results"][0]["passed"] = False
    write_declaration(db, declaration)
    assert not pc.verify_audit(db, SHIPMENT["id"])["valid"]


def test_tampered_submit_clearance_entry_is_detected(db):
    _trusted_ready(db)
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW, SHIPMENT)
    entry = raw_entries(db)[-1]
    entry["payload"]["clearance"]["results"][3]["passed"] = False
    write_entry_text(db, entry["sequence"], canonical(entry))
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == entry["sequence"]


def test_untrusted_shipper_stays_submitted_and_reviewable(db):
    ready(db)
    submitted = pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW, SHIPMENT)
    assert submitted["status"] == "submitted" and submitted["clearance"]["decision"] == "manual_review"
    assert pc.review_declaration(db, SHIPMENT["id"], "accepted", "", REVIEWER, NOW)["status"] == "accepted"
    assert pc.verify_audit(db, SHIPMENT["id"])["valid"]
