"""72-hour hub load / bottleneck forecast for every catalog node (synthetic).

For each port or terminal the module produces an hourly series from the frozen
clock to +72 h: handling demand versus handling capacity (utilization), the
expected wait and the bottleneck windows (utilization >= 0.85).

Demand at a node is the sum of

* a seeded *synthetic background* load with a diurnal profile, so the picture
  is not driven by a handful of demo shipments. It is a deterministic function
  of (SEED, port id) and is labelled synthetic in the response;
* the demo shipments: every shipment's *current route* (as assembled by
  ``main.detail()``; routing is not duplicated here) yields a visit to each
  node on it. Cargo is handled after arrival and before departure and occupies
  the yard in between. A shipment without a feasible route is held at its
  current node until the end of the horizon.

A fluid queue carries unserved work forward: ``backlog += demand - capacity``.
Utilization is ``(demand + carried backlog / BACKLOG_WORKOFF_HOURS) / capacity``
(clamped), the wait is
``backlog / capacity`` plus a congestion term that grows near saturation plus
any scenario queue extension.

Scenario design. Scenarios are stored per shipment, therefore:

1. each shipment's own scenario already shaped its own route in ``detail()``
   (delayed arrival, later sailing, blocked service -> held or re-routed), so
   it reaches the forecast through the route legs;
2. the scenario of the *requested* shipment (``?shipment_id=``) is additionally
   applied as the regional condition, consistent with
   ``routing.scenario_profile``:
   - storm (``wind_mps``): berth capacity at the regional node drops with the
     wind while the storm lasts, background arrivals slip by
     ``arrival_delay_hours`` and the wait grows by ``queue_extra_hours``;
   - congestion (``queue_extra_hours`` without wind): extra background demand
     and a longer wait at the regional node;
   - closure (``blocked_service_ids``): the origin of a blocked service loses
     its sea-side capacity, so cargo accumulates there; part of its background
     flow shifts to the destinations of the remaining services from that node
     and the blocked service's destination receives less.
   Storm and congestion are modelled locally at ``REGIONAL_NODE`` (Makhachkala)
   with a fallback to the requested shipment's current node for catalogs
   without it.

Capacities and background levels are demo-scale parameters keyed by port id,
with defaults by node kind for ids this module has never seen.
"""
from datetime import datetime, timedelta, timezone
from math import cos, isfinite, pi
from random import Random

SEED = 42
HORIZON_HOURS = 72
STEP_HOURS = 1
BOTTLENECK_THRESHOLD = 0.85
BUSY_THRESHOLD = 0.7
MAX_UTILIZATION = 2.5
MAX_WAIT_HOURS = 72.0
REGIONAL_NODE = "makhachkala"

# Handling capacity (tonnes/hour) and mean background utilization. Demo scale:
# small enough that a single 20 t consignment is visible on the curve.
NODE_PARAMS: dict[str, dict[str, float]] = {
    "rail_hub": {"capacity_tph": 70.0, "base_utilization": 0.50},
    "makhachkala": {"capacity_tph": 42.0, "base_utilization": 0.52},
    "olya": {"capacity_tph": 30.0, "base_utilization": 0.46},
    "anzali": {"capacity_tph": 45.0, "base_utilization": 0.52},
    "tehran": {"capacity_tph": 60.0, "base_utilization": 0.46},
    "aktau": {"capacity_tph": 50.0, "base_utilization": 0.52},
    "turkmenbashi": {"capacity_tph": 48.0, "base_utilization": 0.50},
    "amirabad": {"capacity_tph": 40.0, "base_utilization": 0.50},
}
KIND_DEFAULTS: dict[str, dict[str, float]] = {
    "terminal": {"capacity_tph": 60.0, "base_utilization": 0.50},
    "port": {"capacity_tph": 40.0, "base_utilization": 0.50},
    "destination": {"capacity_tph": 55.0, "base_utilization": 0.46},
}
DEFAULT_PARAMS = {"capacity_tph": 40.0, "base_utilization": 0.50}

