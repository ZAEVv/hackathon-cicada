"""Current weather for every catalog node: live Open-Meteo or synthetic fallback.

``WEATHER_PROVIDER`` selects the provider (read when the service is created):

* ``open-meteo`` (default for the app and Docker): one batched request to the
  Open-Meteo forecast API (wind at 10 m in m/s, precipitation) and one to the
  marine API (significant wave height) for all catalog coordinates, run in
  parallel with a short timeout.  Results live in memory for ``TTL_SECONDS``, so
  page loads hit the network at most once per TTL.
* ``synthetic``: the deterministic offline table in ``data.py`` (the test suite
  uses this; no network I/O).

Failure handling never raises and never waits longer than ``DEADLINE_SECONDS``:

1. a port missing from an otherwise valid response falls back alone;
2. if the forecast request fails (timeout, HTTP error, redirect off Open-Meteo,
   oversized or malformed body) the last good live snapshot is served as
   ``source="cache"`` with its real observation time, else synthetic weather
   with ``fallback=True``; the next attempt is made after ``RETRY_SECONDS``;
3. a sea port with no marine value gets the documented wind-based wave estimate
   (``wave_estimated=True``); inland nodes have no sea state (0 m).

``observed_at`` is the observation time reported by Open-Meteo, and ``stale``
is judged against the wall clock (live data older than ``STALE_AFTER_HOURS``).
The simulation clock and schedules are unchanged: live weather is applied as the
current conditions, and scenario overrides (storm wind/waves) still win in
``predictive.predict``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Callable

from . import data

logger = logging.getLogger("sct.weather")

PROVIDER_ENV = "WEATHER_PROVIDER"
PROVIDERS = ("open-meteo", "synthetic")
DEFAULT_PROVIDER = "open-meteo"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
TIMEOUT_SECONDS = 3.0
# Hard bound on how long one refresh may hold a request (covers slow DNS, which
# the socket timeout does not).
DEADLINE_SECONDS = TIMEOUT_SECONDS + 0.5
TTL_SECONDS = 30 * 60
RETRY_SECONDS = 2 * 60
STALE_AFTER_HOURS = 3.0
MAX_RESPONSE_BYTES = data.MAX_RESPONSE_BYTES
SEA_KINDS = frozenset({"port"})


def configured_provider() -> str:
    value = (os.getenv(PROVIDER_ENV) or "").strip().lower()
    if not value:
        return DEFAULT_PROVIDER
    if value not in PROVIDERS:
        logger.warning("Unknown %s value; using synthetic weather", PROVIDER_ENV)
        return "synthetic"
    return value


def _allowed_final_url(url: Any, expected: str) -> bool:
    """True only for the expected HTTPS endpoint (checked after redirects)."""

    want = urllib.parse.urlsplit(expected)
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except (TypeError, ValueError, AttributeError):
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == want.hostname
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
        and parts.path == want.path
    )


def _get_json(base_url: str, params: dict[str, str], timeout: float) -> Any:
    """GET JSON from an Open-Meteo endpoint; raise on anything unexpected."""

    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if getattr(response, "status", None) != 200:
            raise ValueError("unexpected HTTP status")
        if not _allowed_final_url(response.geturl(), base_url):
            raise ValueError("redirected away from Open-Meteo")
        body = data._read_limited(response, MAX_RESPONSE_BYTES)
    if body is None:
        raise ValueError("response too large")
    return json.loads(body.decode("utf-8"))


def _locations(payload: Any, count: int) -> list[dict]:
    """Open-Meteo returns an object for one location and a list for several."""

    items = [payload] if isinstance(payload, dict) and count == 1 else payload
    if not isinstance(items, list) or len(items) != count:
        raise ValueError("location count mismatch")
    return items


def _current(item: Any) -> dict:
    current = item.get("current") if isinstance(item, dict) else None
    return current if isinstance(current, dict) else {}


def _coordinates(ports: list[dict]) -> dict[str, str]:
    return {
        "latitude": ",".join(f"{float(p['lat']):.4f}" for p in ports),
        "longitude": ",".join(f"{float(p['lon']):.4f}" for p in ports),
        "timezone": "UTC",
    }


class WeatherService:
    """Per-process weather source with an in-memory TTL cache."""

    def __init__(
        self,
        ports: list[dict],
        provider: str | None = None,
        *,
        ttl_seconds: float = TTL_SECONDS,
        retry_seconds: float = RETRY_SECONDS,
        timeout_seconds: float = TIMEOUT_SECONDS,
        deadline_seconds: float = DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.provider = provider if provider is not None else configured_provider()
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown weather provider: {self.provider}")
        self._ports = [dict(p) for p in ports if p.get("id") in data._PORT_BY_ID]
        self._ttl = ttl_seconds
        self._retry = retry_seconds
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._lock = threading.Lock()
        self._snapshot: dict[str, dict] | None = None
        self._last_live: dict[str, dict] = {}
        self._expires_at = 0.0
        self.refresh_count = 0  # network refresh attempts (for tests/diagnostics)

    # ------------------------------------------------------------ public API
    def get(self, port_id: str) -> dict[str, Any]:
        """Weather dict (``schemas.Weather``) for a catalog port id."""

        if port_id not in data._PORT_BY_ID:
            raise KeyError(f"unknown port_id: {port_id}")
        if self.provider == "synthetic":
            return data.get_weather(port_id)
        item = self._current().get(port_id)
        if item is None:
            return self._fallback(port_id)
        result = dict(item)
        if result["source"] in ("open-meteo", "cache"):
            result["stale"] = self._is_stale(result["observed_at"])
        return result

    def warm(self) -> None:
        """Fetch in the background so the first page load does not wait."""

        if self.provider == "synthetic":
            return
        threading.Thread(target=self._current, name="weather-warm", daemon=True).start()

    # ------------------------------------------------------------ internals
    def _fallback(self, port_id: str) -> dict[str, Any]:
        weather = data.get_weather(port_id)
        weather["fallback"] = True
        return weather

    def _is_stale(self, observed_at: str) -> bool:
        observed = data._parse_utc(observed_at)
        if observed is None:
            return True
        return (self._utcnow() - observed).total_seconds() > STALE_AFTER_HOURS * 3600

    def _current(self) -> dict[str, dict]:
        with self._lock:
            now = self._monotonic()
            if self._snapshot is not None and now < self._expires_at:
                return self._snapshot
            self.refresh_count += 1
            live = self._fetch()
            if live:
                self._last_live = {**self._last_live, **live}
                self._snapshot = live
                self._expires_at = self._monotonic() + self._ttl
            else:
                # Keep serving the last real observation (honestly labelled) and
                # retry soon; without one, ports fall back to synthetic in get().
                self._snapshot = {
                    port_id: {**item, "source": "cache"}
                    for port_id, item in self._last_live.items()
                }
                self._expires_at = self._monotonic() + self._retry
            return self._snapshot

    def _fetch(self) -> dict[str, dict] | None:
        """One batched forecast + marine refresh; None if the forecast failed."""

        sea = [p for p in self._ports if p.get("kind") in SEA_KINDS]
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="open-meteo")
        try:
            forecast = pool.submit(
                _get_json,
                FORECAST_URL,
                {
                    **_coordinates(self._ports),
                    "current": "wind_speed_10m,precipitation",
                    "wind_speed_unit": "ms",
                },
                self._timeout,
            )
            marine = (
                pool.submit(
                    _get_json,
                    MARINE_URL,
                    {**_coordinates(sea), "current": "wave_height"},
                    self._timeout,
                )
                if sea
                else None
            )
            started = self._monotonic()
            try:
                forecast_payload = forecast.result(timeout=self._deadline)
            except (FutureTimeout, Exception) as exc:  # noqa: BLE001 - any failure falls back
                logger.warning("Open-Meteo forecast unavailable (%s); using fallback weather",
                               type(exc).__name__)
                return None
            waves: dict[str, float] = {}
            if marine is not None:
                remaining = max(0.0, self._deadline - (self._monotonic() - started))
                try:
                    marine_payload = marine.result(timeout=remaining)
                    for port, item in zip(sea, _locations(marine_payload, len(sea))):
                        wave = _current(item).get("wave_height")
                        if data._in_range(wave, data.MAX_WAVE_HEIGHT_M):
                            waves[port["id"]] = float(wave)
                except (FutureTimeout, Exception) as exc:  # noqa: BLE001
                    logger.warning("Open-Meteo marine unavailable (%s); estimating waves from wind",
                                   type(exc).__name__)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        try:
            items = _locations(forecast_payload, len(self._ports))
        except ValueError:
            logger.warning("Open-Meteo forecast payload malformed; using fallback weather")
            return None
        result: dict[str, dict] = {}
        for port, item in zip(self._ports, items):
            current = _current(item)
            wind = current.get("wind_speed_10m")
            rain = current.get("precipitation")
            observed = data._plausible_observed_at(current.get("time"))
            if (
                observed is None
                or not data._in_range(wind, data.MAX_WIND_MPS)
                or not data._in_range(rain, data.MAX_PRECIPITATION_MM)
            ):
                continue  # this port alone falls back
            is_sea = port.get("kind") in SEA_KINDS
            wave = waves.get(port["id"])
            estimated = is_sea and wave is None
            if estimated:
                wave = data.estimate_wave_height_m(float(wind))
            result[port["id"]] = {
                "wind_mps": float(wind),
                "precipitation_mm": float(rain),
                "wave_height_m": wave if is_sea else 0.0,
                "source": "open-meteo",
                "observed_at": data._to_utc_iso(observed),
                "stale": False,
                "fallback": False,
                "wave_estimated": estimated,
            }
        return result or None
