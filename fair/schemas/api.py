import json
from typing import Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field, model_validator

from fair.quality.claims import fact_index
from fair.quality.contracts import Evidence, SourcePolicy, ValidationContract
from fair.quality.grounding import grounded_result
from fair.schemas.domain import (
    DTO,
    Attempt,
    BenchmarkCheck,
    Capability,
    CrossCheckReport,
    PrivacyClass,
    QualityReport,
    SourcePolicyReport,
)


class SolveRequest(DTO):
    client_id: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=100_000)
    task_type: str | None = Field(default=None, max_length=64)
    quality_level: Literal["commodity", "standard", "advanced", "high_impact_support"] = "standard"
    privacy_class: PrivacyClass = "PUBLIC"
    expected_schema: dict | None = None
    required_capabilities: set[Capability] = Field(default_factory=set)
    freshness_required: bool = False
    cross_check_required: bool = False
    validation: ValidationContract | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    source_policy: SourcePolicy | None = None

    @model_validator(mode="after")
    def unique_sources(self):
        if self.source_policy is not None and (
            self.validation is None
            or self.validation.kind not in {"grounded_json", "grounded_claims"}
        ):
            raise ValueError("Source policies require a structured grounding contract")
        ids = [item.source_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("Evidence source IDs must be unique")
        if self.validation is not None and self.validation.kind == "grounded_claims":
            fact_index(self.evidence)
        if self.validation is not None and self.validation.kind == "grounded_json":
            try:
                expected = grounded_result(self.validation, self.evidence)
                if len(json.dumps(expected)) > 50000:
                    raise ValueError("Grounded result exceeds validation budget")
            except RecursionError as error:
                raise ValueError("Grounded source exceeds depth budget") from error
        if self.expected_schema is not None:
            try:
                Draft202012Validator.check_schema(self.expected_schema)
                encoded = json.dumps(self.expected_schema)
            except (SchemaError, RecursionError) as error:
                raise ValueError("Invalid expected JSON schema") from error
            if len(encoded) > 20000 or '"$ref"' in encoded or '"$dynamicRef"' in encoded:
                raise ValueError(
                    "Schema references and schemas over 20000 characters are unsupported"
                )
        return self


class SolveResponse(DTO):
    request_id: str
    status: Literal["ACCEPTED", "ESCALATION_REQUIRED", "FAILED"]
    reason_code: str
    attempts: list[Attempt]
    minimum_required: float
    best_quality_score: float | None = None
    verification_state: Literal[
        "UNVERIFIED",
        "DETERMINISTIC_ARITHMETIC",
        "HOST_REFERENCE_MATCH",
        "SOURCE_DATA_MATCH",
        "STRUCTURED_CLAIMS_SUPPORTED",
        "BOUNDED_CODE_TESTS",
        "NATIVE_CODE_TESTS",
    ] = "UNVERIFIED"
    output: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    quality: QualityReport | None = None
    model_disagreement: Literal["NOT_ASSESSED", "NONE", "DETECTED"] = "NOT_ASSESSED"
    cross_check: CrossCheckReport = Field(default_factory=CrossCheckReport)
    source_policy: SourcePolicyReport = Field(default_factory=SourcePolicyReport)
    benchmark_checks: list[BenchmarkCheck] = Field(default_factory=list)
    recommended_capability: str = "VALIDATED_FREE_MODEL_OR_HOST_REVIEW"
    paid_inference_executed: Literal[False] = False