HANDLING_WINDOW_HOURS = 4.0   # a consignment is (un)loaded over this window
BACKLOG_WORKOFF_HOURS = 4.0   # carried backlog is planned over this many hours
YARD_SHARE_PER_HOUR = 0.03    # share of the mass re-handled per hour of dwell
FINAL_DWELL_HOURS = 3.0       # unloading at the last node of a route
MERGE_GAP_HOURS = 2           # windows closer than this are reported as one

STORM_HOURS = 18              # full storm effect, then linear recovery
STORM_RECOVERY_HOURS = 6
CONGESTION_HOURS = 30
CONGESTION_DECAY_HOURS = 24
CONGESTION_DEMAND_PER_QUEUE_HOUR = 0.06
CLOSURE_CAPACITY_FACTOR = 0.5  # landside keeps working, the berth does not
CLOSURE_DIVERTED_SHARE = 0.3
CLOSURE_DESTINATION_FACTOR = 0.85

R_BACKGROUND = "Background peak load"
R_STORM = "Storm limits berth operations"
R_CONGESTION = "Scenario congestion at terminal"
R_CLOSURE = "Sea service closed: cargo accumulates"
R_DIVERTED = "Diverted flow from closed service"
R_SHIPMENTS = "Demo shipments handled in this window"
R_HELD = "Shipment held: route unavailable"
R_BACKLOG = "Backlog carried over"

DISCLAIMER = ("Synthetic forecast: seeded background load and demo capacities; "
              "not real port statistics.")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Datetime must have a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def node_params(port: dict) -> dict[str, float]:
    """Capacity parameters for a port; unknown ids fall back to their kind."""
    params = NODE_PARAMS.get(port.get("id")) or KIND_DEFAULTS.get(port.get("kind")) or DEFAULT_PARAMS
    return dict(params)


def background_profile(port_id: str, now: datetime, seed: int = SEED) -> list[float]:
    """Seeded diurnal multipliers (mean about 1) for hours 0..HORIZON_HOURS.

    A str seed makes ``random.Random`` hash with SHA-512, so the series is the
    same in every process (unlike the salted built-in ``hash``).
    """
    rng = Random(f"hubs:{seed}:{port_id}")
    peak_hour = rng.uniform(10.0, 16.0)       # UTC hour of the daily peak
    amplitude = rng.uniform(0.24, 0.34)
    second = rng.uniform(0.04, 0.10)
    level = rng.uniform(0.96, 1.04)
    values = []
    for hour in range(HORIZON_HOURS + 1):
        clock = (now + timedelta(hours=hour)).hour + (now.minute / 60.0)
        day = cos(2 * pi * (clock - peak_hour) / 24.0)
        half = cos(4 * pi * (clock - peak_hour - 3.0) / 24.0)
        noise = rng.uniform(-0.04, 0.04)
        values.append(max(0.2, level * (1.0 + amplitude * day + second * half) + noise))
    return values


