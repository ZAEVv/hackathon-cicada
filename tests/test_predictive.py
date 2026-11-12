"""Contract and leakage checks independent of the generated demo catalog."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

from backend.app.predictive import predict, train_model
from backend.app.schemas import ModelMetrics, Prediction

CALIBRATION_FACTOR = "Miss probability from synthetic forecast-error spread; not guaranteed calibrated"


class ZeroDelay:
    """Regressor stub: no additional delay, records the features it was given."""

    def __init__(self):
        self.seen = []

    def predict(self, features):
        self.seen.append(features.copy())
        return np.zeros(len(features))


def history_rows(count=100):
    rng = np.random.default_rng(42)
    start = datetime(2026, 8, 15, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        wind = float(rng.uniform(0, 24))
        queue = float(rng.uniform(0, 5))
        delay = 0.2 + max(0, wind - 8) ** 2 / 30 + queue / 10
        rows.append({
            "shipment_id": f"history-{i}",
            "observed_at": (start + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            "queue_hours": queue, "wind_mps": wind, "precipitation_mm": 0,
            "observed_delay_hours": 0, "mass_tonnes": 20, "hours_to_cutoff": 4,
            "delay_hours": delay, "missed_connection": int(delay + queue >= 4),
        })
    return rows


@pytest.fixture
def inputs():
    return (
        {"id": "SCT-001", "planned_port_arrival": "2026-11-12T10:00:00Z",
         "documents_ready_at": "2026-11-12T11:00:00Z", "queue_hours": 0.5,
         "observed_delay_hours": 0, "mass_kg": 20000},
        {"wind_mps": 6, "precipitation_mm": 0},
        {"kind": "none", "arrival_delay_hours": 0, "queue_extra_hours": 0, "wind_mps": None},
    )


def test_chronological_split_baseline_and_no_holdout_leakage():
    rows = history_rows()
    snapshot = deepcopy(rows)
    model, metrics = train_model(list(reversed(rows)))
    ModelMetrics.model_validate(metrics)
    assert (metrics["train_rows"], metrics["test_rows"]) == (80, 20)
    assert metrics["split_at"] == rows[80]["observed_at"]
    baseline = np.median([row["delay_hours"] for row in rows[:80]])
    expected = np.mean([abs(row["delay_hours"] - baseline) for row in rows[80:]])
    assert metrics["baseline_mae_hours"] == pytest.approx(expected)
    assert isinstance(model.regressor, HistGradientBoostingRegressor)
    assert len(model.residuals) == 20  # last quarter of the 80 training rows
    assert metrics["roc_auc"] is not None
    changed = deepcopy(rows)
    for row in changed[80:]:
        row["delay_hours"] += 1000
        row["missed_connection"] = 1 - row["missed_connection"]
        row["wind_mps"] += 100
        row["hours_to_cutoff"] += 100
    other, _ = train_model(changed)
    probe = np.asarray([[0.5, 6, 0, 0, 20], [5, 22, 0, 3, 20]])
    np.testing.assert_array_equal(model.regressor.predict(probe), other.regressor.predict(probe))
    np.testing.assert_array_equal(model.residuals, other.residuals)
    assert rows == snapshot


def test_forecast_is_monotonic_in_wind_queue_and_known_delay():
    model, _ = train_model(history_rows())
    for column, values in ((0, np.linspace(0, 12, 25)), (1, np.linspace(0, 40, 41)),
                           (2, np.linspace(0, 30, 16)), (3, np.linspace(0, 8, 17))):
        probe = np.tile([1.0, 10.0, 0.0, 0.0, 20.0], (len(values), 1))
        probe[:, column] = values
        assert (np.diff(model.regressor.predict(probe)) >= 0).all()


def test_repeatable_schema_normal_and_storm(inputs):
    rows = history_rows()
    first, metrics = train_model(rows)
    second, other_metrics = train_model(rows)
    assert metrics == other_metrics
    snapshot = deepcopy(inputs)
    normal = predict(first, *inputs)
    Prediction.model_validate(normal)
    assert normal == predict(second, *inputs)
    assert normal["delay_hours"] < 2
    assert normal["ready_at"] < "2026-11-12T14:00:00Z"
    storm = {"kind": "storm", "arrival_delay_hours": 3, "queue_extra_hours": 2, "wind_mps": 22}
    disrupted = predict(first, inputs[0], inputs[1], storm)
    assert disrupted["ready_at"] > "2026-11-12T14:00:00Z"
    assert 0 <= disrupted["miss_probability"] <= 1
    assert math.isfinite(disrupted["miss_probability"])
    assert inputs == snapshot


def test_known_effects_counted_once_and_documents_are_maximum(inputs):
    rows = history_rows(10)
    for row in rows:
        row["delay_hours"] = 0.25
        row["missed_connection"] = 0
    model, _ = train_model(rows)
    shipment, weather, scenario = deepcopy(inputs)
    shipment["observed_delay_hours"] = 1
    scenario.update(arrival_delay_hours=2, queue_extra_hours=1.5)
    result = predict(model, shipment, weather, scenario)
    # 10:00 + 1 observed + 2 scenario + .5 queue + 1.5 extra + 1 handling + .25 predicted.
    assert result["ready_at"] == "2026-11-12T16:15:00Z"
    assert result["delay_hours"] == pytest.approx(0.25)
    shipment["documents_ready_at"] = "2026-11-13T03:00:00Z"
    result = predict(model, shipment, weather, scenario)
    assert result["ready_at"] == shipment["documents_ready_at"]
    assert "Documents determine readiness" in result["factors"]
    assert "Documents ready after cutoff" in result["factors"]
    assert CALIBRATION_FACTOR in result["factors"]
    assert result["miss_probability"] == 1
    assert result["risk"] == "high"  # Late documents miss the cutoff whatever the model says.


@pytest.mark.parametrize("label", [0, 1])
@pytest.mark.parametrize("count", [2, 3, 10])
def test_small_single_class_history(inputs, label, count):
    rows = history_rows(count)
    for row in rows:
        row["missed_connection"] = label
    model, metrics = train_model(rows)
    assert metrics["roc_auc"] is None
    assert metrics["test_rows"] >= 1
    assert len(model.residuals) >= 1 and np.isfinite(model.residuals).all()
    # Labels only score the holdout; they never drive the forecast.
    relabelled = deepcopy(rows)
    for row in relabelled:
        row["missed_connection"] = 1 - label
    result = predict(model, *inputs)
    Prediction.model_validate(result)
    assert result == predict(train_model(relabelled)[0], *inputs)


def test_two_class_holdout_reports_auc(inputs):
    rows = history_rows(10)
    for row in rows:
        row["missed_connection"] = 0
    rows[-1]["missed_connection"] = 1
    model, metrics = train_model(rows)
    assert 0 <= metrics["roc_auc"] <= 1
    Prediction.model_validate(predict(model, *inputs))


def test_only_prediction_time_features_used(inputs):
    model, _ = train_model(history_rows())
    shipment, weather, scenario = deepcopy(inputs)
    expected = predict(model, shipment, weather, scenario)
    shipment.update(actual_at="2099-01-01T00:00:00Z", delay_hours=10000,
                    missed_connection=1, events=[{"actual_at": "2099-01-01T00:00:00Z"}])
    assert predict(model, shipment, weather, scenario) == expected


def test_feature_order_cutoff_and_overrides(inputs):
    model, _ = train_model(history_rows())
    recorder = ZeroDelay()
    object.__setattr__(model, "regressor", recorder)
    object.__setattr__(model, "residuals", np.array([-1.0, 0.0, 0.4, 0.6]))
    shipment, weather, scenario = deepcopy(inputs)
    shipment.update(observed_delay_hours=1, planned_port_arrival="2026-11-13T10:00:00Z")
    scenario.update(arrival_delay_hours=3, queue_extra_hours=2, wind_mps=22)
    # Known readiness: 10:00 + 1 + 3 observed/scenario + .5 + 2 queue + 1 handling = 17:30.
    default = predict(model, shipment, weather, scenario)
    np.testing.assert_array_equal(recorder.seen[-1], [[2.5, 22, 0, 4, 20]])
    assert default["miss_probability"] == 1  # Fallback cutoff: arrival day 14:00Z.
    assert "Readiness after demo cutoff" in default["factors"]
    planned = predict(model, shipment, weather, scenario, cutoff="2026-11-13T18:00:00Z")
    np.testing.assert_array_equal(recorder.seen[-1], recorder.seen[0])  # Cutoff is not a delay driver.
    assert planned["ready_at"] == default["ready_at"] == "2026-11-13T17:30:00Z"
    assert planned["miss_probability"] == 0.25  # Only the +0.6h error exceeds the 0.5h budget.
    assert "Readiness within demo cutoff" in planned["factors"]
    with pytest.raises(ValueError):
        predict(model, shipment, weather, scenario, cutoff="2026-11-13T18:00:00")


def test_forecast_does_not_depend_on_prediction_clock(inputs):
    model, _ = train_model(history_rows())
    early = predict(model, *inputs, now="2026-11-12T08:00:00Z")
    assert early == predict(model, *inputs, now="2026-11-12T10:30:00Z")
    assert early == predict(model, *inputs, now="2026-11-12T12:00:00Z")


def test_probability_agrees_with_readiness(inputs):
    model, _ = train_model(history_rows())
    object.__setattr__(model, "regressor", ZeroDelay())
    object.__setattr__(model, "residuals", np.zeros(5))
    shipment, weather, scenario = deepcopy(inputs)
    on_time = predict(model, shipment, weather, scenario)  # Ready 11:30, cutoff 14:00.
    assert (on_time["miss_probability"], on_time["risk"]) == (0, "low")
    scenario["queue_extra_hours"] = 3  # Ready 14:30.
    late = predict(model, shipment, weather, scenario)
    assert (late["miss_probability"], late["risk"]) == (1, "high")
    assert "Readiness after demo cutoff" in late["factors"]


def test_documents_beyond_cutoff_force_a_miss(inputs):
    model, _ = train_model(history_rows())
    shipment, weather, scenario = deepcopy(inputs)
    shipment["documents_ready_at"] = "2026-11-12T20:00:00Z"
    after = predict(model, shipment, weather, scenario)
    assert after["ready_at"] == shipment["documents_ready_at"]
    assert after["miss_probability"] == 1
    assert after["risk"] == "high"
    assert "Documents ready after cutoff" in after["factors"]
    relaxed = predict(model, shipment, weather, scenario, cutoff="2026-11-12T20:00:00Z")
    assert "Documents ready after cutoff" not in relaxed["factors"]  # Equality is on time.


def test_readiness_exactly_at_cutoff_is_allowed(inputs):
    rows = history_rows(10)
    for row in rows:
        row.update(delay_hours=0.25, missed_connection=0)
    model, _ = train_model(rows)
    shipment, weather, scenario = deepcopy(inputs)
    shipment["queue_hours"] = 2.75
    result = predict(model, shipment, weather, scenario)
    assert result["ready_at"] == "2026-11-12T14:00:00Z"
    assert "Readiness within demo cutoff" in result["factors"]
    assert result["miss_probability"] == 0


@pytest.mark.parametrize("change", [
    {"delay_hours": -1}, {"queue_hours": float("nan")}, {"wind_mps": float("inf")},
    {"mass_tonnes": 0}, {"missed_connection": 2},
    {"observed_at": "2026-11-12T08:00:00Z"}, {"observed_at": "2026-10-01T08:00:00"},
])
def test_invalid_history(change):
    rows = history_rows(10)
    rows[0].update(change)
    with pytest.raises(ValueError):
        train_model(rows)


def test_missing_duplicate_and_empty_history():
    for rows in ([], history_rows(1), [{}] * 2):
        with pytest.raises(ValueError):
            train_model(rows)
    rows = history_rows(10)
    rows[1]["shipment_id"] = rows[0]["shipment_id"]
    with pytest.raises(ValueError):
        train_model(rows)


def test_tied_observation_times_allow_independent_rows(inputs):
    rows = history_rows(10)
    for row in rows:
        row["observed_at"] = rows[0]["observed_at"]
    model, metrics = train_model(rows)
    assert metrics["train_rows"] == 8
    Prediction.model_validate(predict(model, *inputs))


def test_generated_demo_history_and_cutoff():
    from backend.app.data import get_weather, load_demo_data

    catalog = load_demo_data()
    model, metrics = train_model(catalog["history"])
    shipment = next(row for row in catalog["shipments"] if row["id"] == "SCT-001")
    weather = get_weather("makhachkala")
    normal = predict(model, shipment, weather, {"kind": "none"})
    storm = predict(model, shipment, weather, {
        "kind": "storm", "arrival_delay_hours": 3, "queue_extra_hours": 2, "wind_mps": 22,
    })
    assert metrics["model_mae_hours"] < metrics["baseline_mae_hours"]
    assert metrics["roc_auc"] > 0.8
    assert normal["delay_hours"] < 2
    assert normal["ready_at"] < "2026-11-12T14:00:00Z"
    assert normal["risk"] == "low"
    assert storm["ready_at"] > "2026-11-12T14:00:00Z"
    assert (storm["miss_probability"], storm["risk"]) == (1, "high")


def test_demo_cutoffs_come_from_planned_services():
    from backend.app.data import get_weather, load_demo_data
    from backend.app.routing import compute_routes, planned_cutoff, scenario_profile

    catalog = load_demo_data()
    model, _ = train_model(catalog["history"])
    cutoffs = {row["id"]: planned_cutoff(row, catalog["services"]) for row in catalog["shipments"]}
    assert cutoffs["SCT-001"] == "2026-11-12T14:00:00Z"  # sea-main 18:00Z minus 4h.
    assert cutoffs["SCT-002"] == "2026-11-12T17:30:00Z"  # transfer-olya 18:00Z minus 0.5h.
    for shipment in catalog["shipments"]:
        weather = get_weather(shipment["current_location_id"])
        for kind in ("none", "storm", "congestion"):
            profile = scenario_profile(kind)
            forecast = predict(model, shipment, weather, profile, cutoff=cutoffs[shipment["id"]])
            route = compute_routes(shipment, catalog["ports"], catalog["services"], forecast, profile)
            # The point forecast and the scheduler judge the same departure.
            late = forecast["ready_at"] > cutoffs[shipment["id"]]
            assert route["current_route"]["missed_connection"] == late
            assert ("Readiness after demo cutoff" in forecast["factors"]) == late


def test_planned_cutoff_without_a_planned_departure():
    from backend.app.data import load_demo_data
    from backend.app.routing import planned_cutoff

    catalog = load_demo_data()
    shipment = catalog["shipments"][0]
    shipment["planned_service_ids"] = ["rail-main"]  # Nothing planned onward from the port.
    assert planned_cutoff(shipment, catalog["services"]) is None


@pytest.mark.parametrize("probability,risk", [(0, "low"), (0.349, "low"), (0.35, "medium"),
                                           (0.699, "medium"), (0.7, "high"), (1, "high")])
def test_risk_thresholds(inputs, probability, risk):
    model, _ = train_model(history_rows(2))
    object.__setattr__(model, "regressor", ZeroDelay())
    residuals = np.zeros(1000)
    residuals[:round(probability * 1000)] = 100.0  # That share of errors misses the cutoff.
    object.__setattr__(model, "residuals", residuals)
    result = predict(model, *inputs)
    assert result["miss_probability"] == probability
    assert result["risk"] == risk


@pytest.mark.parametrize("section,key,value", [
    (0, "mass_kg", 0), (0, "queue_hours", -1), (0, "observed_delay_hours", float("nan")),
    (0, "planned_port_arrival", "not-a-date"), (1, "wind_mps", float("inf")),
    (2, "arrival_delay_hours", -3), (2, "queue_extra_hours", -2),
])
def test_invalid_prediction(inputs, section, key, value):
    model, _ = train_model(history_rows(10))
    inputs[section][key] = value
    with pytest.raises(ValueError):
        predict(model, *inputs)


def test_invalid_bundle_and_clock(inputs):
    with pytest.raises(ValueError):
        predict(None, *inputs)
    model, _ = train_model(history_rows(10))
    with pytest.raises(ValueError):
        predict(model, *inputs, now="2026-11-12T08:00:00")
