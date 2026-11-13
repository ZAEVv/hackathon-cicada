from copy import deepcopy

import pytest

from backend.app.routing import compute_routes, scenario_profile
from backend.app.schemas import Route, Scenario


def service(id, origin, destination, departures, **overrides):
    result = dict(id=id, origin_id=origin, destination_id=destination, mode="sea",
                  distance_km=100, duration_hours=2, departures=departures,
                  cutoff_hours=1, handling_hours=1, capacity_tonnes=20,
                  cost_usd=100, allowed_cargo=["general"])
    result.update(overrides)
    return result


@pytest.fixture
def inputs():
    shipment = dict(id="s", current_location_id="a", destination_id="c", cargo="general",
                    mass_kg=20000, planned_service_ids=["inbound", "main", "final"],
                    planned_port_arrival="2026-11-12T10:00:00Z", queue_hours=0,
                    documents_ready_at="2026-11-12T10:00:00Z")
    ports = [dict(id=x) for x in "xabc"]
    services = [
        service("inbound", "x", "a", ["2026-11-12T08:00:00Z"]),
        service("main", "a", "b", ["2026-11-12T12:00:00Z", "2026-11-13T12:00:00Z"]),
        service("final", "b", "c", ["2026-11-12T15:00:00Z", "2026-11-12T16:00:00Z", "2026-11-13T16:00:00Z"], cutoff_hours=0),
        service("divert", "a", "c", ["2026-11-12T14:00:00Z"], duration_hours=5),
    ]
    return shipment, ports, services, dict(ready_at="2026-11-12T11:00:00Z"), scenario_profile("none")


def test_cutoff_inclusive_and_no_initial_double_handling(inputs):
    result = compute_routes(*inputs)
    route = result["current_route"]
    Route(**route)
    assert route["feasible"] and not route["missed_connection"]
    assert [leg["service_id"] for leg in route["legs"]] == ["main", "final"]
    assert route["legs"][0]["departure_at"] == "2026-11-12T12:00:00Z"
    assert route["legs"][1]["departure_at"] == "2026-11-12T15:00:00Z"
    assert route["cost_usd"] == sum(leg["cost_usd"] for leg in route["legs"])
    assert route["co2_kg"] == sum(leg["co2_kg"] for leg in route["legs"])


def test_missed_cutoff_recovery_and_stable_ids(inputs):
    inputs[3]["ready_at"] = "2026-11-12T11:00:01Z"
    original = deepcopy(inputs)
    result = compute_routes(*inputs)
    assert inputs == original
    current = result["current_route"]
    assert not current["feasible"] and current["missed_connection"]
    assert current["legs"][0]["departure_at"] == "2026-11-13T12:00:00Z"
    recovery = next(r for r in result["alternatives"] if r["id"] == current["id"])
    assert recovery["feasible"] and not recovery["missed_connection"]
    assert any(r["legs"][0]["service_id"] == "divert" for r in result["alternatives"])
    assert result == compute_routes(*inputs)


def test_block_capacity_cargo_and_parallel_services(inputs):
    inputs[2].append(service("parallel", "a", "b", ["2026-11-12T13:00:00Z"]))
    inputs[4]["blocked_service_ids"] = ["main", "divert"]
    result = compute_routes(*inputs)
    assert not result["current_route"]["feasible"]
    assert result["current_route"]["legs"] == []
    assert result["alternatives"][0]["legs"][0]["service_id"] == "parallel"
    inputs[2][-1]["capacity_tonnes"] = 19.999
    assert compute_routes(*inputs)["alternatives"] == []
    inputs[2][-1]["capacity_tonnes"] = 20
    inputs[2][-1]["allowed_cargo"] = ["other"]
    assert compute_routes(*inputs)["alternatives"] == []


def test_transfer_handling_and_next_sailing(inputs):
    inputs[2][2]["handling_hours"] = 2
    result = compute_routes(*inputs)
    assert result["current_route"]["legs"][1]["departure_at"] == "2026-11-12T16:00:00Z"
    inputs[2][2]["departures"] = ["2026-11-13T16:00:00Z"]
    assert compute_routes(*inputs)["current_route"]["legs"][1]["departure_at"] == "2026-11-13T16:00:00Z"


def test_profiles():
    for kind in ("none", "storm", "congestion", "closure"):
        Scenario(**scenario_profile(kind))
    assert scenario_profile("storm")["arrival_delay_hours"] == 3
    assert scenario_profile("storm")["queue_extra_hours"] == 2
    assert scenario_profile("congestion")["queue_extra_hours"] == 6
    assert scenario_profile("closure")["blocked_service_ids"] == ["sea-main"]
    with pytest.raises(ValueError):
        scenario_profile("unknown")


def test_catalog_offers_eastern_alternatives_for_every_demo_shipment():
    import time

    from backend.app.data import SIMULATION_NOW, load_demo_data

    catalog = load_demo_data()
    started = time.monotonic()
    for shipment in catalog["shipments"]:
        result = compute_routes(shipment, catalog["ports"], catalog["services"],
                                {"ready_at": shipment["documents_ready_at"]},
                                scenario_profile("none"), SIMULATION_NOW)
        assert result["current_route"]["legs"]
        Route.model_validate(result["current_route"])
    assert time.monotonic() - started < 5

    sct004 = next(s for s in catalog["shipments"] if s["id"] == "SCT-004")
    result = compute_routes(sct004, catalog["ports"], catalog["services"],
                            {"ready_at": sct004["documents_ready_at"]}, scenario_profile("none"),
                            SIMULATION_NOW)
    used = {leg["service_id"] for route in result["alternatives"] for leg in route["legs"]}
    assert used & {"rail-kz-tm", "sea-tkm", "sea-aktau"}


def test_closure_reroutes_makhachkala_shipment_via_amirabad():
    from backend.app.data import SIMULATION_NOW, load_demo_data

    catalog = load_demo_data()
    sct001 = next(s for s in catalog["shipments"] if s["id"] == "SCT-001")
    result = compute_routes(sct001, catalog["ports"], catalog["services"],
                            {"ready_at": "2026-11-12T12:00:00Z"}, scenario_profile("closure"),
                            SIMULATION_NOW)
    assert result["current_route"]["feasible"] is False
    assert result["alternatives"]
    east = [alt for alt in result["alternatives"]
            if [leg["service_id"] for leg in alt["legs"]] == ["sea-mak-amirabad", "rail-amirabad"]]
    assert east and east[0]["feasible"] is True
    assert east[0]["legs"][0]["destination_id"] == "amirabad"