def _visits(detail: dict, now: datetime, end: datetime) -> list[dict]:
    """Node visits of one shipment along its current route within the horizon."""
    shipment, route, scenario = detail["shipment"], detail["current_route"], detail["scenario"]
    mass = max(0.0, float(shipment["mass_kg"]) / 1000.0)
    arrival = _time(shipment["planned_port_arrival"]) + timedelta(
        hours=float(shipment.get("observed_delay_hours") or 0) + float(scenario.get("arrival_delay_hours") or 0))
    legs = route.get("legs") or []
    visits = []
    if not legs:
        # No feasible route: the cargo stays where it is for the whole horizon.
        visits.append({"port_id": shipment["current_location_id"], "arrive": arrival,
                       "depart": None, "held": True})
    else:
        visits.append({"port_id": legs[0]["origin_id"], "arrive": arrival,
                       "depart": _time(legs[0]["departure_at"]), "held": False})
        for leg, following in zip(legs, legs[1:]):
            visits.append({"port_id": leg["destination_id"], "arrive": _time(leg["arrival_at"]),
                           "depart": _time(following["departure_at"]), "held": False})
        last = _time(legs[-1]["arrival_at"])
        visits.append({"port_id": legs[-1]["destination_id"], "arrive": last,
                       "depart": None, "held": False, "final": True})
    result = []
    for visit in visits:
        final = visit.pop("final", False)
        start = max(visit["arrive"], now)
        if visit["depart"] is None:
            stop = end if visit["held"] else visit["arrive"] + timedelta(hours=FINAL_DWELL_HOURS)
        else:
            stop = max(visit["depart"], start)
        if stop <= now or start >= end:
            continue
        result.append({**visit, "shipment_id": shipment["id"], "mass_t": mass, "final": final,
                       "start": start, "stop": min(stop, end)})
    return result


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _visit_demand(visit: dict, now: datetime) -> list[float]:
    """Tonnes handled per hourly slot for one visit."""
    start = (visit["start"] - now).total_seconds() / 3600.0
    stop = (visit["stop"] - now).total_seconds() / 3600.0
    mass = visit["mass_t"]
    rate = mass / HANDLING_WINDOW_HOURS
    windows = []
    if visit["arrive"] >= now:                      # unloading after arrival
        windows.append((start, start + HANDLING_WINDOW_HOURS))
    if visit["depart"] is not None:                 # loading before departure
        windows.append((max(start, stop - HANDLING_WINDOW_HOURS), max(stop, start + 1e-9)))
    demand = []
    for hour in range(HORIZON_HOURS + 1):
        slot = (float(hour), float(hour + 1))
        value = sum(rate * _overlap(*slot, *window) for window in windows)
        value += mass * YARD_SHARE_PER_HOUR * _overlap(*slot, start, max(stop, start))
        demand.append(value)
    return demand


def _ramp(hour: int, full: int, decay: int) -> float:
    """1 while the effect lasts, then linearly down to 0."""
    if hour <= full:
        return 1.0
    return max(0.0, 1.0 - (hour - full) / decay) if decay else 0.0


def _regional_effects(ports: list[dict], services: list[dict], scenario: dict,
                      fallback_node: str | None) -> dict[str, dict]:
    """Per-node hourly modifiers implied by the regional scenario."""
    port_ids = [port["id"] for port in ports]
    hours = range(HORIZON_HOURS + 1)
    effects = {pid: {"capacity": [1.0] * len(hours), "demand": [1.0] * len(hours),
                     "extra_tph": [0.0] * len(hours), "wait": [0.0] * len(hours),
                     "shift": 0, "reasons": [[] for _ in hours]} for pid in port_ids}
    node = REGIONAL_NODE if REGIONAL_NODE in effects else fallback_node
    queue_extra = float(scenario.get("queue_extra_hours") or 0)
    wind = scenario.get("wind_mps")
    if node in effects and wind is not None:
        # 22 m/s costs about 40% of berth productivity; up to 10 m/s costs none.
        loss = min(0.6, max(0.0, (float(wind) - 10.0) / 30.0))
        effect = effects[node]
        effect["shift"] = int(round(float(scenario.get("arrival_delay_hours") or 0)))
        for hour in hours:
            strength = _ramp(hour, STORM_HOURS, STORM_RECOVERY_HOURS)
            if strength > 0:
                effect["capacity"][hour] *= 1.0 - loss * strength
                effect["wait"][hour] += queue_extra * strength
                effect["reasons"][hour].append(R_STORM)
    elif node in effects and queue_extra > 0:
        effect = effects[node]
        for hour in hours:
            strength = _ramp(hour, CONGESTION_HOURS, CONGESTION_DECAY_HOURS)
            if strength > 0:
                effect["demand"][hour] *= 1.0 + CONGESTION_DEMAND_PER_QUEUE_HOUR * queue_extra * strength
                effect["wait"][hour] += queue_extra * strength
                effect["reasons"][hour].append(R_CONGESTION)
    blocked = set(scenario.get("blocked_service_ids") or [])
    for service in services:
        if service["id"] not in blocked or service["origin_id"] not in effects:
            continue
        origin = service["origin_id"]
        for hour in hours:
            effects[origin]["capacity"][hour] *= CLOSURE_CAPACITY_FACTOR
            effects[origin]["demand"][hour] *= 1.0 - CLOSURE_DIVERTED_SHARE
            effects[origin]["reasons"][hour].append(R_CLOSURE)
        destination = service["destination_id"]
        if destination in effects and destination != origin:
            for hour in hours:
                effects[destination]["demand"][hour] *= CLOSURE_DESTINATION_FACTOR
        alternatives = sorted({s["destination_id"] for s in services
                               if s["origin_id"] == origin and s["id"] not in blocked
                               and s["destination_id"] in effects
                               and s["destination_id"] not in (origin, destination)})
        for alternative in alternatives:
            effects[alternative]["diverted_from"] = origin
            effects[alternative]["diverted_share"] = CLOSURE_DIVERTED_SHARE / len(alternatives)
    return effects


