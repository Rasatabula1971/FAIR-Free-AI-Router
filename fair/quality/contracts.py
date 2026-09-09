import json
import keyword
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictBool, StrictInt, field_validator

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


class GroundedField(DTO):
    output_key: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    pointer: str = Field(max_length=512)


class GroundedValidation(DTO):
    kind: Literal["grounded_json"]
    fields: list[GroundedField] = Field(min_length=1, max_length=20)

    @field_validator("fields")
    @classmethod
    def distinct_keys(cls, value):
        keys = [field.output_key for field in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Grounded output keys must be unique")
        return value


class FunctionCase(DTO):
    arguments: list[StrictInt | StrictBool] = Field(max_length=8)
    expected: StrictInt | StrictBool

    @field_validator("arguments", "expected")
    @classmethod
    def bounded_integers(cls, value):
        values = value if isinstance(value, list) else [value]
        if any(type(item) is int and item.bit_length() > 256 for item in values):
            raise ValueError("Function inputs and expectations are limited to 256-bit integers")
        return value


class FunctionValidation(DTO):
    kind: Literal["python_function"]
    function_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    cases: list[FunctionCase] = Field(min_length=1, max_length=32)

    @field_validator("cases")
    @classmethod
    def consistent_arity(cls, value):
        if len({len(case.arguments) for case in value}) != 1:
            raise ValueError("Function test cases must use the same argument count")
        return value

    @field_validator("function_name")
    @classmethod
    def valid_name(cls, value):
        if keyword.iskeyword(value):
            raise ValueError("Function name must not be a keyword")
        return value


class NativeFunctionValidation(FunctionValidation):
    kind: Literal["native_python_function"]


ValidationContract = Annotated[
    ArithmeticValidation
    | ReferenceValidation
    | GroundedValidation
    | FunctionValidation
    | NativeFunctionValidation,
    Field(discriminator="kind"),
]


class Evidence(DTO):
    source_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
