"""Transparent, transport-only CO2e estimate for remaining route legs.

The factors are fixed illustrative constants from the frozen demo contract
(``docs/CONTRACTS.md``). They are not a certified emission-factor set and this
module makes no WTW, GLEC, ISO or regulatory-compliance claim.

Scope: only the legs passed by the caller are counted. The caller is expected
to pass the remaining journey, already excluding completed legs, terminal
handling, storage and empty repositioning.
"""

import math
from numbers import Real
from typing import Any

# kg CO2e / (tonne * km), illustrative demo values from the frozen contract.
FACTOR_VERSION = "v1"
FACTORS_KG_PER_TONNE_KM: dict[str, float] = {
    "rail": 0.022,
    "road": 0.105,
    "sea": 0.016,
}
DISCLAIMER = "Illustrative transport-only estimate; not ISO/GLEC certified."


def _finite_nonnegative(value: Any, *, field: str) -> float:
    """Return *value* as a finite, non-negative float or raise ValueError.

    ``bool`` is rejected deliberately because it is a subclass of ``int`` but
    is not a meaningful distance or mass. JSON numbers arrive as ``int`` or
    ``float``; other ``numbers.Real`` implementations (for example numpy
    scalars) are accepted and normalised to ``float``.
    """

    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite non-negative number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


def calculate_emissions(legs: list[dict], mass_kg: float) -> dict:
    """Calculate illustrative transport emissions for remaining route legs.

    Formula, per leg::

        co2_kg = (mass_kg / 1000) * distance_km * factor_kg_per_tonne_km[mode]

    ``mass_kg`` is kilograms, ``distance_km`` is kilometres and each factor is
    kg CO2e per tonne-kilometre. The result is in kg CO2e.

    An empty ``legs`` list is valid and produces zero total emissions. Zero
    mass and zero distance are valid boundaries and also produce zero for the
    affected leg. Negative or non-finite distances/mass and unknown transport
    modes are invalid and raise ``ValueError``. No rounding is applied here;
    display layers may round, but calculations must retain full precision.

    The returned dict matches ``schemas.Emissions``. ``by_leg_kg`` preserves
    the input leg order. Extra leg fields (for example ``service_id`` or a
    stale ``co2_kg``) are ignored; emissions are always recomputed from mode
    and distance.
    """

    mass_kg_value = _finite_nonnegative(mass_kg, field="mass_kg")
    if not isinstance(legs, list):
        raise ValueError("legs must be a list of dicts")

    mass_tonnes = mass_kg_value / 1000.0
    by_leg_kg: list[float] = []

    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise ValueError(f"legs[{index}] must be a dict")

        if "mode" not in leg:
            raise ValueError(f"legs[{index}].mode is required")
        mode = leg["mode"]
        if not isinstance(mode, str) or mode not in FACTORS_KG_PER_TONNE_KM:
            supported = ", ".join(sorted(FACTORS_KG_PER_TONNE_KM))
            raise ValueError(
                f"legs[{index}].mode must be one of {supported}, got {mode!r}"
            )

        if "distance_km" not in leg:
            raise ValueError(f"legs[{index}].distance_km is required")
        distance_km = _finite_nonnegative(
            leg["distance_km"], field=f"legs[{index}].distance_km"
        )

        by_leg_kg.append(mass_tonnes * distance_km * FACTORS_KG_PER_TONNE_KM[mode])

    return {
        "total_kg": sum(by_leg_kg, 0.0),
        "by_leg_kg": by_leg_kg,
        "factor_version": FACTOR_VERSION,
        "factors_kg_per_tonne_km": dict(FACTORS_KG_PER_TONNE_KM),
        "disclaimer": DISCLAIMER,
    }
