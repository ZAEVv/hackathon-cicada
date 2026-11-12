"""Synthetic catalog, demo shipments, seeded history and weather provider.

Everything in this module is illustrative demo data.  Service IDs, departures and
coordinates are not claims about real Caspian logistics schedules or availability.
The synthetic clock is fixed in the frozen contracts; no wall-clock time or network
I/O is used unless ``get_weather(..., use_live=True)`` is explicitly requested.

The live path (off by default, not reachable from the HTTP API, which only calls
``get_weather(port_id)`` with catalog port ids) accepts only a 200 response whose
final URL is still ``https://api.open-meteo.com/v1/forecast``, reads at most
``MAX_RESPONSE_BYTES``, and applies plausibility bounds to fetched and cached
values.  ``cache_path`` is trusted caller configuration; the cache is read only
from a small regular file and written atomically via a private temporary file.
"""

from __future__ import annotations

import copy
import json
import math
import os
import random
import stat
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

try:  # Support both ``app.data`` and a direct ``data`` import in small scripts.
    from .schemas import SEED, SIMULATION_NOW
except ImportError:  # pragma: no cover - package layout is the normal path
    from schemas import SEED, SIMULATION_NOW

# Optional Open-Meteo path.  A demo must never wait on the network, so this is
# deliberately short and every failure falls back to deterministic synthetic data.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_TIMEOUT_SECONDS = 2.5
OPEN_METEO_HOST = "api.open-meteo.com"
OPEN_METEO_PATH = "/v1/forecast"
CACHE_MAX_AGE_HOURS = 6.0
# Response bodies and cache files are tiny JSON objects; anything larger is
# treated as unavailable rather than buffered.
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CACHE_BYTES = 64 * 1024
# Plausibility bounds applied to fetched AND cached values before they reach the
# model.  Out-of-range values are treated as unavailable.
MAX_WIND_MPS = 100.0
MAX_PRECIPITATION_MM = 500.0
MAX_WAVE_HEIGHT_M = 40.0
_OBSERVED_AT_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_OBSERVED_AT_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)

_HORIZON_START = datetime(2026, 11, 12, tzinfo=timezone.utc)
_HORIZON_END = datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)
_HISTORY_DAYS = 90
_HISTORY_ROWS = 720  # eight independent rows per day, comfortably above the 600 floor

_PORTS: list[dict[str, Any]] = [
    {
        "id": "rail_hub",
        "name": "Astrakhan Rail Hub",
        "country": "RU",
        "lat": 46.35,
        "lon": 48.04,
        "kind": "terminal",
    },
    {
        "id": "makhachkala",
        "name": "Makhachkala Sea Port",
        "country": "RU",
        "lat": 42.98,
        "lon": 47.50,
        "kind": "port",
    },
    {
        "id": "olya",
        "name": "Olya River-Sea Port",
        "country": "RU",
        "lat": 45.78,
        "lon": 47.55,
        "kind": "port",
    },
    {
        "id": "anzali",
        "name": "Anzali Port",
        "country": "IR",
        "lat": 37.47,
        "lon": 49.46,
        "kind": "port",
    },
    {
        "id": "tehran",
        "name": "Tehran Dry Port",
        "country": "IR",
        "lat": 35.69,
        "lon": 51.39,
        "kind": "destination",
    },
]


