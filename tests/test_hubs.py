"""Hub load / bottleneck forecast: module behaviour and the API contract."""
from datetime import datetime, timedelta
from math import isfinite

import pytest
from fastapi.testclient import TestClient

from backend.app import hubs, routing
from backend.app.main import create_app
from backend.app.schemas import SIMULATION_NOW, HubForecast

BROKER = {"Authorization": "Bearer dev-broker-token"}
REVIEWER = {"Authorization": "Bearer dev-reviewer-token"}
URL = "/api/hubs/forecast"
ENV_VARS = ("BROKER_TOKEN", "REVIEWER_TOKEN", "DISABLE_DEFAULT_TOKENS", "ENABLE_API_DOCS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def shared_client(tmp_path_factory):
    # One application (one model fit) for the module; tests reset what they change.
    path = tmp_path_factory.mktemp("hubs") / "demo.sqlite3"
    with TestClient(create_app(str(path))) as test_client:
        yield test_client


@pytest.fixture
def client(shared_client):
    yield shared_client
    shared_client.app.state.scenarios.clear()
    shared_client.app.state.selected.clear()


def when(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get(client, shipment_id="SCT-001"):
    response = client.get(URL, params={"shipment_id": shipment_id}, headers=BROKER)
    assert response.status_code == 200
    return response.json()


def scenario(client, kind, shipment_id="SCT-001"):
    response = client.post(f"/api/shipments/{shipment_id}/scenario", json={"kind": kind}, headers=BROKER)
    assert response.status_code == 200


def hub(payload, port_id):
    return next(item for item in payload["hubs"] if item["port_id"] == port_id)


def assert_well_formed(payload, port_ids):
    HubForecast.model_validate(payload)
    now = when(SIMULATION_NOW)
    assert payload["synthetic"] is True and payload["simulation_now"] == SIMULATION_NOW
    assert payload["horizon_hours"] == 72 and payload["step_hours"] == 1
    assert "ynthetic" in payload["disclaimer"]
    assert [item["port_id"] for item in payload["hubs"]] == list(port_ids)
    for item in payload["hubs"]:
        points = item["points"]
        assert [p["offset_hours"] for p in points] == list(range(73))
        assert [when(p["at"]) for p in points] == [now + timedelta(hours=h) for h in range(73)]
        for point in points:
            numbers = [point[key] for key in ("utilization", "load_tph", "capacity_tph",
                                               "background_tph", "shipments_tph", "queue_hours")]
            assert all(isfinite(value) and value >= 0 for value in numbers)
            assert point["utilization"] <= hubs.MAX_UTILIZATION
            assert point["queue_hours"] <= hubs.MAX_WAIT_HOURS
            assert point["capacity_tph"] > 0
        assert item["peak_utilization"] == max(p["utilization"] for p in points)
        hot = any(p["utilization"] >= payload["bottleneck_threshold"] for p in points)
        assert (item["status"] == "bottleneck") == hot
        assert item["bottleneck_count"] == sum(w["port_id"] == item["port_id"] for w in payload["bottlenecks"])
    windows = payload["bottlenecks"]
    assert [w["start_at"] for w in windows] == sorted(w["start_at"] for w in windows)
    for window in windows:
        start, stop, peak = when(window["start_at"]), when(window["end_at"]), when(window["peak_at"])
        assert now <= start < stop <= now + timedelta(hours=72)
        assert start <= peak < stop or peak == stop == now + timedelta(hours=72)
        assert window["peak_utilization"] >= payload["bottleneck_threshold"]
        assert window["severity"] == ("high" if window["peak_utilization"] >= 1 else "medium")
        assert window["reasons"] and all(isinstance(code, str) and code for code in window["reasons"])
        assert window["shipment_ids"] == sorted(set(window["shipment_ids"]))
        assert window["port_id"] in port_ids
    for port_id in port_ids:
        own = [w for w in windows if w["port_id"] == port_id]
        assert all(a["end_at"] < b["start_at"] for a, b in zip(own, own[1:]))


def test_base_forecast_covers_72_hours_for_every_catalog_port(client):
    port_ids = [port["id"] for port in client.app.state.catalog["ports"]]
    payload = get(client)
    assert payload["regional_scenario"] == "none" and payload["focus_shipment_id"] == "SCT-001"
    assert_well_formed(payload, port_ids)
    # Demo shipments are visible on top of the synthetic background.
    assert any(p["shipments_tph"] > 0 and "SCT-001" in p["shipment_ids"]
               for p in hub(payload, "makhachkala")["points"])


def test_forecast_is_deterministic(client):
    assert get(client) == get(client)
    with_default = client.get(URL, headers=BROKER)
    assert with_default.status_code == 200
    assert with_default.json()["hubs"] == get(client)["hubs"]
    now = when(SIMULATION_NOW)
    assert hubs.background_profile("makhachkala", now) == hubs.background_profile("makhachkala", now)
    assert hubs.background_profile("makhachkala", now) != hubs.background_profile("olya", now)


def test_storm_on_selected_shipment_raises_makhachkala_peak(client):
    base = get(client)
    scenario(client, "storm")
    storm = get(client)
    assert storm["regional_scenario"] == "storm"
    assert_well_formed(storm, [item["port_id"] for item in base["hubs"]])
    assert hub(storm, "makhachkala")["peak_utilization"] > hub(base, "makhachkala")["peak_utilization"]
    assert hub(storm, "makhachkala")["max_queue_hours"] > hub(base, "makhachkala")["max_queue_hours"]
    window = next(w for w in storm["bottlenecks"] if w["port_id"] == "makhachkala")
    assert hubs.R_STORM in window["reasons"] and "SCT-001" in window["shipment_ids"]
    # Storm is local: an unrelated inland terminal keeps its base picture.
    assert hub(storm, "rail_hub") == hub(base, "rail_hub")
    # Another shipment's view keeps SCT-001's delayed route but no regional storm.
    other = get(client, "SCT-002")
    assert other["regional_scenario"] == "none"
    assert hub(other, "makhachkala")["peak_utilization"] < hub(storm, "makhachkala")["peak_utilization"]


def test_congestion_extends_wait_at_the_regional_node(client):
    base = get(client)
    scenario(client, "congestion")
    congested = get(client)
    assert hub(congested, "makhachkala")["max_queue_hours"] >= hub(base, "makhachkala")["max_queue_hours"] + 5
    assert any(hubs.R_CONGESTION in w["reasons"] for w in congested["bottlenecks"])


def test_closure_accumulates_at_origin_and_shifts_load_to_alternative(client):
    base = get(client)
    scenario(client, "closure")
    closed = get(client)
    assert_well_formed(closed, [item["port_id"] for item in base["hubs"]])

    def mean(payload, port_id, key):
        points = hub(payload, port_id)["points"]
        return sum(point[key] for point in points) / len(points)

    # Background flow leaves the closed berth for the alternative port ...
    assert mean(closed, "olya", "background_tph") > mean(base, "olya", "background_tph")
    assert mean(closed, "makhachkala", "background_tph") < mean(base, "makhachkala", "background_tph")
    assert mean(closed, "anzali", "background_tph") < mean(base, "anzali", "background_tph")
    # ... while the origin loses capacity and stays loaded.
    assert mean(closed, "makhachkala", "capacity_tph") < mean(base, "makhachkala", "capacity_tph")
    assert mean(closed, "makhachkala", "utilization") > mean(base, "makhachkala", "utilization")
    assert any(hubs.R_DIVERTED in w["reasons"] for w in closed["bottlenecks"] if w["port_id"] == "olya")
    # SCT-001 has no feasible planned route: it is held at Makhachkala to the end.
    assert "SCT-001" in hub(closed, "makhachkala")["points"][-1]["shipment_ids"]
    held = [w for w in closed["bottlenecks"] if w["port_id"] == "makhachkala"]
    assert held and all(hubs.R_CLOSURE in w["reasons"] for w in held)
    assert any(hubs.R_HELD in w["reasons"] for w in held)


def test_selected_alternative_moves_the_shipment_to_other_nodes(client):
    scenario(client, "closure")
    detail = client.get("/api/shipments/SCT-001", headers=BROKER).json()
    alternative = next(r for r in detail["alternatives"] if r["feasible"]
                       and any(leg["origin_id"] == "olya" for leg in r["legs"]))
    chosen = client.post("/api/shipments/SCT-001/route", json={"route_id": alternative["id"]}, headers=BROKER)
    assert chosen.status_code == 200
    payload = get(client)
    assert any("SCT-001" in p["shipment_ids"] for p in hub(payload, "olya")["points"])
    assert "SCT-001" not in hub(payload, "makhachkala")["points"][-1]["shipment_ids"]


def test_authentication_and_unknown_shipment(client):
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.get(URL, params={"shipment_id": "SCT-001"}, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {"detail": "Valid bearer token required"}
    assert client.get(URL, params={"shipment_id": "SCT-001"}, headers=REVIEWER).status_code == 200
    missing = client.get(URL, params={"shipment_id": "SCT-404-unknown"}, headers=BROKER)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Shipment not found"}
    assert "SCT-404-unknown" not in missing.text
    rejected = client.get(URL, params={"shipment_id": "bad id<script>"}, headers=BROKER)
    assert rejected.status_code == 422
    assert "script" not in rejected.text


def test_works_when_a_new_port_is_added_to_the_catalog(client):
    ports = client.app.state.catalog["ports"]
    extra = [{"id": "aktau", "name": "Aktau Sea Port", "country": "KZ", "lat": 43.6, "lon": 51.2, "kind": "port"},
             {"id": "brand_new_hub", "name": "New Hub", "country": "TM", "lat": 40.0, "lon": 53.0, "kind": "depot"}]
    ports.extend(extra)
    try:
        payload = get(client)
        assert_well_formed(payload, [port["id"] for port in ports])
        new = hub(payload, "brand_new_hub")
        assert new["nominal_capacity_tph"] == hubs.DEFAULT_PARAMS["capacity_tph"]
        assert 0 < new["peak_utilization"] < hubs.BOTTLENECK_THRESHOLD
        scenario(client, "closure")
        assert_well_formed(get(client), [port["id"] for port in ports])
    finally:
        del ports[-len(extra):]


def test_module_handles_an_arbitrary_catalog_without_the_regional_node():
    ports = [{"id": "p1", "kind": "port"}, {"id": "p2", "kind": "terminal"}, {"id": "p3"}]
    services = [{"id": "sea-main", "origin_id": "p1", "destination_id": "p3"},
                {"id": "bypass", "origin_id": "p1", "destination_id": "p2"}]
    leg = {"service_id": "sea-main", "origin_id": "p1", "destination_id": "p3",
           "departure_at": "2026-11-12T20:00:00Z", "arrival_at": "2026-11-13T20:00:00Z"}
    shipment = {"id": "X-1", "mass_kg": 30000.0, "current_location_id": "p1",
                "planned_port_arrival": "2026-11-12T10:00:00Z", "observed_delay_hours": 1.0}

    def detail(kind, legs):
        return {"shipment": shipment, "scenario": routing.scenario_profile(kind),
                "current_route": {"legs": legs}}

    base = hubs.forecast(ports, services, [detail("none", [leg])], routing.scenario_profile("none"),
                         focus_shipment_id="X-1")
    assert_well_formed(base, ["p1", "p2", "p3"])
    assert any("X-1" in p["shipment_ids"] for p in hub(base, "p3")["points"])
    # Without Makhachkala the storm lands on the requested shipment's node.
    storm = hubs.forecast(ports, services, [detail("storm", [leg])], routing.scenario_profile("storm"),
                          focus_shipment_id="X-1")
    assert_well_formed(storm, ["p1", "p2", "p3"])
    assert hub(storm, "p1")["peak_utilization"] > hub(base, "p1")["peak_utilization"]
    assert hub(storm, "p2") == hub(base, "p2")
    closed = hubs.forecast(ports, services, [detail("closure", [])], routing.scenario_profile("closure"),
                           focus_shipment_id="X-1")
    assert_well_formed(closed, ["p1", "p2", "p3"])
    assert hub(closed, "p2")["peak_utilization"] > hub(base, "p2")["peak_utilization"]
    assert "X-1" in hub(closed, "p1")["points"][-1]["shipment_ids"]
    # No shipments, no ports, unknown focus: still a valid (empty) forecast.
    empty = hubs.forecast([], [], [], None, focus_shipment_id=None)
    assert empty["hubs"] == [] and empty["bottlenecks"] == []
    HubForecast.model_validate(empty)
