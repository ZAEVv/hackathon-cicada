"""Cross-feature regressions found by the final QA pass (catalog x scenarios x
hub forecast x summary x pre-clearance x frontend translations)."""
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from backend.app import hubs, preclearance
from backend.app.main import create_app

BROKER = {"Authorization": "Bearer demo-broker-token"}
SHIPMENTS = [f"SCT-00{i}" for i in range(1, 7)]
KINDS = ("none", "storm", "congestion", "closure")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DEMO_BROKER_TOKEN", "DEMO_REVIEWER_TOKEN", "DEMO_DISABLE_DEFAULT_TOKENS",
                 "AUDIT_HMAC_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def shared_client(tmp_path_factory):
    path = tmp_path_factory.mktemp("qa") / "qa.sqlite3"
    with TestClient(create_app(str(path))) as client:
        yield client


@pytest.fixture
def client(shared_client):
    yield shared_client
    shared_client.app.state.scenarios.clear()
    shared_client.app.state.selected.clear()


def scenario(client, sid, kind):
    response = client.post(f"/api/shipments/{sid}/scenario", json={"kind": kind}, headers=BROKER)
    assert response.status_code == 200
    return response.json()


def forecast(client, sid):
    response = client.get("/api/hubs/forecast", params={"shipment_id": sid}, headers=BROKER)
    assert response.status_code == 200
    return {hub["port_id"]: hub for hub in response.json()["hubs"]}, response.json()


@pytest.mark.parametrize("kind", KINDS)
def test_every_departure_event_follows_the_current_route(client, kind):
    # Bug: only sea_departure was refreshed; SCT-006's rail_departure and SCT-002's
    # road_departure kept a static estimate that contradicted the shown route.
    for sid in SHIPMENTS:
        detail = scenario(client, sid, kind)
        for event in detail["shipment"]["events"]:
            if event["actual_at"] or not event["event_type"].endswith("_departure"):
                continue
            mode = event["event_type"].split("_")[0]
            legs = [leg for leg in detail["current_route"]["legs"]
                    if leg["mode"] == mode and leg["origin_id"] == event["location_id"]]
            expected = legs[0]["departure_at"] if legs else None
            assert event["estimated_at"] == expected, (sid, kind, event["event_type"])


def test_rail_departure_of_eastern_shipment_after_missed_connection(client):
    detail = scenario(client, "SCT-006", "none")
    assert detail["current_route"]["missed_connection"]
    event = next(e for e in detail["shipment"]["events"] if e["event_type"] == "rail_departure"
                 and e["location_id"] == "aktau")
    assert event["estimated_at"] == detail["current_route"]["legs"][0]["departure_at"]
    assert event["estimated_at"] > event["planned_at"]


def test_unavailable_route_counts_as_at_risk_in_dashboard_and_summary(client):
    # Bug: under closure SCT-001's route is unavailable (feasible=false,
    # missed_connection=false, low readiness risk), yet the summary showed
    # "docs_pending"/"in_transit" and the dashboard did not count it at risk.
    before = client.get("/api/dashboard", headers=BROKER).json()["summary"]["high_risk"]
    detail = scenario(client, "SCT-001", "closure")
    assert not detail["current_route"]["feasible"] and not detail["current_route"]["missed_connection"]
    assert detail["prediction"]["risk"] != "high"
    dashboard = client.get("/api/dashboard", headers=BROKER).json()
    assert dashboard["summary"]["high_risk"] == before + 1
    recent = client.get("/api/summary", headers=BROKER).json()["recent_shipments"]
    assert next(r for r in recent if r["id"] == "SCT-001")["status"] == "at_risk"
    # Choosing a feasible alternative clears it again.
    alternative = next(a for a in detail["alternatives"] if a["feasible"])
    assert client.post("/api/shipments/SCT-001/route", json={"route_id": alternative["id"]},
                       headers=BROKER).status_code == 200
    assert client.get("/api/dashboard", headers=BROKER).json()["summary"]["high_risk"] == before


