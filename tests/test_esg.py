"""Unit and dimensional tests for the frozen ESG contract.

The hand calculations use the contract definition
``kg CO2e = (mass_kg / 1000) * distance_km * kg CO2e/(tonne * km)``.
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "backend" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from esg import (  # noqa: E402
    DISCLAIMER,
    FACTORS_KG_PER_TONNE_KM,
    FACTOR_VERSION,
    calculate_emissions,
)
from schemas import Emissions  # noqa: E402


def _leg(mode: str, distance_km: float, **overrides: object) -> dict:
    """Build a complete Leg-shaped dict so tests exercise the routing input."""

    leg = {
        "service_id": f"{mode}-demo",
        "origin_id": "origin",
        "destination_id": "destination",
        "mode": mode,
        "departure_at": "2026-11-12T18:00:00Z",
        "arrival_at": "2026-11-13T18:00:00Z",
        "cutoff_at": "2026-11-12T14:00:00Z",
        "distance_km": distance_km,
        "cost_usd": 1000.0,
        "co2_kg": -999.0,  # deliberately stale: ESG must recompute it
    }
    leg.update(overrides)
    return leg


def test_hand_calculated_rail_leg_uses_tonne_kilometre_dimension() -> None:
    # 20,000 kg = 20 t; 20 t * 1,000 km * 0.022 kg/(t*km) = 440 kg CO2e.
    result = calculate_emissions([_leg("rail", 1000.0)], 20_000.0)

    assert result["by_leg_kg"] == [pytest.approx(440.0)]
    assert result["total_kg"] == pytest.approx(440.0)
    assert result["factor_version"] == "demo-v1"


def test_contract_example_sea_and_road_legs_sum_to_942_kg() -> None:
    # Sea: 20 t * 600 km * 0.016 = 192 kg.
    # Road: 20 t * 357.14285714285717 km * 0.105 = 750 kg.
    result = calculate_emissions(
        [_leg("sea", 600.0), _leg("road", 357.14285714285717)],
        20_000.0,
    )

    assert result["by_leg_kg"] == [
        pytest.approx(192.0),
        pytest.approx(750.0),
    ]
    assert result["total_kg"] == pytest.approx(942.0)


def test_mixed_modes_preserve_input_order_and_total_is_sum_of_legs() -> None:
    legs = [
        _leg("rail", 1000.0, service_id="rail-main"),
        _leg("sea", 600.0, service_id="sea-main"),
        _leg("road", 357.14285714285717, service_id="road-final"),
    ]

    result = calculate_emissions(legs, 20_000.0)

    assert len(result["by_leg_kg"]) == 3
    assert result["by_leg_kg"] == [
        pytest.approx(440.0),
        pytest.approx(192.0),
        pytest.approx(750.0),
    ]
    assert result["total_kg"] == pytest.approx(sum(result["by_leg_kg"]))
    assert result["total_kg"] == pytest.approx(1382.0)


def test_output_matches_frozen_emissions_schema_and_is_json_serializable() -> None:
    result = calculate_emissions([_leg("road", 100.0)], 20_000.0)

    validated = Emissions.model_validate(result)
    assert validated.total_kg == pytest.approx(210.0)
    assert validated.by_leg_kg == [pytest.approx(210.0)]
    assert validated.factor_version == FACTOR_VERSION
    assert validated.factors_kg_per_tonne_km == FACTORS_KG_PER_TONNE_KM
    assert validated.disclaimer == DISCLAIMER

    # Domain modules exchange JSON-compatible dictionaries.
    round_tripped = json.loads(json.dumps(result))
    assert round_tripped["total_kg"] == pytest.approx(210.0)


def test_empty_legs_are_allowed_and_return_zero() -> None:
    result = calculate_emissions([], 20_000.0)

    assert result["total_kg"] == 0.0
    assert result["by_leg_kg"] == []
    assert Emissions.model_validate(result).total_kg == 0.0


def test_zero_mass_and_zero_distance_are_valid_zero_boundaries() -> None:
    zero_mass = calculate_emissions(
        [_leg("rail", 1000.0), _leg("sea", 600.0)], 0.0
    )
    assert zero_mass["total_kg"] == 0.0
    assert zero_mass["by_leg_kg"] == [0.0, 0.0]

    zero_distance = calculate_emissions([_leg("road", 0.0)], 20_000.0)
    assert zero_distance["total_kg"] == 0.0
    assert zero_distance["by_leg_kg"] == [0.0]

    assert calculate_emissions([], 0.0)["total_kg"] == 0.0


def test_mass_conversion_scales_linearly_in_kilograms() -> None:
    # A 1,000 kg shipment is exactly one tonne.
    one_tonne = calculate_emissions([_leg("rail", 500.0)], 1_000.0)
    five_tonnes = calculate_emissions([_leg("rail", 500.0)], 5_000.0)
    twenty_tonnes = calculate_emissions([_leg("rail", 500.0)], 20_000.0)

    assert one_tonne["total_kg"] == pytest.approx(1.0 * 500.0 * 0.022)
    assert five_tonnes["total_kg"] == pytest.approx(5.0 * 500.0 * 0.022)
    assert twenty_tonnes["total_kg"] == pytest.approx(20.0 * 500.0 * 0.022)
    assert twenty_tonnes["total_kg"] == pytest.approx(20.0 * one_tonne["total_kg"])


def test_distance_scales_linearly_in_kilometres() -> None:
    short = calculate_emissions([_leg("sea", 250.0)], 12_000.0)
    long = calculate_emissions([_leg("sea", 1000.0)], 12_000.0)

    assert long["total_kg"] == pytest.approx(4.0 * short["total_kg"])


def test_factors_version_and_disclaimer_are_frozen_illustrative_values() -> None:
    result = calculate_emissions([_leg("rail", 1.0)], 1_000.0)

    assert result["factor_version"] == "demo-v1"
    assert result["factors_kg_per_tonne_km"] == {
        "rail": 0.022,
        "road": 0.105,
        "sea": 0.016,
    }
    assert result["disclaimer"] == (
        "Illustrative transport-only estimate; not ISO/GLEC certified."
    )
    assert "not ISO/GLEC certified" in result["disclaimer"]


def test_recomputes_from_mode_and_distance_ignoring_stale_co2_field() -> None:
    leg = _leg("road", 100.0, co2_kg=999_999.0, extra_field="ignored")

    result = calculate_emissions([leg], 20_000.0)

    assert result["by_leg_kg"] == [pytest.approx(210.0)]
    assert result["total_kg"] == pytest.approx(210.0)


def test_inputs_are_not_mutated_and_returned_factor_map_is_independent() -> None:
    legs = [_leg("rail", 1000.0), _leg("sea", 600.0)]

    def snapshot() -> tuple[list[dict], float]:
        return copy.deepcopy(legs), copy.deepcopy(FACTORS_KG_PER_TONNE_KM)

    legs_before, factors_before = snapshot()
    first = calculate_emissions(legs, 20_000.0)

    assert legs == legs_before
    assert FACTORS_KG_PER_TONNE_KM == factors_before

    first["factors_kg_per_tonne_km"]["rail"] = 999.0
    first["by_leg_kg"].append(123.0)
    second = calculate_emissions(legs, 20_000.0)

    assert second["factors_kg_per_tonne_km"]["rail"] == 0.022
    assert len(second["by_leg_kg"]) == 2
    assert FACTORS_KG_PER_TONNE_KM["rail"] == 0.022


@pytest.mark.parametrize("mass_kg", [-1, -0.001, float("nan"), float("inf"), float("-inf")])
def test_invalid_mass_is_rejected(mass_kg: float) -> None:
    with pytest.raises(ValueError, match="mass_kg"):
        calculate_emissions([_leg("rail", 100.0)], mass_kg)


@pytest.mark.parametrize(
    "distance_km", [-1, -0.001, float("nan"), float("inf"), float("-inf")]
)
def test_invalid_distance_is_rejected(distance_km: float) -> None:
    with pytest.raises(ValueError, match=r"legs\[0\]\.distance_km"):
        calculate_emissions([_leg("rail", distance_km)], 20_000.0)


@pytest.mark.parametrize("mode", ["air", "SEA", "", "unknown"])
def test_unknown_modes_are_rejected(mode: str) -> None:
    with pytest.raises(ValueError, match=r"legs\[0\]\.mode"):
        calculate_emissions([_leg(mode, 100.0)], 20_000.0)


@pytest.mark.parametrize("mode", [None, 1, True, ["sea"]])
def test_non_string_modes_are_rejected(mode: object) -> None:
    with pytest.raises(ValueError, match=r"legs\[0\]\.mode"):
        calculate_emissions([_leg(mode, 100.0)], 20_000.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["mode", "distance_km"])
def test_missing_required_leg_fields_are_rejected(field: str) -> None:
    leg = _leg("rail", 100.0)
    del leg[field]

    with pytest.raises(ValueError, match=field):
        calculate_emissions([leg], 20_000.0)


def test_invalid_leg_container_types_are_rejected() -> None:
    with pytest.raises(ValueError, match="legs must be a list"):
        calculate_emissions((_leg("rail", 100.0),), 20_000.0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"legs\[0\] must be a dict"):
        calculate_emissions(["not-a-leg"], 20_000.0)  # type: ignore[list-item]


@pytest.mark.parametrize("bad_value", [True, False, "1000", None])
def test_non_numeric_mass_and_distance_are_rejected(bad_value: object) -> None:
    with pytest.raises(ValueError, match="mass_kg"):
        calculate_emissions([_leg("rail", 100.0)], bad_value)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"legs\[0\]\.distance_km"):
        calculate_emissions(
            [_leg("rail", bad_value)], 20_000.0  # type: ignore[arg-type]
        )


def test_integer_inputs_are_accepted_as_json_numbers() -> None:
    result = calculate_emissions([_leg("rail", 1000)], 20_000)

    assert result["total_kg"] == pytest.approx(440.0)
    assert math.isfinite(result["total_kg"])


def test_invalid_leg_is_rejected_even_if_earlier_legs_are_valid() -> None:
    legs = [
        _leg("rail", 100.0),
        _leg("air", 100.0),
    ]

    with pytest.raises(ValueError, match=r"legs\[1\]\.mode"):
        calculate_emissions(legs, 20_000.0)
