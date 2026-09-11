import json
import keyword
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictBool, StrictInt, field_validator

from fair.constants import SECONDS_IN_DAY, SECONDS_IN_YEAR
from fair.quality.arithmetic import calculate
from fair.quality.code_validator import SAFE_BUILTINS, bounded
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


class FactKey(DTO):
    subject: str = Field(min_length=1, max_length=128)
    predicate: str = Field(min_length=1, max_length=128)
    context: str = Field(min_length=1, max_length=256)

    @field_validator("subject", "predicate", "context")
    @classmethod
    def exact_nonblank_key(cls, value):
        if value != value.strip():
            raise ValueError("Fact keys must not have surrounding whitespace")
        return value

    def key(self):
        return self.subject, self.predicate, self.context


class ClaimTarget(FactKey):
    claim_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ClaimsValidation(DTO):
    kind: Literal["grounded_claims"]
    claims: list[ClaimTarget] = Field(min_length=1, max_length=20)

    @field_validator("claims")
    @classmethod
    def unique_claims(cls, value):
        if len({claim.claim_id for claim in value}) != len(value):
            raise ValueError("Claim IDs must be unique")
        if len({claim.key() for claim in value}) != len(value):
            raise ValueError("Requested fact keys must be unique")
        return value


CodeValue = StrictInt | StrictBool | Annotated[list[StrictInt | StrictBool], Field(max_length=64)]


class FunctionCase(DTO):
    arguments: list[CodeValue] = Field(max_length=8)
    expected: CodeValue

    @field_validator("arguments")
    @classmethod
    def bounded_integers(cls, value):
        for item in value:
            bounded(item)
        return value

    @field_validator("expected")
    @classmethod
    def bounded_expected(cls, value):
        return bounded(value)


class FunctionValidation(DTO):
    kind: Literal["python_function"]
    function_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    cases: list[FunctionCase] = Field(min_length=1, max_length=32)

    @field_validator("cases")
    @classmethod
    def consistent_arity(cls, value):
        if len({len(case.arguments) for case in value}) != 1:
            raise ValueError("Function test cases must use the same argument count")
        if len(json.dumps([case.model_dump() for case in value])) > 24000:
            raise ValueError("Function test data exceeds 24000-character budget")
        return value

    @field_validator("function_name")
    @classmethod
    def valid_name(cls, value):
        if keyword.iskeyword(value) or value in SAFE_BUILTINS:
            raise ValueError("Function name must not be a keyword or reserved builtin")
        return value


class NativeFunctionValidation(FunctionValidation):
    kind: Literal["native_python_function"]


ValidationContract = Annotated[
    ArithmeticValidation
    | ReferenceValidation
    | GroundedValidation
    | ClaimsValidation
    | FunctionValidation
    | NativeFunctionValidation,
    Field(discriminator="kind"),
]


class SourcePolicy(DTO):
    max_age_seconds: int = Field(default=SECONDS_IN_DAY, ge=1, le=SECONDS_IN_YEAR)
    allowed_source_classes: set[Literal["PRIMARY", "SECONDARY"]] = Field(
        default_factory=lambda: {"PRIMARY", "SECONDARY"}, min_length=1, max_length=2
    )
    min_independent_origins: int = Field(default=1, ge=1, le=10)


class Evidence(DTO):
    source_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    review_id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
