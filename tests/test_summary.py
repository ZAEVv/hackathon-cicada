"""Corridor summary endpoint: determinism, validation, auth and internal consistency."""
import math

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app

BROKER = {"Authorization": "Bearer dev-broker-token"}
ENV_VARS = ("BROKER_TOKEN", "REVIEWER_TOKEN", "DISABLE_DEFAULT_TOKENS", "ENABLE_API_DOCS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # One app per module: startup trains the model and the endpoint is read-only.
    path = tmp_path_factory.mktemp("summary") / "demo.sqlite3"
    with TestClient(create_app(str(path))) as test_client:
        yield test_client


def get(client, weeks=4):
    response = client.get(f"/api/summary?weeks={weeks}", headers=BROKER)
    assert response.status_code == 200, response.text
    return response.json()


def numbers(value):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from numbers(item)


@pytest.mark.parametrize("weeks", [4, 13])
def test_deterministic_and_flagged_synthetic(client, weeks):
    first, second = get(client, weeks), get(client, weeks)
    assert first == second
    assert first["synthetic"] is True and first["weeks"] == weeks
    assert len(first["weekly"]) == weeks
    assert "Синтетическая" in first["disclaimer"]


def test_default_is_four_weeks(client):
    assert client.get("/api/summary", headers=BROKER).json()["weeks"] == 4


@pytest.mark.parametrize("value", ["0", "1", "5", "12", "14", "-4", "abc", "4.5", "", "%00", "9" * 400])
def test_weeks_validation_never_echoes_input(client, value):
    response = client.get("/api/summary", params={"weeks": value}, headers=BROKER)
    assert response.status_code == 422
    if len(value) > 3:  # short digits would collide with the "4 or 13" message
        assert value not in response.text


def test_auth_required(client):
    assert client.get("/api/summary?weeks=4").status_code == 401
    assert client.get("/api/summary?weeks=4", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Authentication wins over parameter validation.
    assert client.get("/api/summary?weeks=7").status_code == 401


def test_weekly_series_matches_kpis(client):
    body = get(client, 4)
    assert sum(w["shipments"] for w in body["weekly"]) == body["kpis"]["shipments"]["value"]
    assert body["weekly"] == sorted(body["weekly"], key=lambda w: w["week_start"])
    # co2e = period tonnage * intensity
    tonnes = sum(w["tonnes"] for w in body["weekly"])
    assert body["kpis"]["co2e_tonnes"]["value"] == pytest.approx(
        tonnes * body["co2e_intensity_kg_per_tonne"] / 1000)


def test_deltas_consistent(client):
    kpis = get(client, 4)["kpis"]
    for kpi in kpis.values():
        assert kpi["previous"] is not None
        assert kpi["delta"] == pytest.approx(kpi["value"] - kpi["previous"])
        if kpi["previous"]:
            assert kpi["delta_pct"] == pytest.approx(kpi["delta"] / kpi["previous"] * 100)
    assert 0 <= kpis["on_time_pct"]["value"] <= 100


def test_thirteen_weeks_has_no_fabricated_previous_period(client):
    # The history spans 90 days, so 13 weeks has no full previous period.
    for kpi in get(client, 13)["kpis"].values():
        assert kpi["previous"] is None and kpi["delta"] is None and kpi["delta_pct"] is None


def test_recent_shipments_match_dashboard(client):
    dashboard = client.get("/api/dashboard", headers=BROKER).json()
    ids = {d["shipment"]["id"] for d in dashboard["shipments"]}
    recent = get(client)["recent_shipments"]
    assert recent and {r["id"] for r in recent} <= ids
    assert all(r["id"].startswith("SCT-") for r in recent)
    by_id = {d["shipment"]["id"]: d["shipment"] for d in dashboard["shipments"]}
    for row in recent:
        source = by_id[row["id"]]
        assert row["origin_id"] == source["origin_id"]
        assert row["destination_id"] == source["destination_id"]
        assert row["mass_tonnes"] == pytest.approx(source["mass_kg"] / 1000)
        assert row["origin_name"] and row["destination_name"]


def test_mode_split_sums_to_100(client):
    split = get(client)["mode_split"]
    assert {m["mode"] for m in split} == {"sea", "road", "rail"}
    assert sum(m["share_pct"] for m in split) == pytest.approx(100)
    assert all(m["tonne_km"] >= 0 for m in split)


def test_all_numbers_finite(client):
    for weeks in (4, 13):
        assert all(math.isfinite(n) for n in numbers(get(client, weeks)))


def test_closure_does_not_treat_unknown_route_as_zero_emission_cargo(client):
    path = "/api/shipments/SCT-001/scenario"
    try:
        response = client.post(path, json={"kind": "closure"}, headers=BROKER)
        assert response.status_code == 200
        assert response.json()["current_route"]["legs"] == []
        details = client.get("/api/dashboard", headers=BROKER).json()["shipments"]
        scheduled = [d for d in details if d["current_route"]["legs"]]
        # Missed transfers still have a scheduled recovery with real distances.
        assert any(d["current_route"]["missed_connection"] for d in scheduled)
        expected = sum(d["current_route"]["co2_kg"] for d in scheduled) / sum(
            d["shipment"]["mass_kg"] / 1000 for d in scheduled)
        body = get(client)
        assert body["co2e_intensity_kg_per_tonne"] == pytest.approx(expected)
        assert body["kpis"]["co2e_tonnes"]["value"] == pytest.approx(
            sum(w["tonnes"] for w in body["weekly"]) * expected / 1000)
    finally:
        client.post(path + "/reset", headers=BROKER)


def test_intensity_without_scheduled_routes_is_finite(client):
    from backend.app.schemas import ShipmentDetail
    from backend.app.summary import _intensity

    path = "/api/shipments/SCT-001/scenario"
    try:
        response = client.post(path, json={"kind": "closure"}, headers=BROKER)
        assert response.status_code == 200
        unavailable = ShipmentDetail.model_validate(response.json())
        assert not unavailable.current_route.legs
        assert _intensity([unavailable]) == 0.0
        assert _intensity([]) == 0.0
    finally:
        client.post(path + "/reset", headers=BROKER)
