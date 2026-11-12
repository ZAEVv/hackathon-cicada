"""Corridor summary computed from the seeded synthetic history and current demo shipments.

Everything here is synthetic. Two sources are combined:

* the seeded history rows (``data.load_demo_data()["history"]``) drive the weekly
  series and the period KPIs (count, on-time share, average delay, tonnage);
* the current demo shipments (``ShipmentDetail`` objects the dashboard already
  computes) drive the mode split (tonne-km of the current routes), the CO2e
  intensity and the "recent shipments" list.

CO2e approximation: history rows carry no route, so their emissions are
``mass_tonnes * intensity``, where ``intensity`` (kg CO2e per tonne) is the
mass-weighted average of the current routes: ``sum(route CO2e) / sum(shipment
tonnes)``. Route CO2e itself uses the ``esg.py`` factors (demo-v1, transport-only,
remaining legs). The history is therefore valued as if it had travelled the current
mix of corridors; it is an illustration, not a measurement.
"""
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import esg
from .schemas import SIMULATION_NOW, CorridorSummary, ShipmentDetail

WEEK = timedelta(days=7)
RECENT_LIMIT = 10
# Declaration statuses that mean documents no longer block the shipment.
DOCS_OK = frozenset({"ready", "submitted", "demo_accepted", "auto_accepted"})
METHODOLOGY = (
    "Периоды считаются неделями назад от SIMULATION_NOW. Дельта сравнивается с предыдущим "
    "равным периодом; если история его не покрывает, дельта не показывается. "
    "Вовремя = без пропущенной стыковки. CO2e истории = масса × средняя интенсивность "
    f"текущих маршрутов (факторы esg.py, {esg.FACTOR_VERSION}). Доли видов транспорта "
    "считаются по т·км текущих маршрутов."
)
DISCLAIMER = "Синтетическая история, не операционные данные. " + esg.DISCLAIMER


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _kpi(value: float, previous: float | None) -> dict:
    if previous is None:
        return {"value": value, "previous": None, "delta": None, "delta_pct": None}
    return {"value": value, "previous": previous, "delta": value - previous,
            "delta_pct": (value - previous) / previous * 100 if previous else None}


def _stats(rows: list[tuple[datetime, dict]]) -> dict[str, float]:
    count = len(rows)
    return {
        "count": count,
        "tonnes": sum(r["mass_tonnes"] for _, r in rows),
        "on_time": (100.0 * sum(1 - int(r["missed_connection"]) for _, r in rows) / count) if count else 0.0,
        "delay": (sum(r["delay_hours"] for _, r in rows) / count) if count else 0.0,
    }


def _intensity(details: list[ShipmentDetail]) -> float:
    tonnes = sum(d.shipment.mass_kg for d in details) / 1000
    co2 = sum(d.current_route.co2_kg for d in details)
    return co2 / tonnes if tonnes > 0 else 0.0


def _mode_split(details: list[ShipmentDetail]) -> list[dict]:
    tonne_km = {"sea": 0.0, "road": 0.0, "rail": 0.0}
    for d in details:
        tonnes = d.shipment.mass_kg / 1000
        for leg in d.current_route.legs:
            tonne_km[leg.mode] += tonnes * leg.distance_km
    total = sum(tonne_km.values())
    return [{"mode": m, "tonne_km": v, "share_pct": v / total * 100 if total else 0.0}
            for m, v in tonne_km.items()]


def _status(d: ShipmentDetail) -> str:
    # Same rule as Dashboard.summary.high_risk: an unavailable route is at risk too.
    if not d.current_route.feasible or d.prediction.risk == "high":
        return "at_risk"
    declaration = d.preclearance
    if declaration is None or declaration.status not in DOCS_OK:
        return "docs_pending"
    return "attention" if d.prediction.risk == "medium" else "in_transit"


def build_summary(history: Iterable[dict[str, Any]], details: list[ShipmentDetail],
                  ports: list[dict[str, Any]], weeks: int) -> CorridorSummary:
    """Build the summary for ``weeks`` (4 or 13) ending at SIMULATION_NOW."""
    now = _parse(SIMULATION_NOW)
    rows = [(_parse(r["observed_at"]), r) for r in history]
    history_start = min((t for t, _ in rows), default=now)
    start = now - weeks * WEEK
    prev_start = start - weeks * WEEK

    current = [(t, r) for t, r in rows if start <= t < now]
    # The previous period is only comparable if the history covers all of it.
    previous = [(t, r) for t, r in rows if prev_start <= t < start] if prev_start >= history_start else None

    cur = _stats(current)
    prev = _stats(previous) if previous is not None else None
    intensity = _intensity(details)

    def co2(stats: dict | None) -> float | None:
        return None if stats is None else stats["tonnes"] * intensity / 1000

    def pick(stats: dict | None, key: str) -> float | None:
        return None if stats is None else stats[key]

    weekly = []
    for i in range(weeks):
        lo = start + i * WEEK
        bin_rows = [r for t, r in current if lo <= t < lo + WEEK]
        weekly.append({"week_start": lo.date().isoformat(), "shipments": len(bin_rows),
                       "tonnes": sum(r["mass_tonnes"] for r in bin_rows)})

    names = {port["id"]: port["name"] for port in ports}
    recent = [{"id": d.shipment.id, "origin_id": d.shipment.origin_id,
               "origin_name": names.get(d.shipment.origin_id, d.shipment.origin_id),
               "destination_id": d.shipment.destination_id,
               "destination_name": names.get(d.shipment.destination_id, d.shipment.destination_id),
               "mass_tonnes": d.shipment.mass_kg / 1000, "status": _status(d)}
              for d in sorted(details, key=lambda d: d.shipment.id)[:RECENT_LIMIT]]

    return CorridorSummary(
        weeks=weeks, period_start=start.date().isoformat(), period_end=now.date().isoformat(),
        weekly=weekly,
        kpis={"shipments": _kpi(cur["count"], pick(prev, "count")),
              "on_time_pct": _kpi(cur["on_time"], pick(prev, "on_time")),
              "avg_delay_hours": _kpi(cur["delay"], pick(prev, "delay")),
              "co2e_tonnes": _kpi(co2(cur), co2(prev))},
        mode_split=_mode_split(details), recent_shipments=recent,
        co2e_intensity_kg_per_tonne=intensity, methodology=METHODOLOGY, disclaimer=DISCLAIMER)
