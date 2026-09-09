from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fair.quality.version import ENGINE_VERSION


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
    UNVERIFIED = "UNVERIFIED"


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
    independence_group: str | None = Field(default=None, min_length=1, max_length=128)
    model_revision: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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


class Citation(DTO):
    source_id: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=10000)


class Assertion(DTO):
    subject: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=1024)


class NormalizedModelResponse(DTO):
    provider_id: str
    model_id: str
    text: str = Field(max_length=100000)
    finish_reason: str = "stop"
    citations: list[Citation] = Field(default_factory=list, max_length=50)
    assertions: list[Assertion] = Field(default_factory=list, max_length=50)


class ProviderHealth(DTO):
    provider_id: str
    state: ProviderState
    source: Literal["OBSERVED", "OFFLINE_FIXTURE"]


class QuotaSnapshot(DTO):
    provider_id: str
    quota_unit: Literal["requests"] = "requests"
    quota_limit: int | None = Field(default=None, ge=0)
    quota_remaining_estimate: int | None = Field(default=None, ge=0)
    reset_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ClaimCheck(DTO):
    claim_id: str
    status: Literal[
        "SUPPORTED",
        "CONTRADICTED",
        "UNSUPPORTED",
        "CONFLICTING_EVIDENCE",
        "INVALID_PROVENANCE",
        "MISSING",
        "ABSTAINED",
        "INSUFFICIENT_EVIDENCE",
    ]
    evidence_count: int = Field(ge=0)


class SourceCheck(DTO):
    source_id: str
    status: Literal[
        "REVIEWED",
        "REVIEW_UNAVAILABLE",
        "CONTENT_MISMATCH",
        "REJECTED",
        "EXPIRED",
        "STALE",
        "FUTURE_REVIEW",
        "SOURCE_CLASS_DISALLOWED",
    ]


class SourcePolicyReport(DTO):
    state: Literal["NOT_REQUESTED", "PASSED", "BLOCKED", "SERVICE_FAILED"] = "NOT_REQUESTED"
    checked_at: datetime | None = None
    checks: list[SourceCheck] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    independent_origins: dict[str, int] = Field(default_factory=dict)
    policy_fingerprint: str | None = None


class BenchmarkCheck(DTO):
    provider_id: str
    model_id: str
    task_class: str
    state: Literal[
        "NOT_REQUESTED",
        "UNAVAILABLE",
        "FIXTURE",
        "UNREVIEWED",
        "VERSION_MISMATCH",
        "EXPIRED",
        "INSUFFICIENT",
        "VALIDATION_GAP",
        "FALSE_ACCEPTANCE",
        "BELOW_THRESHOLD",
        "PASSED",
        "SERVICE_FAILED",
    ] = "NOT_REQUESTED"
    checked_at: datetime | None = None
    calibration_samples: int = 0
    holdout_samples: int = 0
    conservative_score: float | None = None
    minimum_required: float | None = None
    benchmark_fingerprint: str | None = None


class QualityReport(DTO):
    overall_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    hard_reject: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    verification_state: Literal[
        "UNVERIFIED",
        "STRUCTURE_VALIDATED",
        "DETERMINISTIC_ARITHMETIC",
        "HOST_REFERENCE_MATCH",
        "SOURCE_DATA_MATCH",
        "STRUCTURED_CLAIMS_SUPPORTED",
        "BOUNDED_CODE_TESTS",
        "NATIVE_CODE_TESTS",
    ] = "UNVERIFIED"
    validator_results: dict[str, str] = Field(default_factory=dict)
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    source_policy: SourcePolicyReport = Field(default_factory=SourcePolicyReport)
    engine_version: str = ENGINE_VERSION
    validation_fingerprint: str | None = None


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
    role: Literal["PRIMARY", "CROSS_CHECK"] = "PRIMARY"


class CrossCheckReport(DTO):
    required: bool = False
    state: Literal[
        "NOT_REQUESTED",
        "NOT_RUN",
        "UNAVAILABLE",
        "PASSED",
        "REJECTED",
        "DISAGREEMENT",
        "STOPPED",
        "SERVICE_FAILED",
        "SOURCE_BLOCKED",
        "BENCHMARK_BLOCKED",
    ] = "NOT_REQUESTED"
    primary_attempt_number: int | None = None
    verification_attempt_number: int | None = None
    attempts_count: int = 0
    agreement_basis: Literal["NOT_ASSESSED", "EXACT_VALUE", "HOST_TEST_CASES"] = "NOT_ASSESSED"
