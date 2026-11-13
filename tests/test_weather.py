"""Open-Meteo weather provider with a mocked HTTP layer (never touches the network)."""
import json
import threading
import urllib.parse
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import data, weather
from backend.app.main import create_app
from backend.app.schemas import SIMULATION_NOW, Weather

PORTS = data.load_demo_data()["ports"]
SEA = [p for p in PORTS if p["kind"] == "port"]
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
BROKER = {"Authorization": "Bearer dev-broker-token"}


class FakeResponse:
    def __init__(self, payload, *, url, status=200, body=None):
        self._body = body if body is not None else json.dumps(payload).encode()
        self._offset = 0
        self.status = status
        self._url = url

    def geturl(self):
        return self._url

    def read(self, amt=None):
        end = len(self._body) if amt is None else self._offset + amt
        chunk = self._body[self._offset:end]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def forecast_payload(ports, wind=7.5, rain=0.4, time="2026-11-13T08:45"):
    return [{"current": {"time": time, "wind_speed_10m": wind + i, "precipitation": rain}}
            for i, _ in enumerate(ports)]


def marine_payload(ports, wave=1.2):
    # Olya is a river-sea port: the marine model has no cell there (null).
    return [{"current": {"time": "2026-11-13T08:45",
                         "wave_height": None if p["id"] == "olya" else wave}} for p in ports]


class FakeHttp:
    """Routes urlopen calls by endpoint; records every request."""

    def __init__(self, forecast=None, marine=None):
        self.forecast = forecast if forecast is not None else forecast_payload(PORTS)
        self.marine = marine if marine is not None else marine_payload(SEA)
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        base = url.split("?", 1)[0]
        handler = self.forecast if base == weather.FORECAST_URL else self.marine
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(url)
        return FakeResponse(handler, url=url)


def service(monkeypatch, http, **kwargs):
    monkeypatch.setattr(weather.urllib.request, "urlopen", http)
    clock = kwargs.pop("clock", [0.0])
    return weather.WeatherService(PORTS, "open-meteo", monotonic=lambda: clock[0],
                                  utcnow=lambda: NOW, **kwargs)


def test_success_batches_all_ports_in_two_requests(monkeypatch):
    http = FakeHttp()
    svc = service(monkeypatch, http)
    result = svc.get("makhachkala")
    Weather.model_validate(result)
    assert result["source"] == "open-meteo" and result["fallback"] is False
    assert result["observed_at"] == "2026-11-13T08:45:00Z"  # the API's time, not the demo clock
    assert result["stale"] is False
    index = [p["id"] for p in PORTS].index("makhachkala")
    assert result["wind_mps"] == 7.5 + index and result["precipitation_mm"] == 0.4
    assert result["wave_height_m"] == 1.2 and result["wave_estimated"] is False
    # One forecast request for every node, one marine request for sea ports only.
    assert len(http.calls) == 2
    queries = {u.split("?")[0]: urllib.parse.parse_qs(u.split("?")[1]) for u, _ in http.calls}
    forecast = queries[weather.FORECAST_URL]
    assert len(forecast["latitude"][0].split(",")) == len(PORTS)
    assert forecast["wind_speed_unit"] == ["ms"]
    assert forecast["current"] == ["wind_speed_10m,precipitation"]
    assert len(queries[weather.MARINE_URL]["latitude"][0].split(",")) == len(SEA)
    assert all(0 < timeout <= 3 for _, timeout in http.calls)


def test_river_port_wave_is_estimated_and_inland_has_no_sea(monkeypatch):
    svc = service(monkeypatch, FakeHttp())
    olya = svc.get("olya")
    assert olya["source"] == "open-meteo" and olya["wave_estimated"] is True
    assert olya["wave_height_m"] == data.estimate_wave_height_m(olya["wind_mps"])
    tehran = svc.get("tehran")
    assert tehran["wave_height_m"] == 0.0 and tehran["wave_estimated"] is False


