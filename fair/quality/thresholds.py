from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

LEVELS = ("commodity", "standard", "advanced", "high_impact_support")
DEFAULT_THRESHOLDS = dict(zip(LEVELS, (75, 82, 88, 92), strict=True))
# Score for an answer that passed only the expected_schema check. Schema conformance
# proves shape, not truth: clears the default commodity/standard thresholds and fails
# advanced/high_impact_support, which keep requiring a deterministic contract.
STRUCTURE_VALIDATED_SCORE = 85.0
ThresholdMap = dict[
    Literal["commodity", "standard", "advanced", "high_impact_support"],
    Annotated[float, Field(strict=True, gt=0, le=100, allow_inf_nan=False)],
]


def validate_thresholds(value):
    thresholds = TypeAdapter(ThresholdMap).validate_python(value)
    ordered = [thresholds[level] for level in LEVELS if level in thresholds]
    if not ordered or ordered != sorted(ordered):
        raise ValueError("Quality thresholds must be nonempty and nondecreasing by quality level")
    return thresholds