def _windows(port_id: str, points: list[dict], reasons: list[list[str]], visits: list[dict],
             now: datetime) -> list[dict]:
    hot = [i for i, point in enumerate(points) if point["utilization"] >= BOTTLENECK_THRESHOLD]
    groups: list[list[int]] = []
    for index in hot:
        if groups and index - groups[-1][-1] <= MERGE_GAP_HOURS:
            groups[-1].append(index)
        else:
            groups.append([index])
    windows = []
    for group in groups:
        first, last = group[0], group[-1]
        peak = max(group, key=lambda i: (points[i]["utilization"], -i))
        start = now + timedelta(hours=first)
        stop = now + timedelta(hours=min(last + 1, HORIZON_HOURS))
        codes: list[str] = []
        for index in group:
            for code in reasons[index]:
                if code not in codes:
                    codes.append(code)
        present = [v for v in visits if v["start"] < max(stop, start + timedelta(hours=1)) and v["stop"] > start]
        if not codes or codes == [R_BACKLOG]:
            codes.insert(0, R_BACKGROUND)
        if any(v["held"] for v in present):
            codes.append(R_HELD)
        if any(not v["held"] for v in present):
            codes.append(R_SHIPMENTS)
        peak_value = points[peak]["utilization"]
        windows.append({
            "port_id": port_id, "start_at": _iso(start), "end_at": _iso(stop),
            "start_offset_hours": first, "end_offset_hours": min(last + 1, HORIZON_HOURS),
            "peak_at": points[peak]["at"], "peak_utilization": peak_value,
            "max_queue_hours": max(points[i]["queue_hours"] for i in group),
            "severity": "high" if peak_value >= 1.0 else "medium",
            "reasons": codes,
            "shipment_ids": sorted({v["shipment_id"] for v in present}),
        })
    return windows