def test_cache_hit_within_ttl_then_refresh(monkeypatch):
    http = FakeHttp()
    clock = [0.0]
    svc = service(monkeypatch, http, clock=clock)
    for port in PORTS:
        svc.get(port["id"])
    assert len(http.calls) == 2 and svc.refresh_count == 1
    clock[0] = weather.TTL_SECONDS - 1
    svc.get("aktau")
    assert len(http.calls) == 2
    clock[0] = weather.TTL_SECONDS + 1
    svc.get("aktau")
    assert len(http.calls) == 4 and svc.refresh_count == 2


def test_timeout_falls_back_to_marked_synthetic_and_retries_later(monkeypatch):
    http = FakeHttp(forecast=TimeoutError("timed out"), marine=TimeoutError("timed out"))
    clock = [0.0]
    svc = service(monkeypatch, http, clock=clock)
    result = svc.get("makhachkala")
    Weather.model_validate(result)
    assert result["source"] == "synthetic" and result["fallback"] is True
    assert result["observed_at"] == SIMULATION_NOW
    assert {k: result[k] for k in ("wind_mps", "precipitation_mm")} == {
        k: data.get_weather("makhachkala")[k] for k in ("wind_mps", "precipitation_mm")}
    calls = len(http.calls)
    svc.get("aktau")  # within the retry pause: no new network attempt
    assert len(http.calls) == calls
    clock[0] = weather.RETRY_SECONDS + 1
    svc.get("aktau")
    assert len(http.calls) > calls


def test_hanging_request_is_bounded_by_deadline(monkeypatch):
    release = threading.Event()

    def hang(url):
        release.wait(5)
        raise TimeoutError

    http = FakeHttp(forecast=hang, marine=hang)
    svc = service(monkeypatch, http, deadline_seconds=0.2)
    try:
        assert svc.get("anzali")["fallback"] is True
    finally:
        release.set()


@pytest.mark.parametrize("forecast", [
    {"current": {"wind_speed_10m": 5}},                       # object instead of a list
    forecast_payload(PORTS)[:-1],                              # wrong location count
    [{"no": "current"}] * len(PORTS),                          # missing current block
    [{"current": {"time": "2026-11-13T08:45", "wind_speed_10m": None,
                  "precipitation": 0}}] * len(PORTS),          # null values
    [{"current": {"time": "garbage", "wind_speed_10m": 5,
                  "precipitation": 0}}] * len(PORTS),          # bad timestamp
    [{"current": {"time": "2026-11-13T08:45", "wind_speed_10m": 500,
                  "precipitation": 0}}] * len(PORTS),          # implausible wind
])
def test_malformed_payload_falls_back(monkeypatch, forecast):
    svc = service(monkeypatch, FakeHttp(forecast=forecast))
    result = svc.get("makhachkala")
    Weather.model_validate(result)
    assert result["source"] == "synthetic" and result["fallback"] is True


def test_non_json_and_oversized_bodies_fall_back(monkeypatch):
    for body in (b"{not json", b"[" + b" " * (weather.MAX_RESPONSE_BYTES + 10) + b"]"):
        http = FakeHttp(forecast=lambda url, body=body: FakeResponse(None, url=url, body=body))
        assert service(monkeypatch, http).get("aktau")["fallback"] is True


@pytest.mark.parametrize("final_url", [
    "https://evil.example/v1/forecast", "http://api.open-meteo.com/v1/forecast",
    "https://api.open-meteo.com.evil.example/v1/forecast", "https://api.open-meteo.com/v1/other",
])
def test_redirect_away_from_open_meteo_falls_back(monkeypatch, final_url):
    http = FakeHttp(forecast=lambda url: FakeResponse(forecast_payload(PORTS), url=final_url))
    assert service(monkeypatch, http).get("aktau")["fallback"] is True


def test_non_200_falls_back(monkeypatch):
    http = FakeHttp(forecast=lambda url: FakeResponse(forecast_payload(PORTS), url=url, status=503))
    assert service(monkeypatch, http).get("aktau")["fallback"] is True


