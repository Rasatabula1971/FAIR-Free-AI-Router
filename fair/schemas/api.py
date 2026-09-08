from typing import Literal

from pydantic import Field

from fair.schemas.domain import DTO, Attempt, Capability, PrivacyClass


class SolveRequest(DTO):
    client_id: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=100_000)
    task_type: str | None = Field(default=None, max_length=64)
    quality_level: Literal["commodity", "standard", "advanced", "high_impact_support"] = "standard"
    privacy_class: PrivacyClass = "PUBLIC"
    expected_schema: dict | None = None
    required_capabilities: set[Capability] = Field(default_factory=set)
    freshness_required: bool = False


class SolveResponse(DTO):
    request_id: str
    status: Literal["ESCALATION_REQUIRED", "FAILED"]
    reason_code: str
    attempts: list[Attempt]
    minimum_required: float
    best_quality_score: float | None = None
    verification_state: Literal["UNVERIFIED"] = "UNVERIFIED"
    recommended_capability: str = "VALIDATED_FREE_MODEL_OR_HOST_REVIEW"
    paid_inference_executed: Literal[False] = False
