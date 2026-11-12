"""Deterministic scheduling of the remaining simulated journey."""
from datetime import datetime, timedelta, timezone
from itertools import product

import networkx as nx

from .esg import calculate_emissions


def scenario_profile(kind: str) -> dict:
    if kind not in {"none", "storm", "congestion", "closure"}:
        raise ValueError(f"Unknown scenario: {kind}")
    return {
        "kind": kind,
        "arrival_delay_hours": 3 if kind == "storm" else 0,
        "queue_extra_hours": 2 if kind == "storm" else 6 if kind == "congestion" else 0,
        "wind_mps": 22 if kind == "storm" else None,
        "blocked_service_ids": ["sea-main"] if kind == "closure" else [],
    }


def _time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Datetime must have a timezone")
    return dt.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _schedule(path, ready, now, shipment, blocked):
    legs = []
    for index, service in enumerate(path):
        if (service["id"] in blocked or service["capacity_tonnes"] < shipment["mass_kg"] / 1000
                or shipment["cargo"] not in service["allowed_cargo"]):
            return None
        available = ready + timedelta(hours=service["handling_hours"] if index else 0)
        departure = next((d for d in sorted(map(_time, service["departures"]))
                          if d >= now and available <= d - timedelta(hours=service["cutoff_hours"])), None)
        if departure is None:
            return None
        ready = departure + timedelta(hours=service["duration_hours"])
        legs.append({"service_id": service["id"], "origin_id": service["origin_id"],
                     "destination_id": service["destination_id"], "mode": service["mode"],
                     "departure_at": _iso(departure), "arrival_at": _iso(ready),
                     "cutoff_at": _iso(departure - timedelta(hours=service["cutoff_hours"])),
                     "distance_km": service["distance_km"], "cost_usd": service["cost_usd"]})
    return legs


def _route(legs, mass, label, *, feasible=True, reason=None, missed=False, empty_id="unavailable"):
    legs = [dict(leg) for leg in legs]
    emissions = calculate_emissions(legs, mass)
    for leg, co2 in zip(legs, emissions["by_leg_kg"]):
        leg["co2_kg"] = co2
    return {"id": "+".join(f'{leg["service_id"]}@{leg["departure_at"]}' for leg in legs) or empty_id,
            "label": label, "feasible": feasible, "reason": reason,
            "missed_connection": missed, "legs": legs,
            "eta": legs[-1]["arrival_at"] if legs else None,
            "cost_usd": sum(leg["cost_usd"] for leg in legs),
            "co2_kg": emissions["total_kg"], "emissions": emissions}


def _planned_path(shipment: dict, services: list[dict]) -> list[dict]:
    """Remaining originally planned services, or [] if they do not form a path."""
    by_id = {service["id"]: service for service in services}
    origin, destination = shipment["current_location_id"], shipment["destination_id"]
    planned = [by_id[sid] for sid in shipment["planned_service_ids"]]
    # The shipment may be inbound on a preceding leg: start only at its next handling location.
    start = next((i for i, service in enumerate(planned) if service["origin_id"] == origin), len(planned))
    original = planned[start:]
    if (not original or original[-1]["destination_id"] != destination or
            any(a["destination_id"] != b["origin_id"] for a, b in zip(original, original[1:]))):
        return []
    return original


def _planned_ready(shipment: dict) -> datetime:
    # Planned readiness excludes forecast and scenario delays; initial handling is counted once.
    return max(_time(shipment["planned_port_arrival"]) + timedelta(
        hours=shipment["queue_hours"] + 1), _time(shipment["documents_ready_at"]))


def planned_cutoff(shipment: dict, services: list[dict],
                   now: str = "2026-11-12T08:00:00Z") -> str | None:
    """Cutoff of the first originally planned departure, or None without one.

    Uses the same baseline as ``compute_routes`` so the forecast's miss
    probability and the route's ``missed_connection`` refer to one departure.
    Pass the original shipment (plan values), not a scenario-adjusted copy.
    """
    original = _planned_path(shipment, services)
    baseline = _schedule(original, _planned_ready(shipment), _time(now), shipment, set()) if original else None
    return baseline[0]["cutoff_at"] if baseline else None


def compute_routes(shipment: dict, ports: list[dict], services: list[dict],
                   prediction: dict, scenario: dict,
                   now: str = "2026-11-12T08:00:00Z") -> dict:
    now_dt = _time(now)
    ready = _time(prediction["ready_at"])
    origin, destination = shipment["current_location_id"], shipment["destination_id"]
    port_ids = {port["id"] for port in ports}
    if origin not in port_ids or destination not in port_ids:
        raise KeyError(origin if origin not in port_ids else destination)
    original = _planned_path(shipment, services)
    blocked = set(scenario["blocked_service_ids"])
    planned_ready = _planned_ready(shipment)
    baseline = _schedule(original, planned_ready, now_dt, shipment, set()) if original else None
    actual = _schedule(original, ready, now_dt, shipment, blocked) if original else None
    unblocked = _schedule(original, ready, now_dt, shipment, set()) if original else None
    missed = bool(baseline and (unblocked is None or any(
        a["departure_at"] != b["departure_at"] for a, b in zip(unblocked, baseline))))
    if actual is None:
        current = _route([], shipment["mass_kg"], "Planned route", feasible=False,
                         reason="Planned route is unavailable under current conditions.",
                         missed=missed, empty_id="unavailable:" + "+".join(s["id"] for s in original))
    else:
        current = _route(actual, shipment["mass_kg"], "Planned route", feasible=not missed,
                         reason="Planned connection missed; showing earliest recovery." if missed else None,
                         missed=missed)

    graph = nx.DiGraph()
    graph.add_nodes_from(port_ids)
    for service in services:
        graph.add_edge(service["origin_id"], service["destination_id"])
    choices = {}
    if missed and actual is not None:
        recovery = _route(actual, shipment["mass_kg"], "Next-departure recovery")
        choices[recovery["id"]] = recovery
    if origin != destination and nx.has_path(graph, origin, destination):
        for nodes in nx.all_simple_paths(graph, origin, destination):
            groups = [[s for s in services if s["origin_id"] == a and s["destination_id"] == b]
                      for a, b in zip(nodes, nodes[1:])]
            for path in product(*groups):
                legs = _schedule(path, ready, now_dt, shipment, blocked)
                if legs is None:
                    continue
                route = _route(legs, shipment["mass_kg"], "Alternative route")
                if route["id"] != current["id"] or not current["feasible"]:
                    choices.setdefault(route["id"], route)
    alternatives = sorted(choices.values(), key=lambda r: (r["eta"], r["cost_usd"], r["id"]))[:3]
    return {"current_route": current, "alternatives": alternatives}