def forecast(ports: list[dict], services: list[dict], details: list[dict],
             regional_scenario: dict | None = None, now: str = "2026-11-12T08:00:00Z",
             *, focus_shipment_id: str | None = None, seed: int = SEED) -> dict:
    """Hourly load forecast for every port in ``ports``.

    ``details`` are ``ShipmentDetail`` dumps (each already reflects its own
    scenario and selected route). ``regional_scenario`` is a
    ``routing.scenario_profile`` dict applied to node capacity and background.
    """
    now_dt = _time(now)
    # The last point describes the hour starting at +72 h.
    end = now_dt + timedelta(hours=HORIZON_HOURS + STEP_HOURS)
    scenario = regional_scenario or {"kind": "none"}
    focus = next((d for d in details if d["shipment"]["id"] == focus_shipment_id), None)
    fallback_node = focus["shipment"]["current_location_id"] if focus else None
    effects = _regional_effects(ports, services, scenario, fallback_node)

    visits_by_port: dict[str, list[dict]] = {}
    for detail in details:
        for visit in _visits(detail, now_dt, end):
            visits_by_port.setdefault(visit["port_id"], []).append(visit)

    params = {port["id"]: node_params(port) for port in ports}
    profiles = {port["id"]: background_profile(port["id"], now_dt, seed) for port in ports}

    def background_tph(port_id: str, hour: int, shift: int = 0) -> float:
        index = min(HORIZON_HOURS, max(0, hour - shift))
        return params[port_id]["capacity_tph"] * params[port_id]["base_utilization"] * profiles[port_id][index]

    hubs, bottlenecks = [], []
    for port in ports:
        port_id = port["id"]
        effect = effects[port_id]
        nominal = params[port_id]["capacity_tph"]
        visits = sorted(visits_by_port.get(port_id, []), key=lambda v: (v["start"], v["shipment_id"]))
        shipment_demand = [_visit_demand(visit, now_dt) for visit in visits]
        diverted_from = effect.get("diverted_from")
        backlog = 0.0
        points, reasons = [], []
        for hour in range(HORIZON_HOURS + 1):
            at = now_dt + timedelta(hours=hour)
            capacity = max(1e-6, nominal * effect["capacity"][hour])
            background = background_tph(port_id, hour, effect["shift"]) * effect["demand"][hour]
            diverted = (background_tph(diverted_from, hour) * effect["diverted_share"]
                        if diverted_from else 0.0)
            cargo = sum(series[hour] for series in shipment_demand)
            demand = background + diverted + cargo
            carried = backlog
            utilization = min(MAX_UTILIZATION, (demand + carried / BACKLOG_WORKOFF_HOURS) / capacity)
            backlog = max(0.0, carried + demand - capacity)
            rho = min(utilization, 0.95)
            wait = backlog / capacity + 0.15 * rho * rho / (1.0 - rho) + effect["wait"][hour]
            wait = min(MAX_WAIT_HOURS, wait)
            if not (isfinite(utilization) and isfinite(wait)):
                raise ValueError("Hub forecast produced a non-finite value")
            present = sorted({v["shipment_id"] for v in visits
                              if v["start"] < at + timedelta(hours=1) and v["stop"] > at})
            codes = list(effect["reasons"][hour])
            if diverted > 0:
                codes.append(R_DIVERTED)
            if carried > 0.05 * capacity and not codes:
                codes.append(R_BACKLOG)
            reasons.append(codes)
            points.append({
                "at": _iso(at), "offset_hours": hour,
                "utilization": round(utilization, 4),
                "load_tph": round(demand, 2), "capacity_tph": round(capacity, 2),
                "background_tph": round(background + diverted, 2),
                "shipments_tph": round(cargo, 2),
                "queue_hours": round(wait, 2),
                "shipment_ids": present,
            })
        peak = max(range(len(points)), key=lambda i: (points[i]["utilization"], -i))
        peak_value = points[peak]["utilization"]
        windows = _windows(port_id, points, reasons, visits, now_dt)
        bottlenecks.extend(windows)
        hubs.append({
            "port_id": port_id, "nominal_capacity_tph": nominal,
            "peak_utilization": peak_value, "peak_at": points[peak]["at"],
            "max_queue_hours": max(point["queue_hours"] for point in points),
            "status": ("bottleneck" if peak_value >= BOTTLENECK_THRESHOLD
                       else "busy" if peak_value >= BUSY_THRESHOLD else "ok"),
            "bottleneck_count": len(windows),
            "points": points,
        })
    bottlenecks.sort(key=lambda w: (w["start_at"], -w["peak_utilization"], w["port_id"]))
    return {
        "simulation_now": _iso(now_dt), "synthetic": True,
        "horizon_hours": HORIZON_HOURS, "step_hours": STEP_HOURS,
        "bottleneck_threshold": BOTTLENECK_THRESHOLD, "busy_threshold": BUSY_THRESHOLD,
        "regional_scenario": scenario.get("kind", "none"),
        "focus_shipment_id": focus_shipment_id,
        "hubs": hubs, "bottlenecks": bottlenecks,
        "background_source": "synthetic-seeded-diurnal",
        "disclaimer": DISCLAIMER,
    }
