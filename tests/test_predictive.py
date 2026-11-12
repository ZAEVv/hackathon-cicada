"""Contract and leakage checks independent of the generated demo catalog."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import numpy as np
import pytest
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor

from backend.app.predictive import predict, train_model
from backend.app.schemas import ModelMetrics, Prediction


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
    assert isinstance(model.regressor, GradientBoostingRegressor)
    assert isinstance(model.classifier, GradientBoostingClassifier)
    assert metrics["roc_auc"] is not None
    changed = deepcopy(rows)
    for row in changed[80:]:
        row["delay_hours"] += 1000
        row["missed_connection"] = 1 - row["missed_connection"]
        row["wind_mps"] += 100
    other, _ = train_model(changed)
    probe = np.asarray([[0.5, 6, 0, 0, 20, 6], [5, 22, 0, 3, 20, 6]])
    np.testing.assert_array_equal(model.regressor.predict(probe), other.regressor.predict(probe))
    np.testing.assert_array_equal(model.classifier.predict_proba(probe), other.classifier.predict_proba(probe))
    assert rows == snapshot


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
    assert "Known documents readiness after demo cutoff; not modeled by classifier" in result["factors"]
    assert "Synthetic classifier estimate; not guaranteed calibrated" in result["factors"]
    assert result["miss_probability"] == 0
    assert result["risk"] == "low"  # Classifier output is not a routing feasibility rule.


@pytest.mark.parametrize("label", [0, 1])
@pytest.mark.parametrize("count", [2, 3, 10])
def test_small_single_class_history(inputs, label, count):
    rows = history_rows(count)
    for row in rows:
        row["missed_connection"] = label
    model, metrics = train_model(rows)
    assert metrics["roc_auc"] is None
    assert metrics["test_rows"] >= 1
    result = predict(model, *inputs)
    assert result["miss_probability"] == label
    assert result["risk"] == ("high" if label else "low")


def test_single_class_train_two_class_holdout(inputs):
    rows = history_rows(10)
    for row in rows:
        row["missed_connection"] = 0
    rows[-1]["missed_connection"] = 1
    model, metrics = train_model(rows)
    assert metrics["roc_auc"] == 0.5
    assert predict(model, *inputs)["miss_probability"] == 0


def test_only_prediction_time_features_used(inputs):
    model, _ = train_model(history_rows())
    shipment, weather, scenario = deepcopy(inputs)
    expected = predict(model, shipment, weather, scenario)
    shipment.update(actual_at="2099-01-01T00:00:00Z", delay_hours=10000,
                    missed_connection=1, events=[{"actual_at": "2099-01-01T00:00:00Z"}])
    assert predict(model, shipment, weather, scenario) == expected


def test_feature_order_cutoff_and_overrides(inputs):
    model, _ = train_model(history_rows())
    captured = []
    class Recorder:
        def predict(self, features):
            captured.append(features.copy())
            return np.array([0.0])
    object.__setattr__(model, "regressor", Recorder())
    shipment, weather, scenario = deepcopy(inputs)
    shipment.update(observed_delay_hours=1, planned_port_arrival="2026-11-13T10:00:00Z")
    scenario.update(arrival_delay_hours=3, queue_extra_hours=2, wind_mps=22)
    predict(model, shipment, weather, scenario)
    np.testing.assert_array_equal(captured[-1], [[2.5, 22, 0, 4, 20, -21]])
    shipment["id"] = "SCT-002"
    predict(model, shipment, weather, scenario)
    np.testing.assert_array_equal(captured[-1], [[2.5, 22, 0, 4, 20, 3]])


def test_same_arrival_slack_same_probability_independent_of_early_clock(inputs):
    model, _ = train_model(history_rows())
    features_seen = []
    original = model.classifier

    class Recorder:
        classes_ = original.classes_

        def predict_proba(self, features):
            features_seen.append(features.copy())
            return original.predict_proba(features)

    object.__setattr__(model, "classifier", Recorder())
    early = predict(model, *inputs, now="2026-11-12T08:00:00Z")
    later = predict(model, *inputs, now="2026-11-12T10:30:00Z")
    assert early["miss_probability"] == later["miss_probability"]
    np.testing.assert_array_equal(features_seen[0], features_seen[1])
    assert features_seen[0][0, -1] == 3  # 14:00 - (10:00 arrival + 1h handling).
    predict(model, *inputs, now="2026-11-12T12:00:00Z")
    assert features_seen[-1][0, -1] == 2  # Clock consumes slack only after baseline.


def test_documents_beyond_cutoff_dont_override_classifier(inputs):
    model, _ = train_model(history_rows())
    shipment, weather, scenario = deepcopy(inputs)
    before = predict(model, shipment, weather, scenario)
    shipment["documents_ready_at"] = "2026-11-12T20:00:00Z"
    after = predict(model, shipment, weather, scenario)
    assert after["ready_at"] == shipment["documents_ready_at"]
    assert after["miss_probability"] == before["miss_probability"]
    assert after["risk"] == before["risk"]
    assert "Known documents readiness after demo cutoff; not modeled by classifier" in after["factors"]


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
    assert normal["delay_hours"] < 2
    assert normal["ready_at"] < "2026-11-12T14:00:00Z"
    assert storm["ready_at"] > "2026-11-12T14:00:00Z"


@pytest.mark.parametrize("probability,risk", [(0, "low"), (0.349, "low"), (0.35, "medium"),
                                           (0.699, "medium"), (0.7, "high"), (1, "high")])
def test_risk_thresholds(inputs, probability, risk):
    model, _ = train_model(history_rows(2))
    class ConstantProbability:
        classes_ = np.array([0, 1])

        def predict_proba(self, features):
            return np.array([[1 - probability, probability]])
    object.__setattr__(model, "classifier", ConstantProbability())
    assert predict(model, *inputs)["risk"] == risk


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
