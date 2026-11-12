"""Simulated distributed ledger: in-process witness nodes co-signing the audit chain."""
import contextlib
import copy
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import ledger
from backend.app import preclearance as pc
from backend.app.main import create_app
from backend.app.schemas import AuditVerification, LedgerStatus

NOW = "2026-11-12T08:00:00Z"
BROKER = {"id": "demo-broker", "role": "broker"}
REVIEWER = {"id": "demo-reviewer", "role": "reviewer"}
SHIPMENT = {"id": "SCT-001", "mass_kg": 20000, "container_id": "C1"}
PAYLOAD = {"shipper": "Exporter", "consignee": "Importer", "description": "Parts",
           "mass_kg": 20000, "value_usd": 100, "origin_country": "RU", "hs_code": "123456",
           "container_id": "C1", "documents": [
               {"kind": kind, "number": kind, "container_id": "C1", "mass_kg": 20000}
               for kind in ("invoice", "packing_list", "transport")]}
NODE_IDS = [node for node, _ in ledger.NODES]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (pc.AUDIT_KEY_ENV, ledger.KEYS_ENV, "DEMO_BROKER_TOKEN", "DEMO_REVIEWER_TOKEN",
                 "DEMO_DISABLE_DEFAULT_TOKENS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.sqlite")
    pc.init_db(path)
    return path


