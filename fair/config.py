from pydantic import Field, model_validator

from fair.constants import SECONDS_IN_DAY, completion_deadline
from fair.quality.contracts import SourcePolicy
from fair.schemas.domain import DTO


class RoutingSettings(DTO):
    cache_enabled: bool = False
    cache_ttl_seconds: int = Field(default=3600, ge=1, le=SECONDS_IN_DAY)
    cache_max_entries: int = Field(default=1000, ge=1, le=100000)
    confidence_half_life_days: int = Field(default=30, ge=1, le=365)
    drift_drop_points: float = Field(default=15, gt=0, le=100, allow_inf_nan=False)
    # Answered attempts: models that returned something the quality gate judged
    # (ACCEPTED / QUALITY_FAILURE / UNVERIFIED). This is the "how many opinions
    # before escalating" budget.
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Unanswered attempts: models that never produced an answer (INFRA_FAILURE,
    # QUOTA_FAILURE). These do not spend the answer budget -- a provider that
    # timed out or is throttled said nothing about the task -- but they are
    # bounded separately so a fleet-wide outage still ends in finite time.
    # Every (provider, model) is tried at most once per solve either way.
    max_unanswered_attempts: int = Field(default=6, ge=0, le=30)
    max_verification_attempts: int = Field(default=2, ge=1, le=3)
    # Base per-attempt budget, for a request that asks for almost no output. The
    # budget an attempt actually gets is this plus the requested output tokens at
    # output_tokens_per_second, bounded by max_timeout_seconds.
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    # Assumed free-tier throughput, used only to size the budget above. Lower it
    # where models are slow; it never promises a rate, it only decides how long
    # FAIR waits before calling an attempt failed.
    output_tokens_per_second: float = Field(default=30, gt=0, le=10000)
    # Hard ceiling on any single attempt, however many tokens it asked for.
    max_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    circuit_failures: int = Field(default=3, ge=1)
    circuit_window_seconds: float = Field(default=60, gt=0)
    # Groq's request window refills at 86.4 s per request; a short burst reports ~5m45s.
    cooldown_seconds: float = Field(default=360, gt=0)
    quality_weight: float = Field(default=0.65, ge=0, le=1, allow_inf_nan=False)
    quota_weight: float = Field(default=0.20, ge=0, le=1, allow_inf_nan=False)
    reliability_weight: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    source_policy: SourcePolicy | None = None

    def attempt_deadline(self, max_output_tokens):
        return completion_deadline(
            max_output_tokens,
            base_seconds=self.timeout_seconds,
            tokens_per_second=self.output_tokens_per_second,
            ceiling_seconds=self.max_timeout_seconds,
        )

    @model_validator(mode="after")
    def ceiling_is_not_below_the_base(self):
        if self.max_timeout_seconds < self.timeout_seconds:
            raise ValueError("max_timeout_seconds must be at least timeout_seconds")
        return self

    @model_validator(mode="after")
    def selector_weights_sum_to_one(self):
        total = self.quality_weight + self.quota_weight + self.reliability_weight
        if not 0.99 <= total <= 1.01:
            raise ValueError(f"Selector weights must sum to 1.0 (got {total:.4f})")
        return self
