"""Regression checks for cross-module consistency found during lead review."""
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


BROKER = {"Authorization": "Bearer dev-broker-token"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("BROKER_TOKEN", "REVIEWER_TOKEN", "DISABLE_DEFAULT_TOKENS"):
        monkeypatch.delenv(name, raising=False)


def test_document_delay_does_not_move_original_plan_and_events_follow_route(tmp_path):
    headers = BROKER
    path = "/api/shipments/SCT-001"
    with TestClient(create_app(str(tmp_path / "review.sqlite3"))) as client:
        normal = client.get(path, headers=headers).json()
        original_departure = normal["current_route"]["legs"][0]["departure_at"]
        item = normal["shipment"]
        payload = {
            "shipper": "Exporter", "consignee": "Importer", "description": "Parts",
            "mass_kg": item["mass_kg"], "value_usd": 45000, "origin_country": "RU",
            "hs_code": "847990", "container_id": item["container_id"], "documents": [],
        }
        assert client.put(path + "/preclearance", json=payload, headers=headers).status_code == 200
        delayed = client.get(path, headers=headers).json()
        assert delayed["current_route"]["missed_connection"]
        assert not delayed["current_route"]["feasible"]
        assert delayed["current_route"]["legs"][0]["departure_at"] > original_departure
        before = {e["id"]: e for e in normal["shipment"]["events"]}
        events = {e["event_type"]: e for e in delayed["shipment"]["events"]}
        assert events["delivery"]["estimated_at"] == delayed["current_route"]["eta"]
        assert events["sea_departure"]["estimated_at"] == delayed["current_route"]["legs"][0]["departure_at"]
        for event in delayed["shipment"]["events"]:
            assert event["planned_at"] == before[event["id"]]["planned_at"]
            assert event["actual_at"] == before[event["id"]]["actual_at"]


def test_storm_arrival_event_uses_scenario_while_actual_departure_is_preserved(tmp_path):
    with TestClient(create_app(str(tmp_path / "storm.sqlite3"))) as client:
        path = "/api/shipments/SCT-001"
        before = client.get(path, headers=BROKER).json()
        after = client.post(path + "/scenario", json={"kind": "storm"}, headers=BROKER).json()
        original = {e["event_type"]: e for e in before["shipment"]["events"]}
        events = {e["event_type"]: e for e in after["shipment"]["events"]}
        assert events["port_arrival"]["estimated_at"] == "2026-11-12T13:00:00Z"
        assert events["rail_departure"] == original["rail_departure"]
        assert events["delivery"]["estimated_at"] == after["current_route"]["eta"]
