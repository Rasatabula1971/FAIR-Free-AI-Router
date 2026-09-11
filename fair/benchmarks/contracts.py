import math
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from fair.constants import SECONDS_IN_YEAR
from fair.schemas.domain import DTO

Identity = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=10000)]


def wilson(successes, samples):
    """Two-sided 95% Wilson interval for independent Bernoulli observations.

    NIST: https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
    No observations means no estimate, rather than an invented model rating.
    """
    if not 0 <= successes <= samples:
        raise ValueError("Invalid success/sample counts")
    if not samples:
        return None, None
    z = 1.959963984540054
    p = successes / samples
    divisor = 1 + z * z / samples
    center = (p + z * z / (2 * samples)) / divisor
    radius = z * math.sqrt(p * (1 - p) / samples + z * z / (4 * samples**2)) / divisor
    return (
        0.0 if successes == 0 else max(0.0, center - radius),
        1.0 if successes == samples else min(1.0, center + radius),
    )


class BenchmarkPolicy(DTO):
    min_samples: int = Field(default=30, strict=True, ge=1, le=10000)
    max_age_seconds: int = Field(default=2592000, strict=True, ge=1, le=SECONDS_IN_YEAR)


class BenchmarkMetrics(DTO):
    task_class: Identity
    split: Literal["calibration", "holdout"]
    cases: Count
    samples: Count
    successes: Count
    false_accepts: Count
    false_rejects: Count
    unverified: Count
    infrastructure_failures: Count
    quota_failures: Count
    validator_failures: Count

    @model_validator(mode="after")
    def consistent_counts(self):
        if (
            self.samples
            + self.unverified
            + self.infrastructure_failures
            + self.quota_failures
            + self.validator_failures
            != self.cases
        ):
            raise ValueError("Benchmark case counts must reconcile")
        if self.successes + self.false_accepts + self.false_rejects > self.samples:
            raise ValueError("Benchmark outcomes exceed measured samples")
        return self

    def estimate(self):
        lower, upper = wilson(self.successes, self.samples)
        return {
            "pass_rate": self.successes / self.samples if self.samples else None,
            "lower_95": lower,
            "upper_95": upper,
        }


class BenchmarkRun(DTO):
    suite_id: Identity
    suite_revision: Identity
    purpose: Literal["FIXTURE", "WORKLOAD"]
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: Identity
    observed_at: AwareDatetime
    generated_at: AwareDatetime
    engine_version: Identity
    runner_version: Literal["benchmark-v1"] = "benchmark-v1"
    dataset_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    metrics: list[BenchmarkMetrics] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_run(self):
        if self.observed_at > self.generated_at:
            raise ValueError("Benchmark observation cannot follow evaluation")
        keys = [(row.task_class, row.split) for row in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate benchmark metric group")
        if sum(row.cases for row in self.metrics) > 10000:
            raise ValueError("Benchmark exceeds case budget")
        return self


class BenchmarkApproval(DTO):
    run: BenchmarkRun
    client_ids: set[str] = Field(min_length=1, max_length=100)
    reviewer_id: Identity
    review_note: str = Field(min_length=1, max_length=2000)
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    representative_workload: StrictBool

    @model_validator(mode="after")
    def valid_review(self):
        if not self.run.generated_at <= self.approved_at < self.expires_at:
            raise ValueError("Require generated_at <= approved_at < expires_at")
        if not self.review_note.strip() or any(
            not c.strip() or len(c) > 128 for c in self.client_ids
        ):
            raise ValueError("Review rationale and valid client identities are required")
        return self


class BenchmarkFile(DTO):
    approvals: list[BenchmarkApproval] = Field(default_factory=list, max_length=1000)
