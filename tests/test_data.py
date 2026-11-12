"""Tests for the synthetic data owner: catalogs, history and weather fallback."""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import data as data_module  # noqa: E402
from app.data import (  # noqa: E402
    _delay_hours,
    estimate_wave_height_m,
    get_weather,
    load_demo_data,
)
from app.schemas import (  # noqa: E402
    SIMULATION_NOW,
    Port,
    Service,
    Shipment,
    Weather,
)

HISTORY_KEYS = {
    "shipment_id",
    "observed_at",
    "queue_hours",
    "wind_mps",
    "wave_height_m",
    "precipitation_mm",
    "observed_delay_hours",
    "mass_tonnes",
    "hours_to_cutoff",
    "delay_hours",
    "missed_connection",
}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture(scope="module")
def demo() -> dict:
    return load_demo_data(42)


class _FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse (status, geturl, read(n))."""

    def __init__(
        self,
        payload: object,
        *,
        url: str = data_module.OPEN_METEO_URL + "?latitude=1&longitude=2",
        status: int = 200,
        body: bytes | None = None,
    ) -> None:
        self._payload = payload
        self._body = body if body is not None else json.dumps(payload).encode("utf-8")
        self._offset = 0
        self.status = status
        self._url = url
        self.read_sizes: list[int | None] = []

    def geturl(self) -> str:
        return self._url

    def read(self, amt: int | None = None) -> bytes:
        self.read_sizes.append(amt)
        end = len(self._body) if amt is None else self._offset + amt
        chunk = self._body[self._offset:end]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# Catalog and shipments
# ---------------------------------------------------------------------------


def test_catalog_has_required_ports_and_services(demo: dict) -> None:
    assert set(demo) == {"ports", "services", "shipments", "history"}

    ports = {port["id"]: port for port in demo["ports"]}
    assert set(ports) == {
        "rail_hub", "makhachkala", "olya", "anzali", "tehran",
        "aktau", "turkmenbashi", "amirabad",
    }
    assert ports["rail_hub"]["kind"] == "terminal"
    assert ports["tehran"]["kind"] == "destination"
    assert ports["makhachkala"]["kind"] == "port"
    for port in demo["ports"]:
        Port.model_validate(port)
        assert isinstance(port["name"], str) and port["name"].strip()
        assert -90 <= port["lat"] <= 90
        assert -180 <= port["lon"] <= 180

    services = {service["id"]: service for service in demo["services"]}
    expected_routes = {
        "rail-main": ("rail_hub", "makhachkala", "rail"),
        "sea-main": ("makhachkala", "anzali", "sea"),
        "transfer-olya": ("makhachkala", "olya", "road"),
        "sea-olya": ("olya", "anzali", "sea"),
        "road-final": ("anzali", "tehran", "road"),
    }
    assert expected_routes.keys() <= services.keys()
    for service_id, (origin, destination, mode) in expected_routes.items():
        service = services[service_id]
        assert (service["origin_id"], service["destination_id"], service["mode"]) == (
            origin,
            destination,
            mode,
        )
        assert service["capacity_tonnes"] >= 40
        assert service["distance_km"] > 0
        assert service["duration_hours"] > 0
        assert "general" in service["allowed_cargo"]
        Service.model_validate(service)


def test_schedule_horizon_and_frozen_sea_departures(demo: dict) -> None:
    now = _parse(SIMULATION_NOW)
    services = {service["id"]: service for service in demo["services"]}

    for service in demo["services"]:
        departures = [_parse(value) for value in service["departures"]]
        assert departures, f"{service['id']} has no departures"
        assert departures == sorted(departures)
        assert len(set(service["departures"])) == len(service["departures"])
        assert max(departures) >= now + timedelta(days=7)

    sea_main = services["sea-main"]
    assert sea_main["departures"][0] == "2026-11-12T18:00:00Z"
    for day in (13, 14, 15):
        assert f"2026-11-{day}T18:00:00Z" in sea_main["departures"]
    assert sea_main["cutoff_hours"] == 4
    assert sea_main["handling_hours"] == 1

    sea_olya = services["sea-olya"]
    for day in (12, 13, 14, 15):
        assert f"2026-11-{day}T22:00:00Z" in sea_olya["departures"]
    assert sea_olya["cutoff_hours"] == 2

    # Road legs must expose explicit slots, not instantaneous/implied edges.
    for service_id in ("transfer-olya", "road-final"):
        service = services[service_id]
        departures = [_parse(value) for value in service["departures"]]
        assert len(departures) > 20
        gaps = [
            (later - earlier)
            for earlier, later in zip(departures, departures[1:])
        ]
        assert all(gap > timedelta(0) for gap in gaps)
        assert max(gaps) <= timedelta(hours=6)


def test_demo_shipments_and_sct001_contract(demo: dict) -> None:
    shipments = demo["shipments"]
    assert [shipment["id"] for shipment in shipments] == [
        "SCT-001",
        "SCT-002",
        "SCT-003",
        "SCT-004",
        "SCT-005",
        "SCT-006",
    ]
    services = {service["id"]: service for service in demo["services"]}
    for shipment in shipments:
        Shipment.model_validate(shipment)
        assert shipment["cargo"] == "general"
        assert shipment["mass_kg"] == 20000
        assert shipment["planned_service_ids"]
        planned = [services[service_id] for service_id in shipment["planned_service_ids"]]
        assert planned[0]["origin_id"] == shipment["origin_id"]
        for earlier, later in zip(planned, planned[1:]):
            assert earlier["destination_id"] == later["origin_id"]
        assert planned[-1]["destination_id"] == shipment["destination_id"]

    sct001 = shipments[0]
    assert sct001["reference"] == "North-South demo 001"
    assert sct001["container_id"] == "DEMO0000001"
    assert sct001["origin_id"] == "rail_hub"
    assert sct001["destination_id"] == "tehran"
    assert sct001["current_location_id"] == "makhachkala"
    assert sct001["planned_service_ids"] == ["rail-main", "sea-main", "road-final"]
    assert sct001["planned_port_arrival"] == "2026-11-12T10:00:00Z"
    assert sct001["planned_delivery_at"] == "2026-11-14T06:00:00Z"
    assert sct001["documents_ready_at"] == "2026-11-12T11:00:00Z"
    assert sct001["queue_hours"] == 0.5
    assert sct001["observed_delay_hours"] == 0
    assert sct001["events"]
    assert {event["id"] for event in sct001["events"]} == {
        "evt-001-1",
        "evt-001-2",
        "evt-001-3",
        "evt-001-4",
    }


def test_catalog_is_seed_independent_but_history_is_not() -> None:
    first = load_demo_data(42)
    second = load_demo_data(42)
    assert _canonical(first) == _canonical(second)
    assert first["history"] == second["history"]

    other_seed = load_demo_data(43)
    assert other_seed["ports"] == first["ports"]
    assert other_seed["services"] == first["services"]
    assert other_seed["shipments"] == first["shipments"]
    assert other_seed["history"] != first["history"]

    # Callers get copies; mutating a previous result cannot poison the catalog.
    first["ports"][0]["name"] = "mutated"
    assert load_demo_data(42)["ports"][0]["name"] != "mutated"


# ---------------------------------------------------------------------------
# Seeded history
# ---------------------------------------------------------------------------


def test_history_rows_are_independent_chronological_and_in_window(demo: dict) -> None:
    history = demo["history"]
    assert len(history) >= 600
    now = _parse(SIMULATION_NOW)
    start = now - timedelta(days=90)

    previous: datetime | None = None
    shipment_ids: set[str] = set()
    for row in history:
        assert set(row) == HISTORY_KEYS
        assert isinstance(row["shipment_id"], str) and row["shipment_id"]
        shipment_ids.add(row["shipment_id"])

        observed_at = _parse(row["observed_at"])
        assert observed_at.tzinfo is not None
        assert start <= observed_at < now
        if previous is not None:
            assert observed_at > previous
        previous = observed_at

        for field in (
            "queue_hours",
            "wind_mps",
            "wave_height_m",
            "precipitation_mm",
            "observed_delay_hours",
            "mass_tonnes",
            "hours_to_cutoff",
            "delay_hours",
        ):
            value = row[field]
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
            assert math.isfinite(float(value))
        assert row["queue_hours"] >= 0
        assert row["wind_mps"] >= 0
        assert row["precipitation_mm"] >= 0
        assert row["observed_delay_hours"] >= 0
        assert row["mass_tonnes"] > 0
        assert row["hours_to_cutoff"] > 0
        assert row["delay_hours"] >= 0
        assert row["missed_connection"] in (0, 1)

    # One row per shipment: no grouped rows and therefore no grouping leakage.
    assert len(shipment_ids) == len(history)
    assert _parse(history[-1]["observed_at"]) - _parse(history[0]["observed_at"]) >= (
        timedelta(days=89)
    )


def test_history_weather_effects_are_strong_and_nonlinear(demo: dict) -> None:
    history = demo["history"]

    calm = [
        row
        for row in history
        if row["wind_mps"] <= 8
        and row["precipitation_mm"] <= 1
        and row["queue_hours"] <= 2
    ]
    storm = [
        row
        for row in history
        if row["wind_mps"] >= 18 and row["precipitation_mm"] >= 4
    ]
    low_wind = [row for row in history if row["wind_mps"] < 9]
    mid_wind = [row for row in history if 9 <= row["wind_mps"] < 18]
    high_wind = [row for row in history if row["wind_mps"] >= 18]

    assert len(calm) >= 50
    assert len(storm) >= 50
    assert len(low_wind) >= 100
    assert len(mid_wind) >= 20
    assert len(high_wind) >= 50

    calm_delay = statistics.median(row["delay_hours"] for row in calm)
    storm_delay = statistics.median(row["delay_hours"] for row in storm)
    assert calm_delay < 2  # baseline normal low-wind delay must be small
    assert storm_delay > 4 * calm_delay
    assert statistics.mean(row["delay_hours"] for row in storm) > (
        statistics.mean(row["delay_hours"] for row in calm) + 3
    )
    assert statistics.mean(row["missed_connection"] for row in storm) >= 0.8

    # Superlinear weather: high wind and even intermediate wind beat calm, with
    # the storm band substantially above the breezy band.
    calm_all = statistics.median(row["delay_hours"] for row in low_wind)
    mid_delay = statistics.median(row["delay_hours"] for row in mid_wind)
    high_delay = statistics.median(row["delay_hours"] for row in high_wind)
    assert mid_delay > calm_all
    assert high_delay > mid_delay + 2


# ---------------------------------------------------------------------------
# Weather: default, live, cache and fallback
# ---------------------------------------------------------------------------


def test_synthetic_weather_is_deterministic_and_schema_valid(demo: dict) -> None:
    for port in demo["ports"]:
        first = get_weather(port["id"])
        second = get_weather(port["id"])
        assert first == second
        Weather.model_validate(first)
        assert first["source"] == "synthetic"
        assert first["observed_at"] == SIMULATION_NOW
        assert first["stale"] is False
        assert first["wind_mps"] >= 0
        assert first["precipitation_mm"] >= 0
        assert 0 <= first["wave_height_m"] <= 1.0  # calm demo sea state
        if first["wind_mps"] > 0:
            assert first["wave_height_m"] <= estimate_wave_height_m(first["wind_mps"]) + 0.3

    with pytest.raises(KeyError):
        get_weather("unknown-port")


def test_default_weather_never_uses_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("network must not be used by default")

    monkeypatch.setattr(data_module.urllib.request, "urlopen", fail)
    monkeypatch.setattr(data_module, "_fetch_open_meteo", fail)
    weather = get_weather("makhachkala")
    assert weather["source"] == "synthetic"
    Weather.model_validate(weather)


def test_live_weather_parses_honest_timestamp_with_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        captured["url"] = url
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "current": {
                    "time": "2026-11-12T06:15",
                    "wind_speed_10m": 9.2,
                    "precipitation": 1.4,
                }
            }
        )

    monkeypatch.setattr(data_module.urllib.request, "urlopen", fake_urlopen)
    weather = get_weather("anzali", use_live=True)

    assert weather["source"] == "open-meteo"
    # The source timestamp is preserved/normalized, not replaced by the demo clock.
    assert weather["observed_at"] == "2026-11-12T06:15:00Z"
    assert weather["wind_mps"] == 9.2
    assert weather["precipitation_mm"] == 1.4
    assert weather["wave_height_m"] is None
    assert weather["stale"] is False
    Weather.model_validate(weather)
    assert 0 < float(captured["timeout"]) <= 5


def test_live_null_values_fall_back_to_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        return _FakeResponse(
            {
                "current": {
                    "time": "2026-11-12T08:00",
                    "wind_speed_10m": None,  # unavailable/null value
                    "precipitation": 0.0,
                }
            }
        )

    monkeypatch.setattr(data_module.urllib.request, "urlopen", fake_urlopen)
    weather = get_weather("makhachkala", use_live=True)
    assert weather["source"] == "synthetic"
    assert weather["observed_at"] == SIMULATION_NOW
    Weather.model_validate(weather)


def test_live_network_failure_falls_back_to_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise TimeoutError("offline")

    monkeypatch.setattr(data_module.urllib.request, "urlopen", unavailable)
    weather = get_weather("olya", use_live=True)
    assert weather["source"] == "synthetic"
    Weather.model_validate(weather)


def test_live_weather_writes_cache_and_cache_is_used_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_weather = {
        "wind_mps": 8.5,
        "precipitation_mm": 0.7,
        "wave_height_m": None,
        "source": "open-meteo",
        "observed_at": "2026-11-12T07:45:00Z",
        "stale": False,
    }

    def fake_fetch(port: dict) -> dict:
        assert port["id"] == "anzali"
        return dict(live_weather)

    monkeypatch.setattr(data_module, "_fetch_open_meteo", fake_fetch)
    cache_path = tmp_path / "weather-cache.json"
    first = get_weather("anzali", use_live=True, cache_path=str(cache_path))

    assert first == live_weather
    assert cache_path.exists()
    Weather.model_validate(first)

    monkeypatch.setattr(data_module, "_fetch_open_meteo", lambda port: None)
    cached = get_weather("anzali", use_live=True, cache_path=str(cache_path))
    assert cached["source"] == "cache"
    assert cached["observed_at"] == live_weather["observed_at"]
    assert cached["wind_mps"] == live_weather["wind_mps"]
    assert cached["precipitation_mm"] == live_weather["precipitation_mm"]
    assert cached["stale"] is False
    Weather.model_validate(cached)


def test_old_cache_is_flagged_stale_and_corrupt_cache_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(data_module, "_fetch_open_meteo", lambda port: None)
    cache_path = tmp_path / "old-cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "weather": {
                    "wind_mps": 11.0,
                    "precipitation_mm": 2.0,
                    "wave_height_m": None,
                    "source": "open-meteo",
                    "observed_at": "2026-11-01T00:00:00Z",
                    "stale": False,
                },
            }
        ),
        encoding="utf-8",
    )

    cached = get_weather("olya", use_live=True, cache_path=str(cache_path))
    assert cached["source"] == "cache"
    assert cached["stale"] is True
    assert cached["observed_at"] == "2026-11-01T00:00:00Z"
    Weather.model_validate(cached)

    cache_path.write_text("{not-json", encoding="utf-8")
    fallback = get_weather("olya", use_live=True, cache_path=str(cache_path))
    assert fallback["source"] == "synthetic"
    Weather.model_validate(fallback)


# ---------------------------------------------------------------------------
# Live fetch hardening (urlopen is always mocked; no network)
# ---------------------------------------------------------------------------

GOOD_CURRENT = {"time": "2026-11-12T06:15", "wind_speed_10m": 9.2, "precipitation": 1.4}


def _serve(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> list[str]:
    requested: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        requested.append(url)
        assert timeout == data_module.OPEN_METEO_TIMEOUT_SECONDS
        return response

    monkeypatch.setattr(data_module.urllib.request, "urlopen", fake_urlopen)
    return requested


def test_live_request_targets_only_open_meteo_https(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch, _FakeResponse({"current": GOOD_CURRENT}))
    assert get_weather("anzali", use_live=True)["source"] == "open-meteo"
    assert len(requested) == 1
    assert requested[0].startswith("https://api.open-meteo.com/v1/forecast?")


def test_live_oversized_response_is_rejected_with_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"current": GOOD_CURRENT, "padding": "x" * (70 * 1024)}).encode()
    response = _FakeResponse(None, body=body)
    _serve(monkeypatch, response)
    assert get_weather("anzali", use_live=True)["source"] == "synthetic"
    # Never an unbounded read() and never more than limit + 1 bytes in total.
    assert None not in response.read_sizes
    assert sum(size or 0 for size in response.read_sizes) <= data_module.MAX_RESPONSE_BYTES + 1

    exact = json.dumps({"current": GOOD_CURRENT}).encode()
    exact += b" " * (data_module.MAX_RESPONSE_BYTES - len(exact))
    _serve(monkeypatch, _FakeResponse(None, body=exact))
    assert get_weather("anzali", use_live=True)["source"] == "open-meteo"


@pytest.mark.parametrize(
    "final_url",
    [
        "https://evil.example/v1/forecast?latitude=1",
        "http://api.open-meteo.com/v1/forecast?latitude=1",
        "https://api.open-meteo.com.evil.example/v1/forecast",
        "https://user:pw@api.open-meteo.com/v1/forecast",
        "https://api.open-meteo.com:8443/v1/forecast",
        "https://api.open-meteo.com/v1/other",
        "file:///etc/passwd",
        "",
    ],
)
def test_live_redirect_away_from_open_meteo_is_rejected(
    monkeypatch: pytest.MonkeyPatch, final_url: str
) -> None:
    _serve(monkeypatch, _FakeResponse({"current": GOOD_CURRENT}, url=final_url))
    assert get_weather("anzali", use_live=True)["source"] == "synthetic"


def test_live_equivalent_final_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://API.Open-Meteo.com:443/v1/forecast?latitude=37.47"
    _serve(monkeypatch, _FakeResponse({"current": GOOD_CURRENT}, url=url))
    assert get_weather("anzali", use_live=True)["source"] == "open-meteo"


@pytest.mark.parametrize("status", [201, 203, 204, 206, 301, None])
def test_live_non_200_is_rejected(monkeypatch: pytest.MonkeyPatch, status: int | None) -> None:
    _serve(monkeypatch, _FakeResponse({"current": GOOD_CURRENT}, status=status))
    assert get_weather("anzali", use_live=True)["source"] == "synthetic"


@pytest.mark.parametrize(
    "override",
    [
        {"wind_speed_10m": 150.0},
        {"wind_speed_10m": -0.1},
        {"wind_speed_10m": True},
        {"wind_speed_10m": 10**400},
        {"precipitation": 600.0},
        {"precipitation": -1.0},
        {"time": "1900-01-01T00:00"},
        {"time": "2200-01-01T00:00"},
        {"time": 1731398400},
    ],
)
def test_live_implausible_values_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, override: dict
) -> None:
    _serve(monkeypatch, _FakeResponse({"current": {**GOOD_CURRENT, **override}}))
    weather = get_weather("anzali", use_live=True)
    assert weather["source"] == "synthetic"
    Weather.model_validate(weather)


def test_live_boundary_values_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    current = {"time": "2026-11-12T06:15", "wind_speed_10m": 100, "precipitation": 0}
    _serve(monkeypatch, _FakeResponse({"current": current}))
    weather = get_weather("anzali", use_live=True)
    assert weather["source"] == "open-meteo"
    assert weather["wind_mps"] == 100.0 and weather["precipitation_mm"] == 0.0


# ---------------------------------------------------------------------------
# Cache file handling
# ---------------------------------------------------------------------------


def _write_cache_file(path: Path, weather: dict, padding: int = 0) -> None:
    document: dict = {"schema_version": 1, "weather": weather}
    if padding:
        document["padding"] = "x" * padding
    path.write_text(json.dumps(document), encoding="utf-8")


CACHED = {
    "wind_mps": 8.0,
    "precipitation_mm": 0.5,
    "wave_height_m": 1.5,
    "source": "open-meteo",
    "observed_at": "2026-11-12T07:00:00Z",
    "stale": False,
}


@pytest.mark.parametrize(
    "override",
    [
        {"wind_mps": 1000.0},
        {"wind_mps": 10**400},  # used to raise OverflowError out of get_weather
        {"precipitation_mm": 501.0},
        {"observed_at": "0001-01-01T00:00:00Z"},
        {"observed_at": "9999-01-01T00:00:00Z"},
    ],
)
def test_cache_implausible_values_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: dict
) -> None:
    monkeypatch.setattr(data_module, "_fetch_open_meteo", lambda port: None)
    cache_path = tmp_path / "cache.json"
    _write_cache_file(cache_path, {**CACHED, **override})
    weather = get_weather("olya", use_live=True, cache_path=str(cache_path))
    assert weather["source"] == "synthetic"
    Weather.model_validate(weather)


def test_cache_wave_height_bounds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(data_module, "_fetch_open_meteo", lambda port: None)
    cache_path = tmp_path / "cache.json"
    _write_cache_file(cache_path, CACHED)
    assert get_weather("olya", use_live=True, cache_path=str(cache_path))["wave_height_m"] == 1.5
    _write_cache_file(cache_path, {**CACHED, "wave_height_m": 41.0})
    cached = get_weather("olya", use_live=True, cache_path=str(cache_path))
    assert cached["source"] == "cache" and cached["wave_height_m"] is None


def test_cache_size_cap_and_non_regular_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(data_module, "_fetch_open_meteo", lambda port: None)
    cache_path = tmp_path / "cache.json"
    _write_cache_file(cache_path, CACHED, padding=data_module.MAX_CACHE_BYTES)
    assert cache_path.stat().st_size > data_module.MAX_CACHE_BYTES
    assert get_weather("olya", use_live=True, cache_path=str(cache_path))["source"] == "synthetic"

    _write_cache_file(cache_path, CACHED, padding=1024)
    assert get_weather("olya", use_live=True, cache_path=str(cache_path))["source"] == "cache"

    directory = tmp_path / "a-directory"
    directory.mkdir()
    assert get_weather("olya", use_live=True, cache_path=str(directory))["source"] == "synthetic"

    if hasattr(data_module.os, "mkfifo"):
        fifo = tmp_path / "fifo"
        data_module.os.mkfifo(fifo)  # must not block or be read
        assert get_weather("olya", use_live=True, cache_path=str(fifo))["source"] == "synthetic"


def _live(port: dict) -> dict:
    return {
        "wind_mps": 8.5,
        "precipitation_mm": 0.7,
        "wave_height_m": None,
        "source": "open-meteo",
        "observed_at": "2026-11-12T07:45:00Z",
        "stale": False,
    }


def test_cache_write_uses_private_unpredictable_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(data_module, "_fetch_open_meteo", _live)
    cache_path = tmp_path / "weather-cache.json"
    # A pre-planted file at the old predictable temp name must not be used.
    planted = tmp_path / "weather-cache.json.tmp"
    planted.write_text("planted", encoding="utf-8")

    get_weather("anzali", use_live=True, cache_path=str(cache_path))
    assert planted.read_text(encoding="utf-8") == "planted"
    assert json.loads(cache_path.read_text(encoding="utf-8"))["weather"] == _live({})
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "weather-cache.json",
        "weather-cache.json.tmp",
    ]
    if data_module.os.name == "posix":
        assert cache_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_cache_write_failure_cleans_up_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failing: str
) -> None:
    monkeypatch.setattr(data_module, "_fetch_open_meteo", _live)
    cache_path = tmp_path / "weather-cache.json"
    cache_path.write_text("previous", encoding="utf-8")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(data_module.os, failing, broken)
    weather = get_weather("anzali", use_live=True, cache_path=str(cache_path))
    monkeypatch.undo()
    assert weather["source"] == "open-meteo"  # caching stays best-effort
    assert cache_path.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["weather-cache.json"]


def test_eastern_branch_ports_services_and_shipments(demo: dict) -> None:
    ports = {port["id"]: port for port in demo["ports"]}
    assert ports["amirabad"]["country"] == "IR"
    assert ports["aktau"]["country"] == "KZ"
    assert ports["turkmenbashi"]["country"] == "TM"
    assert all(ports[i]["kind"] == "port" for i in ("aktau", "turkmenbashi", "amirabad"))

    services = {service["id"]: service for service in demo["services"]}
    expected = {
        "rail-east": ("rail_hub", "aktau", "rail"),
        "sea-aktau": ("aktau", "amirabad", "sea"),
        "rail-kz-tm": ("aktau", "turkmenbashi", "rail"),
        "sea-tkm": ("turkmenbashi", "amirabad", "sea"),
        "sea-mak-amirabad": ("makhachkala", "amirabad", "sea"),
        "rail-amirabad": ("amirabad", "tehran", "rail"),
    }
    for service_id, (origin, destination, mode) in expected.items():
        service = services[service_id]
        assert (service["origin_id"], service["destination_id"], service["mode"]) == (
            origin, destination, mode)
        assert service["capacity_tonnes"] >= 40
        Service.model_validate(service)

    for shipment in demo["shipments"][3:]:
        assert shipment["destination_id"] == "tehran"
        assert shipment["current_location_id"] in ports
        assert {event["location_id"] for event in shipment["events"]} <= set(ports)

    for port_id in ("aktau", "turkmenbashi", "amirabad"):
        first = get_weather(port_id)
        assert first == get_weather(port_id)
        assert first["source"] == "synthetic"


def test_history_wave_height_correlates_with_wind_and_is_deterministic() -> None:
    history = load_demo_data()["history"]
    assert history == load_demo_data()["history"]
    waves = [row["wave_height_m"] for row in history]
    assert all(0 < w <= 40 for w in waves)
    calm = [r["wave_height_m"] for r in history if r["wind_mps"] < 9]
    stormy = [r["wave_height_m"] for r in history if r["wind_mps"] >= 18]
    assert statistics.median(calm) < 1.0
    assert statistics.median(stormy) > 3.0
    # Sea state adds delay beyond wind: same row, higher waves -> longer delay.
    base = dict(is_storm=False, queue_hours=2.0, wind_mps=12.0, precipitation_mm=0.0,
                observed_delay_hours=0.0, mass_tonnes=20.0)
    low = _delay_hours(**base, wave_height_m=1.0, rng=random.Random(1))
    high = _delay_hours(**base, wave_height_m=4.0, rng=random.Random(1))
    assert high > low + 1.0


def test_wave_estimate_is_monotonic_and_bounded() -> None:
    values = [estimate_wave_height_m(w) for w in range(0, 41)]
    assert values == sorted(values)
    assert 0.2 <= estimate_wave_height_m(0) and max(values) <= 8.0
    assert 0.3 <= estimate_wave_height_m(4) <= 0.8
    assert 3.0 <= estimate_wave_height_m(22) <= 5.0