def test_one_bad_port_falls_back_alone(monkeypatch):
    payload = forecast_payload(PORTS)
    bad = [p["id"] for p in PORTS].index("anzali")
    payload[bad] = {"current": {"time": "2026-11-13T08:45", "wind_speed_10m": None, "precipitation": 0}}
    svc = service(monkeypatch, FakeHttp(forecast=payload))
    assert svc.get("anzali")["fallback"] is True
    assert svc.get("makhachkala")["source"] == "open-meteo"


def test_marine_failure_keeps_live_wind_and_estimates_waves(monkeypatch):
    svc = service(monkeypatch, FakeHttp(marine=TimeoutError("timed out")))
    result = svc.get("makhachkala")
    assert result["source"] == "open-meteo" and result["wave_estimated"] is True
    assert result["wave_height_m"] == data.estimate_wave_height_m(result["wind_mps"])


def test_failure_after_success_serves_last_observation_as_cache(monkeypatch):
    http = FakeHttp()
    clock = [0.0]
    svc = service(monkeypatch, http, clock=clock)
    live = svc.get("makhachkala")
    http.forecast = TimeoutError("offline")
    clock[0] = weather.TTL_SECONDS + 1
    cached = svc.get("makhachkala")
    Weather.model_validate(cached)
    assert cached["source"] == "cache" and cached["fallback"] is False
    assert cached["observed_at"] == live["observed_at"]
    assert cached["wind_mps"] == live["wind_mps"]


def test_old_observation_is_flagged_stale(monkeypatch):
    http = FakeHttp(forecast=forecast_payload(PORTS, time="2026-11-13T04:00"))
    assert service(monkeypatch, http).get("aktau")["stale"] is True


def test_synthetic_provider_never_uses_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr(weather.urllib.request, "urlopen", fail)
    svc = weather.WeatherService(PORTS, "synthetic")
    svc.warm()
    result = svc.get("makhachkala")
    assert result == data.get_weather("makhachkala") and result["source"] == "synthetic"
    with pytest.raises(KeyError):
        svc.get("nowhere")


@pytest.mark.parametrize("value,expected", [
    (None, "open-meteo"), ("", "open-meteo"), ("open-meteo", "open-meteo"),
    (" Synthetic ", "synthetic"), ("bogus", "synthetic"),
])
def test_provider_from_environment(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(weather.PROVIDER_ENV, raising=False)
    else:
        monkeypatch.setenv(weather.PROVIDER_ENV, value)
    assert weather.configured_provider() == expected


def test_api_uses_live_weather_and_storm_still_overrides(monkeypatch, tmp_path):
    http = FakeHttp(forecast=forecast_payload(PORTS, wind=3.0, rain=0.0))
    monkeypatch.setattr(weather.urllib.request, "urlopen", http)
    monkeypatch.setenv(weather.PROVIDER_ENV, "open-meteo")
    with TestClient(create_app(str(tmp_path / "w.sqlite3"))) as client:
        base = client.get("/api/shipments/SCT-001", headers=BROKER).json()
        assert base["weather"]["source"] == "open-meteo"
        assert base["weather"]["observed_at"] == "2026-11-13T08:45:00Z"
        assert base["weather"]["wave_height_m"] == 1.2
        storm = client.post("/api/shipments/SCT-001/scenario", json={"kind": "storm"},
                            headers=BROKER).json()
        # Live conditions are reported as-is; the scenario's storm wind/waves drive the model.
        assert storm["weather"]["wind_mps"] == base["weather"]["wind_mps"]
        assert storm["scenario"]["wind_mps"] == 22 and storm["scenario"]["wave_height_m"] == 4.5
        assert "High wind" in storm["prediction"]["factors"]
        assert "High sea state" in storm["prediction"]["factors"]
        assert storm["prediction"]["delay_hours"] > base["prediction"]["delay_hours"]
    # Dashboard + detail calls for all shipments shared one cached refresh.
    assert sum(u.startswith(weather.FORECAST_URL) for u, _ in http.calls) == 1
