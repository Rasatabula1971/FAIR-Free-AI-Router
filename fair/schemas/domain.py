from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AccessClass(StrEnum):
    FREE_RECURRING = "FREE_RECURRING"
    FREE_DYNAMIC = "FREE_DYNAMIC"
    FREE_LOCAL = "FREE_LOCAL"


class ProviderState(StrEnum):
    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"
    QUOTA_PRESSURE = "QUOTA_PRESSURE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    DEGRADED = "DEGRADED"
    OUTAGE = "OUTAGE"
    DISABLED = "DISABLED"
    TERMS_REVIEW = "TERMS_REVIEW"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"


class RequestStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROFILED = "PROFILED"
    ROUTING = "ROUTING"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    ACCEPTED = "ACCEPTED"
    UNVERIFIED_RESULT = "UNVERIFIED_RESULT"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    INFRA_FAILURE = "INFRA_FAILURE"
    QUOTA_FAILURE = "QUOTA_FAILURE"
    QUALITY_FAILURE = "QUALITY_FAILURE"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    PRIVACY_BLOCK = "PRIVACY_BLOCK"
    TERMS_BLOCK = "TERMS_BLOCK"
    CANCELLED = "CANCELLED"


PrivacyClass = Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
Capability = Literal[
    "reasoning", "coding", "vision", "tool_calling", "structured_output", "embeddings"
]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ModelDescriptor(DTO):
    model_id: str
    context_window: int = Field(gt=0)
    capabilities: set[Capability] = Field(default_factory=set)
    active: bool = True


class ProviderSpec(DTO):
    provider_id: str
    access_class: AccessClass
    status: ProviderState = ProviderState.TERMS_REVIEW
    current_access_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    requires_paid_subscription: bool = True
    requires_credit_purchase: bool = True
    auto_billing_required: bool = True
    programmatic_access: bool = False
    production_eligibility: bool = False
    terms_last_verified: datetime | None = None
    max_data_class: PrivacyClass = "PUBLIC"
    models: list[ModelDescriptor] = Field(default_factory=list)
    request_limit: int | None = Field(default=None, gt=0)


class TaskProfile(DTO):
    task_class: str
    required_capabilities: set[Capability]
    context_tokens_estimate: int
    minimum_quality_score: float
    requires_grounding: bool
    profile_source: Literal["RULES"] = "RULES"


class NormalizedModelRequest(DTO):
    task: str
    model_id: str
    request_id: str
    client_id: str
    task_class: str
    expected_json_schema: dict | None = None
    max_output_tokens: int = 1024


class NormalizedModelResponse(DTO):
    provider_id: str
    model_id: str
    text: str
    finish_reason: str = "stop"


class QualityReport(DTO):
    overall_score: float | None = None
    hard_reject: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    verification_state: Literal["UNVERIFIED", "STRUCTURE_VALIDATED"] = "UNVERIFIED"


class Attempt(DTO):
    attempt_number: int
    provider_id: str
    model_id: str
    selection_score: float
    quota_remaining: int | None
    disposition: AttemptDisposition
    latency_ms: float
    error_type: str | None = None
    quality: QualityReport | None = None
