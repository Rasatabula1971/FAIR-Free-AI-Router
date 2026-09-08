import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator

from fair.quality.arithmetic import calculate
from fair.schemas.domain import DTO


class ArithmeticValidation(DTO):
    kind: Literal["arithmetic"]
    expression: str = Field(min_length=1, max_length=128)

    @field_validator("expression")
    @classmethod
    def bounded_expression(cls, value):
        calculate(value)
        return value


class ReferenceValidation(DTO):
    kind: Literal["reference_json"]
    expected: JsonValue

    @field_validator("expected")
    @classmethod
    def bounded_reference(cls, value):
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (ValueError, RecursionError) as error:
            raise ValueError("Reference must be finite JSON") from error
        if len(encoded) > 20000:
            raise ValueError("Reference exceeds validation budget")
        return value


ValidationContract = Annotated[
    ArithmeticValidation | ReferenceValidation, Field(discriminator="kind")
]


class Evidence(DTO):
    source_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