def raw_execute(path, sql, params=()):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        with conn:
            return conn.execute(sql, params).fetchall()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lifecycle(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    pc.review_declaration(db, SHIPMENT["id"], "needs_revision", "Fix", REVIEWER, NOW)


def remove_witness(db, node):
    os.remove(ledger.witness_path(db, node))


def test_normal_flow_all_nodes_in_sync(db):
    lifecycle(db)
    result = pc.verify_audit(db, SHIPMENT["id"])
    AuditVerification(**result)
    assert result["valid"]
    assert result["ledger"] == {"status": "anchored", "reason": None, "anchored": True, "quorum": 2,
                                "nodes_total": 3, "nodes_available": 3, "nodes_in_sync": 3,
                                "attested_sequence": 4, "local_sequence": 4}
    status = pc.ledger_status(db, SHIPMENT["id"])
    LedgerStatus(**status)
    heads = {entry["hash"] for entry in pc.get_audit(db, SHIPMENT["id"])[-1:]}
    for node in status["nodes"]:
        assert node["state"] == "in_sync" and node["signature_valid"] and node["last_sequence"] == 4
        assert node["head_hash"] in heads
    assert [n["node_id"] for n in status["nodes"]] == NODE_IDS


def test_witness_files_are_private_and_next_to_db(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    for node in NODE_IDS:
        path = ledger.witness_path(db, node)
        assert os.path.dirname(path) == os.path.dirname(db) and os.path.isfile(path)
        if os.name == "posix":
            assert os.stat(path).st_mode & 0o777 == 0o600


def test_recomputed_local_rewrite_is_invalid(db):
    lifecycle(db)
    rows = [json.loads(t) for (t,) in raw_execute(db, "SELECT entry_json FROM audit_entries ORDER BY sequence")]
    previous = "0" * 64
    for entry in rows:  # unkeyed attacker rewrites entry 1 and re-links everything
        if entry["sequence"] == 1:
            entry["actor"] = "forged"
        entry.pop("hash")
        entry["previous_hash"] = previous
        entry["hash"] = previous = pc._entry_digest(entry, None)
        raw_execute(db, "UPDATE audit_entries SET entry_json = ? WHERE sequence = ?",
                    (canonical(entry), entry["sequence"]))
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 1
    assert result["ledger"]["status"] == "conflict" and result["ledger"]["reason"] == "hash_mismatch"
    assert {n["state"] for n in pc.ledger_status(db, SHIPMENT["id"])["nodes"]} == {"diverged"}


def test_truncated_tail_with_declaration_rollback_is_invalid(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    snapshot = raw_execute(db, "SELECT declaration_json FROM declarations")[0][0]
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    raw_execute(db, "DELETE FROM audit_entries WHERE sequence = 3")
    raw_execute(db, "UPDATE declarations SET declaration_json = ?", (snapshot,))
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["entries_checked"] == 2  # the local chain alone looks fine
    assert not result["valid"] and result["first_invalid_sequence"] == 3
    assert result["ledger"]["reason"] == "truncated" and result["ledger"]["attested_sequence"] == 3


def test_deleted_declaration_and_chain_is_invalid(db):
    lifecycle(db)
    raw_execute(db, "DELETE FROM audit_entries")
    raw_execute(db, "DELETE FROM declarations")
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert not result["valid"] and result["first_invalid_sequence"] == 1
    assert result["ledger"]["reason"] == "chain_deleted"


def test_one_witness_deleted_still_valid_with_two_of_three(db):
    lifecycle(db)
    remove_witness(db, "customs-ir")
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] and result["ledger"]["anchored"]
    assert result["ledger"]["nodes_available"] == 2 and result["ledger"]["nodes_in_sync"] == 2
    states = {n["node_id"]: n["state"] for n in pc.ledger_status(db, SHIPMENT["id"])["nodes"]}
    assert states == {"customs-ru": "in_sync", "customs-ir": "unavailable", "port-operator": "in_sync"}
    # The next write still reaches quorum; the re-created empty node refuses the
    # continuation and is reported as behind, not silently re-synced.
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] and result["ledger"]["status"] == "anchored"
    states = {n["node_id"]: n["state"] for n in pc.ledger_status(db, SHIPMENT["id"])["nodes"]}
    assert states["customs-ir"] == "behind"


def test_two_witnesses_deleted_is_not_anchored_but_flow_continues(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    remove_witness(db, "customs-ru")
    remove_witness(db, "port-operator")
    assert pc.verify_audit(db, SHIPMENT["id"])["ledger"]["status"] == "no_quorum"
    validated = pc.validate_declaration(db, SHIPMENT, BROKER, NOW)  # local state change succeeds
    assert validated["status"] == "ready"
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"]
    assert result["ledger"]["status"] == "not_anchored" and not result["ledger"]["anchored"]
    assert result["ledger"]["reason"] == "unanchored_entries"


def test_witness_refuses_forked_append(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    entry = pc.get_audit(db, SHIPMENT["id"])[0]
    other = "a" * 64
    for node in NODE_IDS:
        # Same sequence with a different hash, a gap, and a wrong previous hash are refused.
        assert ledger.witness_append(db, node, SHIPMENT["id"], 1, other, "0" * 64, NOW) is None
        assert ledger.witness_append(db, node, SHIPMENT["id"], 3, other, entry["hash"], NOW) is None
        assert ledger.witness_append(db, node, SHIPMENT["id"], 2, other, other, NOW) is None
        rows = raw_execute(ledger.witness_path(db, node), "SELECT sequence, entry_hash FROM attestations")
        assert rows == [(1, entry["hash"])]
    assert ledger.witness_append(db, "customs-ru", SHIPMENT["id"], 2, other, entry["hash"], NOW)


def test_tampered_witness_record_fails_signature(db):
    lifecycle(db)
    raw_execute(ledger.witness_path(db, "customs-ru"), "UPDATE attestations SET at = 'forged' WHERE sequence = 2")
    status = pc.ledger_status(db, SHIPMENT["id"])
    node = status["nodes"][0]
    assert node["state"] == "invalid_signature" and node["signature_valid"] is False
    assert status["status"] == "anchored"  # the other two still form a quorum


def test_key_override_and_no_key_material_in_responses(db, monkeypatch):
    secret = "customs-ru-override-key-123456"
    monkeypatch.setenv(ledger.KEYS_ENV, f"customs-ru={secret}")
    lifecycle(db)
    text = json.dumps(pc.ledger_status(db, SHIPMENT["id"])) + json.dumps(pc.verify_audit(db, SHIPMENT["id"]))
    assert secret not in text and "NOT-A-SECRET" not in text
    monkeypatch.setenv(ledger.KEYS_ENV, "customs-ru=short")
    with pytest.raises(RuntimeError) as excinfo:
        pc.verify_audit(db, SHIPMENT["id"])
    assert "short" not in str(excinfo.value)


def test_failed_witness_does_not_break_local_write(db, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger, "witness_append", broken)
    saved = pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    assert saved["version"] == 1 and len(pc.get_audit(db, SHIPMENT["id"])) == 1


def test_memory_database_has_no_ledger():
    assert ledger.witness_path(":memory:", "customs-ru") is None
    assert ledger.compare(":memory:", "SCT-001", [])["status"] == "disabled"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "demo.sqlite3"))) as test_client:
        yield test_client


def test_ledger_endpoint(client):
    headers = {"Authorization": "Bearer demo-broker-token"}
    empty = client.get("/api/shipments/SCT-001/ledger", headers=headers)
    assert empty.status_code == 200
    body = empty.json()
    assert body["status"] == "anchored" and body["nodes_in_sync"] == 3 and body["attested_sequence"] == 0
    assert "in-process" in body["disclaimer"]
    verify = client.get("/api/shipments/SCT-001/audit/verify", headers=headers).json()
    assert verify["valid"] and verify["ledger"]["status"] == "anchored"


def test_ledger_endpoint_requires_auth_and_known_shipment(client):
    assert client.get("/api/shipments/SCT-001/ledger").status_code == 401
    response = client.get("/api/shipments/no-such-shipment/ledger",
                          headers={"Authorization": "Bearer demo-reviewer-token"})
    assert response.status_code == 404 and response.json() == {"detail": "Shipment not found"}