@pytest.mark.parametrize("kind", ("storm", "congestion"))
def test_regional_scenario_hits_the_focus_shipments_own_port(client, kind):
    # Bug: storm/congestion on an eastern shipment (Aktau) was applied at
    # Makhachkala, so the shipment's own port showed no effect.
    base, _ = forecast(client, "SCT-004")
    scenario(client, "SCT-004", kind)
    hit, payload = forecast(client, "SCT-004")
    assert payload["regional_scenario"] == kind
    aktau = (hit["aktau"]["peak_utilization"], hit["aktau"]["max_queue_hours"])
    assert aktau > (base["aktau"]["peak_utilization"], base["aktau"]["max_queue_hours"])
    assert hit["makhachkala"] == base["makhachkala"]
    # The western line keeps the Makhachkala storm of the jury script.
    client.app.state.scenarios.clear()
    west_base, _ = forecast(client, "SCT-001")
    scenario(client, "SCT-001", kind)
    west, _ = forecast(client, "SCT-001")
    assert west["makhachkala"]["peak_utilization"] > west_base["makhachkala"]["peak_utilization"]


def test_regional_node_falls_back_without_a_focus():
    ports = [{"id": hubs.REGIONAL_NODE, "kind": "port"}, {"id": "p2", "kind": "port"}]
    storm = {"kind": "storm", "wind_mps": 22, "arrival_delay_hours": 3, "queue_extra_hours": 2,
             "blocked_service_ids": []}
    effects = hubs._regional_effects(ports, [], storm, None)
    assert min(effects[hubs.REGIONAL_NODE]["capacity"]) < 1 and min(effects["p2"]["capacity"]) == 1
    effects = hubs._regional_effects(ports, [], storm, "p2")
    assert min(effects["p2"]["capacity"]) < 1 and min(effects[hubs.REGIONAL_NODE]["capacity"]) == 1


# ---------------------------------------------------------------- translations

def _translation_keys():
    source = (ROOT / "frontend/src/lib.ts").read_text(encoding="utf-8")
    block = source.split("export const translations", 1)[1].split("\n};", 1)[0]
    keys = set(re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', block, re.M))
    keys |= set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", block, re.M))
    return keys


def _emitted_strings(client):
    found = set()
    for kind in KINDS:
        for sid in SHIPMENTS:
            detail = scenario(client, sid, kind)
            found.update(detail["prediction"]["factors"])
            for route in (detail["current_route"], *detail["alternatives"]):
                found.add(route["label"])
                if route["reason"]:
                    found.add(route["reason"])
            found.update(e["event_type"] for e in detail["shipment"]["events"])
        for sid in SHIPMENTS:
            _, payload = forecast(client, sid)
            for window in payload["bottlenecks"]:
                found.update(window["reasons"])
        client.app.state.scenarios.clear()
    return found


def test_every_label_reason_and_factor_has_a_russian_translation(client):
    missing = _emitted_strings(client) - _translation_keys()
    assert not missing, sorted(missing)


def test_validation_rule_and_error_messages_have_russian_translations():
    keys = _translation_keys()
    shipment = {"container_id": "C1", "mass_kg": 100.0}
    bad = {"shipper": " ", "consignee": "", "description": "", "origin_country": "R1",
           "hs_code": "x", "container_id": "C2", "mass_kg": 10, "value_usd": 1,
           "documents": [{"kind": "invoice", "number": "", "container_id": "C3", "mass_kg": 1},
                         {"kind": "invoice", "number": "", "container_id": "C3", "mass_kg": 1}]}
    messages = {issue["message"] for issue in preclearance._validate(bad, shipment)["issues"]}
    for kind in ("invoice", "packing_list", "transport"):
        messages |= {f"Missing {kind}", f"Duplicate {kind}"}
    messages |= {failure for *_, failure in preclearance._RULES} | {"OK"}
    messages |= {"broker role required", "reviewer role required"}
    internal = {"duplicate key", "not text"}  # caught and re-raised as "unreadable"
    for module in ("preclearance.py", "main.py"):
        text = (ROOT / "backend/app" / module).read_text(encoding="utf-8")
        messages |= set(re.findall(r'raise ValueError\("([^"]+)"\)', text)) - internal
        messages |= set(re.findall(r'HTTPException\(\d{3}, "([^"]+)"\)', text))
        messages |= set(re.findall(r'^_UNREADABLE_\w+ = "([^"]+)"', text, re.M))
    messages.add(hubs.DISCLAIMER)
    missing = messages - keys
    assert not missing, sorted(missing)