def _to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime | None:
    """Parse an ISO timestamp as timezone-aware UTC, returning None if invalid."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_SIMULATION_DT = _parse_utc(SIMULATION_NOW)
if _SIMULATION_DT is None:  # defensive: the contract constant must be valid
    raise RuntimeError("SIMULATION_NOW is not a valid UTC timestamp")


def _daily_departures(hour: int) -> list[str]:
    return [
        _to_utc_iso(_HORIZON_START.replace(hour=hour) + timedelta(days=day))
        for day in range((_HORIZON_END.date() - _HORIZON_START.date()).days + 1)
    ]


def _interval_departures(step_hours: int) -> list[str]:
    """Explicit recurring departures, e.g. hourly road departures."""

    step = timedelta(hours=step_hours)
    values: list[str] = []
    cursor = _HORIZON_START
    while cursor <= _HORIZON_END:
        values.append(_to_utc_iso(cursor))
        cursor += step
    return values


_SERVICES: list[dict[str, Any]] = [
    {
        "id": "rail-main",
        "origin_id": "rail_hub",
        "destination_id": "makhachkala",
        "mode": "rail",
        "distance_km": 650.0,
        "duration_hours": 26.0,
        "departures": _daily_departures(6),
        "cutoff_hours": 6.0,
        "handling_hours": 2.0,
        "capacity_tonnes": 80.0,
        "cost_usd": 1200.0,
        "allowed_cargo": ["general"],
    },
    {
        "id": "sea-main",
        "origin_id": "makhachkala",
        "destination_id": "anzali",
        "mode": "sea",
        "distance_km": 600.0,
        "duration_hours": 24.0,
        # Frozen demo anchor: first sailing 12 Nov 18:00Z, then daily 18:00Z.
        "departures": _daily_departures(18),
        "cutoff_hours": 4.0,
        "handling_hours": 1.0,
        "capacity_tonnes": 60.0,
        "cost_usd": 1000.0,
        "allowed_cargo": ["general"],
    },
    {
        "id": "transfer-olya",
        "origin_id": "makhachkala",
        "destination_id": "olya",
        "mode": "road",
        "distance_km": 350.0,
        "duration_hours": 5.0,
        # Road legs have real (explicit) slots rather than instantaneous edges.
        "departures": _interval_departures(1),
        "cutoff_hours": 0.5,
        "handling_hours": 1.0,
        "capacity_tonnes": 40.0,
        "cost_usd": 500.0,
        "allowed_cargo": ["general"],
    },
    {
        "id": "sea-olya",
        "origin_id": "olya",
        "destination_id": "anzali",
        "mode": "sea",
        "distance_km": 400.0,
        "duration_hours": 12.0,
        "departures": _daily_departures(22),
        "cutoff_hours": 2.0,
        "handling_hours": 1.0,
        "capacity_tonnes": 50.0,
        "cost_usd": 700.0,
        "allowed_cargo": ["general"],
    },
    {
        "id": "road-final",
        "origin_id": "anzali",
        "destination_id": "tehran",
        "mode": "road",
        "distance_km": 357.14285714285717,
        "duration_hours": 6.0,
        # Every six hours: the 00:00Z slot after an 18:00Z sea arrival matches
        # the illustrative contract schedule while remaining explicit.
        "departures": _interval_departures(6),
        "cutoff_hours": 0.0,
        "handling_hours": 1.0,
        "capacity_tonnes": 40.0,
        "cost_usd": 600.0,
        "allowed_cargo": ["general"],
    },
]


def _event(
    event_id: str,
    event_type: str,
    location_id: str,
    planned_at: str | None,
    *,
    estimated_at: str | None = None,
    actual_at: str | None = None,
    quality_flag: str = "forecast",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "event_type": event_type,
        "location_id": location_id,
        "planned_at": planned_at,
        "estimated_at": estimated_at,
        "actual_at": actual_at,
        "source": "synthetic",
        "received_at": SIMULATION_NOW,
        "quality_flag": quality_flag,
    }


_SHIPMENTS: list[dict[str, Any]] = [
    {
        "id": "SCT-001",
        "reference": "North-South demo 001",
        "container_id": "DEMO0000001",
        "cargo": "general",
        "mass_kg": 20000.0,
        "origin_id": "rail_hub",
        "destination_id": "tehran",
        "current_location_id": "makhachkala",
        "status": "in_transit",
        "planned_port_arrival": "2026-11-12T10:00:00Z",
        "planned_delivery_at": "2026-11-14T06:00:00Z",
        "planned_service_ids": ["rail-main", "sea-main", "road-final"],
        "queue_hours": 0.5,
        "observed_delay_hours": 0.0,
        "documents_ready_at": "2026-11-12T11:00:00Z",
        "events": [
            _event(
                "evt-001-1",
                "rail_departure",
                "rail_hub",
                "2026-11-10T04:00:00Z",
                actual_at="2026-11-10T04:12:00Z",
                quality_flag="observed",
            ),
            _event(
                "evt-001-2",
                "port_arrival",
                "makhachkala",
                "2026-11-12T10:00:00Z",
                estimated_at="2026-11-12T10:30:00Z",
                quality_flag="forecast",
            ),
            _event(
                "evt-001-3",
                "sea_departure",
                "makhachkala",
                "2026-11-12T18:00:00Z",
                estimated_at="2026-11-12T18:00:00Z",
                quality_flag="forecast",
            ),
            _event(
                "evt-001-4",
                "delivery",
                "tehran",
                "2026-11-14T06:00:00Z",
                quality_flag="forecast",
            ),
        ],
    },
    {
        "id": "SCT-002",
        "reference": "Olya transfer demo 002",
        "container_id": "DEMO0000002",
        "cargo": "general",
        "mass_kg": 20000.0,
        "origin_id": "rail_hub",
        "destination_id": "tehran",
        "current_location_id": "makhachkala",
        "status": "in_transit",
        "planned_port_arrival": "2026-11-12T15:00:00Z",
        "planned_delivery_at": "2026-11-14T18:00:00Z",
        "planned_service_ids": [
            "rail-main",
            "transfer-olya",
            "sea-olya",
            "road-final",
        ],
        "queue_hours": 1.0,
        "observed_delay_hours": 0.5,
        "documents_ready_at": "2026-11-12T17:00:00Z",
        "events": [
            _event(
                "evt-002-1",
                "rail_departure",
                "rail_hub",
                "2026-11-09T06:00:00Z",
                actual_at="2026-11-09T06:05:00Z",
                quality_flag="observed",
            ),
            _event(
                "evt-002-2",
                "port_arrival",
                "makhachkala",
                "2026-11-12T15:00:00Z",
                estimated_at="2026-11-12T15:30:00Z",
                quality_flag="forecast",
            ),
            _event(
                "evt-002-3",
                "road_departure",
                "makhachkala",
                "2026-11-12T19:00:00Z",
                estimated_at="2026-11-12T19:00:00Z",
                quality_flag="forecast",
            ),
            _event(
                "evt-002-4",
                "delivery",
                "tehran",
                "2026-11-14T18:00:00Z",
                quality_flag="forecast",
            ),
        ],
    },
    {
        "id": "SCT-003",
        "reference": "Olya feeder demo 003",
        "container_id": "DEMO0000003",
        "cargo": "general",
        "mass_kg": 20000.0,
        "origin_id": "rail_hub",
        "destination_id": "tehran",
        "current_location_id": "makhachkala",
        "status": "in_transit",
        "planned_port_arrival": "2026-11-13T08:00:00Z",
        "planned_delivery_at": "2026-11-14T18:00:00Z",
        "planned_service_ids": [
            "rail-main",
            "transfer-olya",
            "sea-olya",
            "road-final",
        ],
        "queue_hours": 0.5,
        "observed_delay_hours": 0.0,
        "documents_ready_at": "2026-11-13T09:00:00Z",
        "events": [
            _event(
                "evt-003-1",
                "rail_departure",
                "rail_hub",
                "2026-11-11T06:00:00Z",
                actual_at="2026-11-11T06:10:00Z",
                quality_flag="observed",
            ),
            _event(
                "evt-003-2",
                "port_arrival",
                "makhachkala",
                "2026-11-13T08:00:00Z",
                estimated_at="2026-11-13T08:20:00Z",
                quality_flag="forecast",
            ),
            _event(
                "evt-003-3",
                "sea_departure",
                "olya",
                "2026-11-13T22:00:00Z",
                estimated_at="2026-11-13T22:00:00Z",
                quality_flag="forecast",
            ),
            _event(
                "evt-003-4",
                "delivery",
                "tehran",
                "2026-11-14T18:00:00Z",
                quality_flag="forecast",
            ),
        ],
    },
]


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:  # e.g. a huge JSON integer literal
        return False


def _delay_hours(
    *,
    is_storm: bool,
    queue_hours: float,
    wind_mps: float,
    precipitation_mm: float,
    observed_delay_hours: float,
    mass_tonnes: float,
    rng: random.Random,
) -> float:
    """Nonlinear synthetic delay surface used by the seeded history.

    Normal, low-wind rows stay near zero.  Wind and precipitation are superlinear,
    congestion interacts with wind, and storm rows receive an additional penalty.
    The noise is seeded and small relative to the weather effect.
    """

    base = (
        0.2
        + 0.055 * queue_hours
        + 0.0025 * mass_tonnes
        + 0.10 * observed_delay_hours
    )
    wind_excess = max(0.0, wind_mps - 9.0)
    weather = 0.035 * wind_excess**1.8 + 0.06 * precipitation_mm**1.35
    wind_queue_interaction = (
        0.009 * queue_hours * max(0.0, wind_mps - 8.0) ** 1.35
    )
    congestion = 0.08 * max(0.0, queue_hours - 4.0) ** 1.6
    storm_extra = 0.0
    if is_storm:
        storm_extra = 5.0 + 0.12 * max(0.0, wind_mps - 18.0) ** 1.2
    sigma = 1.15 if is_storm else (0.65 if queue_hours > 5.0 else 0.35)
    value = (
        base
        + weather
        + wind_queue_interaction
        + congestion
        + storm_extra
        + rng.gauss(0.0, sigma)
    )
    return round(max(0.0, value), 3)


def _build_history(seed: int) -> list[dict[str, Any]]:
    """Build independent, chronologically ordered synthetic shipment rows."""

    rng = random.Random(seed)
    start = _SIMULATION_DT - timedelta(days=_HISTORY_DAYS)
    window_seconds = (_SIMULATION_DT - start).total_seconds()
    bin_seconds = window_seconds / _HISTORY_ROWS
    rows: list[dict[str, Any]] = []

    for index in range(_HISTORY_ROWS):
        # Non-overlapping bins plus sub-bin jitter keep rows strictly chronological
        # and strictly before simulation_now, without needing a final unsafe sort.
        offset = index * bin_seconds + rng.uniform(0.0, bin_seconds * 0.9)
        observed_at = _to_utc_iso(start + timedelta(seconds=offset))

        roll = rng.random()
        if roll < 0.13:
            regime = "storm"
            wind_mps = round(rng.uniform(18.0, 30.0), 1)
            precipitation_mm = round(rng.uniform(4.0, 30.0), 1)
            queue_hours = round(rng.uniform(3.0, 12.0), 2)
            observed_delay_hours = round(rng.uniform(1.0, 6.0), 2)
            hours_to_cutoff = round(rng.uniform(1.5, 8.0), 2)
        elif roll < 0.22:
            regime = "congestion"
            wind_mps = round(rng.uniform(2.0, 9.0), 1)
            precipitation_mm = round(rng.uniform(0.0, 3.0), 1)
            queue_hours = round(rng.uniform(5.0, 14.0), 2)
            observed_delay_hours = round(rng.uniform(0.5, 4.0), 2)
            hours_to_cutoff = round(rng.uniform(1.5, 18.0), 2)
        elif roll < 0.32:
            # Breezy/rainy but non-storm rows give the weather surface useful
            # intermediate values instead of a synthetic low/high-only gap.
            regime = "breezy"
            wind_mps = round(rng.uniform(9.0, 17.0), 1)
            precipitation_mm = round(rng.uniform(0.0, 8.0), 1)
            queue_hours = round(rng.uniform(1.0, 5.0), 2)
            observed_delay_hours = round(rng.uniform(0.0, 2.0), 2)
            hours_to_cutoff = round(rng.uniform(4.0, 30.0), 2)
        else:
            regime = "normal"
            wind_mps = round(rng.uniform(1.0, 8.5), 1)
            precipitation_mm = round(
                rng.choice([0.0, 0.0, 0.0, 0.2, 0.5, 1.0, 2.0]), 1
            )
            queue_hours = round(rng.uniform(0.0, 3.5), 2)
            observed_delay_hours = round(rng.uniform(0.0, 1.5), 2)
            hours_to_cutoff = round(rng.uniform(2.0, 72.0), 2)

        mass_tonnes = round(rng.uniform(5.0, 35.0), 1)
        delay_hours = _delay_hours(
            is_storm=regime == "storm",
            queue_hours=queue_hours,
            wind_mps=wind_mps,
            precipitation_mm=precipitation_mm,
            observed_delay_hours=observed_delay_hours,
            mass_tonnes=mass_tonnes,
            rng=rng,
        )
        missed_connection = int(
            observed_delay_hours + queue_hours + delay_hours > hours_to_cutoff
        )
        rows.append(
            {
                "shipment_id": f"HST-{index + 1:04d}",
                "observed_at": observed_at,
                "queue_hours": queue_hours,
                "wind_mps": wind_mps,
                "precipitation_mm": precipitation_mm,
                "observed_delay_hours": observed_delay_hours,
                "mass_tonnes": mass_tonnes,
                "hours_to_cutoff": hours_to_cutoff,
                "delay_hours": delay_hours,
                "missed_connection": missed_connection,
            }
        )

    rows.sort(key=lambda row: row["observed_at"])
    return rows


def load_demo_data(seed: int = SEED) -> dict[str, Any]:
    """Return the frozen catalog, demo shipments and seeded history.

    The catalog and three shipments are stable across seeds.  Only the synthetic
    history depends on ``seed``, and repeated calls with the same seed are
    byte-equivalent.  Returned nested values are copies so callers cannot mutate
    the module-level catalog.
    """

    return {
        "ports": copy.deepcopy(_PORTS),
        "services": copy.deepcopy(_SERVICES),
        "shipments": copy.deepcopy(_SHIPMENTS),
        "history": _build_history(seed),
    }


# Deterministic offline weather for the fixed simulation clock.  These are local
# synthetic values, not observations.  Live values are never written to this table.
_SYNTHETIC_WEATHER: dict[str, dict[str, float]] = {
    "rail_hub": {"wind_mps": 4.0, "precipitation_mm": 0.0},
    "makhachkala": {"wind_mps": 6.0, "precipitation_mm": 0.0},
    "olya": {"wind_mps": 7.0, "precipitation_mm": 0.2},
    "anzali": {"wind_mps": 5.0, "precipitation_mm": 0.0},
    "tehran": {"wind_mps": 3.0, "precipitation_mm": 0.0},
}
_PORT_BY_ID = {port["id"]: port for port in _PORTS}


def _synthetic_weather(port_id: str) -> dict[str, Any]:
    values = _SYNTHETIC_WEATHER[port_id]
    return {
        "wind_mps": values["wind_mps"],
        "precipitation_mm": values["precipitation_mm"],
        "wave_height_m": None,
        "source": "synthetic",
        "observed_at": SIMULATION_NOW,
        "stale": False,
    }


def _is_stale(observed_at: datetime | None) -> bool:
    if observed_at is None:
        return True
    age_hours = abs((_SIMULATION_DT - observed_at).total_seconds()) / 3600.0
    return age_hours > CACHE_MAX_AGE_HOURS


def _in_range(value: Any, upper: float) -> bool:
    return _finite_number(value) and 0.0 <= float(value) <= upper


def _plausible_observed_at(value: Any) -> datetime | None:
    observed_at = _parse_utc(value)
    if observed_at is None or not _OBSERVED_AT_MIN <= observed_at <= _OBSERVED_AT_MAX:
        return None
    return observed_at


def _is_open_meteo_url(url: Any) -> bool:
    """True only for the expected HTTPS endpoint (checked after redirects)."""

    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except (TypeError, ValueError, AttributeError):
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == OPEN_METEO_HOST
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
        and parts.path == OPEN_METEO_PATH
    )


def _read_limited(response: Any, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes; None if the body is larger."""

    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = response.read(limit + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        return None
    return b"".join(chunks)


def _fetch_open_meteo(port: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch current weather; return None on any unavailability or null value."""

    query = urllib.parse.urlencode(
        {
            "latitude": port["lat"],
            "longitude": port["lon"],
            "current": "wind_speed_10m,precipitation",
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }
    )
    url = f"{OPEN_METEO_URL}?{query}"
    try:
        with urllib.request.urlopen(
            url, timeout=OPEN_METEO_TIMEOUT_SECONDS
        ) as response:
            # urllib follows redirects itself; only accept a 200 whose final
            # URL is still the expected HTTPS Open-Meteo endpoint.
            if getattr(response, "status", None) != 200 or not _is_open_meteo_url(
                response.geturl()
            ):
                return None
            body = _read_limited(response, MAX_RESPONSE_BYTES)
        if body is None:
            return None
        payload = json.loads(body.decode("utf-8"))
        current = payload.get("current") if isinstance(payload, dict) else None
        if not isinstance(current, dict):
            return None
        wind = current.get("wind_speed_10m")
        precipitation = current.get("precipitation")
        observed_at = _plausible_observed_at(current.get("time"))
        if (
            not _in_range(wind, MAX_WIND_MPS)
            or not _in_range(precipitation, MAX_PRECIPITATION_MM)
            or observed_at is None
        ):
            return None
        return {
            "wind_mps": float(wind),
            "precipitation_mm": float(precipitation),
            "wave_height_m": None,
            "source": "open-meteo",
            "observed_at": _to_utc_iso(observed_at),
            "stale": _is_stale(observed_at),
        }
    except Exception:
        # Network errors, timeouts, malformed JSON and unexpected payloads are all
        # non-fatal: the demo simply uses cache or synthetic fallback.
        return None


def _read_cache_bytes(cache_path: str) -> bytes | None:
    """Read a small regular file; None for oversized or non-regular files."""

    # O_NONBLOCK keeps a FIFO from blocking open() on POSIX; it is a no-op for
    # regular files and absent on Windows.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(cache_path, flags)
    try:
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            return None
        data = handle.read(MAX_CACHE_BYTES + 1)
    if len(data) > MAX_CACHE_BYTES:
        return None
    return data


def _read_cache(cache_path: str) -> dict[str, Any] | None:
    try:
        raw = _read_cache_bytes(cache_path)
        if raw is None:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    weather = payload.get("weather")
    if not isinstance(weather, dict):
        return None
    wind = weather.get("wind_mps")
    precipitation = weather.get("precipitation_mm")
    observed_at = _plausible_observed_at(weather.get("observed_at"))
    if (
        not _in_range(wind, MAX_WIND_MPS)
        or not _in_range(precipitation, MAX_PRECIPITATION_MM)
        or observed_at is None
    ):
        return None
    wave = weather.get("wave_height_m")
    if wave is not None:
        wave = float(wave) if _in_range(wave, MAX_WAVE_HEIGHT_M) else None
    return {
        "wind_mps": float(wind),
        "precipitation_mm": float(precipitation),
        "wave_height_m": wave,
        "source": "cache",
        "observed_at": _to_utc_iso(observed_at),
        "stale": _is_stale(observed_at),
    }


def _write_cache(cache_path: str, weather: dict[str, Any]) -> None:
    """Persist a live response atomically; caching is best-effort only.

    The temporary file is created with an unpredictable name and owner-only
    permissions (``mkstemp``, O_EXCL) next to the destination, fsynced and then
    renamed over ``cache_path`` (``os.replace`` replaces a symlink rather than
    following it).  It is removed on any failure.
    """

    temporary: str | None = None
    try:
        target = os.path.abspath(os.fspath(cache_path))
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        content = json.dumps({"schema_version": 1, "weather": weather}, sort_keys=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".weather-cache-", suffix=".tmp", dir=parent
        )
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except (OSError, TypeError, ValueError):
        return
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def get_weather(
    port_id: str,
    *,
    use_live: bool = False,
    cache_path: str | None = None,
) -> dict[str, Any]:
    """Return a Weather dict for a catalog port.

    Defaults to deterministic synthetic weather with no network access.  With
    ``use_live=True`` a short Open-Meteo request is attempted, then a valid cache
    is used, and finally synthetic data is returned.  ``source`` and ``stale``
    always identify provenance and freshness; no wall clock is used.
    """

    if port_id not in _PORT_BY_ID:
        raise KeyError(f"unknown port_id: {port_id}")

    if not use_live:
        return _synthetic_weather(port_id)

    live = _fetch_open_meteo(_PORT_BY_ID[port_id])
    if live is not None:
        if cache_path:
            _write_cache(cache_path, live)
        return live

    if cache_path:
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

    return _synthetic_weather(port_id)
