"""Seeded synthetic delay model; importing this module does not train or load data.

One monotonic regressor forecasts additional delay. The miss probability is not
a separate model: it is the share of held-out-from-fitting residuals that would
push readiness past the cutoff, so it cannot contradict the delay forecast.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

import numpy as np
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, roc_auc_score

from .data import MAX_WAVE_HEIGHT_M, estimate_wave_height_m


_NOW = "2026-11-12T08:00:00Z"
_VERSION = "gbm-v4"
_FEATURES = (
    "queue_hours", "wind_mps", "precipitation_mm", "observed_delay_hours",
    "mass_tonnes", "wave_height_m",
)
# More queue, wind, rain, known delay or wave height never lowers the forecast;
# mass is free.
_MONOTONIC = (1, 1, 1, 1, 0, 1)
# hours_to_cutoff is not a delay driver: it is used only to score miss labels.
_HISTORY_NUMBERS = _FEATURES + ("hours_to_cutoff",)
_MIN_LEAF = 10
# Below this the booster cannot split at all; use the train mean explicitly.
_MIN_BOOSTED_ROWS = 2 * _MIN_LEAF
_EPSILON_HOURS = 1e-9


@dataclass(frozen=True)
class _Model:
    regressor: object
    residuals: np.ndarray  # actual - forecast additional delay, hours; train rows only


def _timestamp(value: str, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() != timedelta(0):
            raise ValueError("UTC timestamp required")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UTC ISO timestamp") from exc


def _number(value: object, name: str, *, signed: bool = False) -> float:
    try:
        if isinstance(value, (bool, str)):
            raise ValueError("numeric value required")
        result = float(value)
        if not math.isfinite(result) or (not signed and result < 0):
            raise ValueError("finite nonnegative value required")
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {name}") from exc


def _wave(value: object, wind: object) -> float:
    """Wave height in metres; None falls back to the documented wind estimate."""
    if value is None:
        return estimate_wave_height_m(_number(wind, "wind_mps"))
    result = _number(value, "wave_height_m")
    if result > MAX_WAVE_HEIGHT_M:
        raise ValueError("Invalid wave_height_m")
    return result


def _fit(x: np.ndarray, y: np.ndarray, seed: int) -> object:
    if len(x) < _MIN_BOOSTED_ROWS:
        return DummyRegressor(strategy="mean").fit(x, y)
    return HistGradientBoostingRegressor(
        loss="squared_error", monotonic_cst=list(_MONOTONIC),
        min_samples_leaf=_MIN_LEAF, early_stopping=False, random_state=seed,
    ).fit(x, y)


def _delays(regressor: object, x: np.ndarray) -> np.ndarray:
    result = np.asarray(regressor.predict(x), dtype=float)
    if not np.isfinite(result).all():
        raise ValueError("Model produced non-finite delay")
    return np.maximum(0.0, result)


def _miss_probabilities(delays: np.ndarray, residuals: np.ndarray,
                        budgets: np.ndarray) -> np.ndarray:
    """Share of residuals for which forecast + residual exceeds the budget.

    ``budgets`` is the slack left for additional delay. Equality is on time,
    matching the scheduler's readiness <= cutoff.
    """
    simulated = np.maximum(0.0, delays[:, None] + residuals[None, :])
    return (simulated > budgets[:, None] + _EPSILON_HOURS).mean(axis=1)


def train_model(history: list[dict], seed: int = 42) -> tuple[object, dict]:
    """Fit on the first 80%; held-out rows never influence fitting or baseline.

    One independent shipment per row is required. At least two rows are needed
    for a nonempty chronological holdout. Unsorted history is sorted on a copy.
    Residuals come from the last quarter of the training rows, scored by a
    regressor fitted on the rows before them, so they are not in-sample
    optimistic; with too few rows for that they fall back to in-sample.
    """
    if not isinstance(history, list) or len(history) < 2:
        raise ValueError("At least two independent history rows are required")
    rows = []
    seen = set()
    for row in history:
        try:
            shipment_id = row["shipment_id"]
            if not isinstance(shipment_id, str) or not shipment_id.strip() or shipment_id in seen:
                raise ValueError("History requires unique shipment IDs")
            seen.add(shipment_id)
            observed = _timestamp(row["observed_at"], "observed_at")
            if observed >= _timestamp(_NOW, "simulation_now"):
                raise ValueError("History must precede simulation_now")
            # Older history without a sea-state column falls back to the wind estimate.
            wave = row.get("wave_height_m")
            numbers = [
                _wave(wave, row["wind_mps"]) if key == "wave_height_m"
                else _number(row[key], key, signed=key == "hours_to_cutoff")
                for key in _HISTORY_NUMBERS
            ]
            if numbers[4] <= 0:
                raise ValueError("mass_tonnes must be positive")
            target = _number(row["delay_hours"], "delay_hours")
            label = _number(row["missed_connection"], "missed_connection")
            if label not in (0, 1):
                raise ValueError("missed_connection must be 0 or 1")
            rows.append((observed, numbers, target, int(label)))
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed history row") from exc
    rows.sort(key=lambda row: row[0])
    split = max(1, int(len(rows) * 0.8))
    numbers = np.asarray([row[1] for row in rows], dtype=float)
    x, slack = numbers[:, :len(_FEATURES)], numbers[:, len(_FEATURES)]
    y = np.asarray([row[2] for row in rows], dtype=float)
    labels = np.asarray([row[3] for row in rows], dtype=int)
    regressor = _fit(x[:split], y[:split], seed)
    calibration = split // 4
    if calibration:
        fit_end = split - calibration
        proxy = _fit(x[:fit_end], y[:fit_end], seed)
        residuals = y[fit_end:split] - _delays(proxy, x[fit_end:split])
    else:
        residuals = y[:split] - _delays(regressor, x[:split])
    estimates = _delays(regressor, x[split:])
    # History labels a miss when observed + queue + additional delay > slack.
    budgets = slack[split:] - x[split:, 3] - x[split:, 0]
    probabilities = _miss_probabilities(estimates, residuals, budgets)
    baseline = np.full(len(rows) - split, np.median(y[:split]))
    metrics = {
        "model_version": _VERSION,
        "train_rows": split,
        "test_rows": len(rows) - split,
        "split_at": rows[split][0].isoformat().replace("+00:00", "Z"),
        "baseline_mae_hours": float(mean_absolute_error(y[split:], baseline)),
        "model_mae_hours": float(mean_absolute_error(y[split:], estimates)),
        "roc_auc": (
            float(roc_auc_score(labels[split:], probabilities))
            if len(np.unique(labels[split:])) == 2 else None
        ),
        "disclaimer": "Held-out synthetic history; not validated on real Caspian operations.",
    }
    return _Model(regressor, residuals), metrics


def predict(model: object, shipment: dict, weather: dict,
            scenario: dict, now: str = _NOW, *, cutoff: str | None = None) -> dict:
    """Forecast additional delay, then add known delays exactly once.

    ``cutoff`` is the planned departure's cutoff (see ``routing.planned_cutoff``);
    without it the arrival day's 14:00Z cutoff is assumed. The miss
    probability is the chance that readiness, including documents, falls after
    that cutoff given the spread of past forecast errors. It is a synthetic
    estimate, not a guaranteed calibrated probability. Factors describe input
    conditions, not SHAP or causal attribution.
    """
    if not isinstance(model, _Model):
        raise ValueError("Expected a bundle returned by train_model")
    try:
        _timestamp(now, "now")
        arrival = _timestamp(shipment["planned_port_arrival"], "planned_port_arrival")
        documents = _timestamp(shipment["documents_ready_at"], "documents_ready_at")
        queue = _number(shipment["queue_hours"], "queue_hours")
        queue_extra = _number(scenario.get("queue_extra_hours", 0), "queue_extra_hours")
        observed = _number(shipment["observed_delay_hours"], "observed_delay_hours")
        arrival_extra = _number(scenario.get("arrival_delay_hours", 0), "arrival_delay_hours")
        override = scenario.get("wind_mps")
        wind = _number(weather["wind_mps"] if override is None else override, "wind_mps")
        rain = _number(weather["precipitation_mm"], "precipitation_mm")
        # Scenario wave override, else the weather observation; a wind-only override
        # makes the observed sea state stale, so it is re-estimated from that wind.
        wave_override = scenario.get("wave_height_m")
        wave_source = weather.get("wave_height_m") if override is None else None
        wave = _wave(wave_override if wave_override is not None else wave_source, wind)
        mass = _number(shipment["mass_kg"], "mass_kg") / 1000
        if mass <= 0:
            raise ValueError("mass_kg must be positive")
        deadline = (
            arrival.replace(hour=14, minute=0, second=0, microsecond=0)
            if cutoff is None else _timestamp(cutoff, "cutoff")
        )
        # Readiness before any model-forecast delay: known effects plus 1h handling.
        known_ready = arrival + timedelta(
            hours=observed + arrival_extra + queue + queue_extra + 1,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed prediction inputs") from exc
    except OverflowError as exc:
        raise ValueError("Baseline readiness exceeds supported timestamp range") from exc
    features = np.asarray([[queue + queue_extra, wind, rain, observed + arrival_extra, mass, wave]])
    delay = float(_delays(model.regressor, features)[0])
    try:
        processing_ready = known_ready + timedelta(hours=delay)
    except OverflowError as exc:
        raise ValueError("Readiness exceeds supported timestamp range") from exc
    ready = max(processing_ready, documents)
    if documents > deadline:
        probability = 1.0  # Known, not forecast: documents alone miss the cutoff.
    else:
        budget = (deadline - known_ready).total_seconds() / 3600
        probability = float(_miss_probabilities(
            np.asarray([delay]), np.asarray(model.residuals, dtype=float), np.asarray([budget]),
        )[0])
    factors = [
        "High wind" if wind >= 15 else "Moderate wind" if wind >= 8 else "Low wind",
        "Long terminal queue" if queue + queue_extra >= 3 else "Short terminal queue",
    ]
    if wave >= 3:
        factors.append("High sea state")
    elif wave >= 1.5:
        factors.append("Moderate sea state")
    if rain > 0:
        factors.append("Precipitation present")
    if observed + arrival_extra > 0:
        factors.append("Known arrival delay included once")
    if queue_extra > 0:
        factors.append("Scenario queue extension included once")
    if documents > processing_ready:
        factors.append("Documents determine readiness")
    if documents > deadline:
        factors.append("Documents ready after cutoff")
    factors.append("Readiness after cutoff" if ready > deadline else "Readiness within cutoff")
    factors.append("Miss probability from synthetic forecast-error spread; not guaranteed calibrated")
    return {
        "delay_hours": delay,
        "ready_at": ready.isoformat().replace("+00:00", "Z"),
        "miss_probability": probability,
        "risk": "low" if probability < 0.35 else "medium" if probability < 0.7 else "high",
        "factors": factors,
        "model_version": _VERSION,
        "data_source": "synthetic",
    }
