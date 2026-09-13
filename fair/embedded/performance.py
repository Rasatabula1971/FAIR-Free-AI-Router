"""In-memory performance tracking — same scoring math, zero SQL."""

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _utcnow():
    return datetime.now(UTC)


@dataclass
class PerfStats:
    attempts: int = 0
    accepted: int = 0
    quality_failures: int = 0
    infra_failures: int = 0
    quota_failures: int = 0
    unverified: int = 0
    hallucination_events: int = 0
    quality_samples: int = 0
    quality_sum: float = 0
    recent_quality: list[float] = field(default_factory=list)
    recent_outcomes: list[dict] = field(default_factory=list)
    last_quality_at: datetime | None = None


class MemoryPerformanceRegistry:
    def __init__(self, settings=None, clock=_utcnow):
        from fair.config import RoutingSettings

        self.settings = settings or RoutingSettings()
        self.clock = clock
        self._stats: dict[tuple[str, str, str], PerfStats] = {}

    def record(self, attempt, task_class):
        key = (attempt.provider_id, attempt.model_id, task_class)
        row = self._stats.setdefault(key, PerfStats())
        row.attempts += 1
        row.recent_outcomes = (
            row.recent_outcomes
            + [{"disposition": attempt.disposition, "latency_ms": attempt.latency_ms}]
        )[-20:]
        counter = {
            "ACCEPTED": "accepted",
            "QUALITY_FAILURE": "quality_failures",
            "INFRA_FAILURE": "infra_failures",
            "QUOTA_FAILURE": "quota_failures",
            "UNVERIFIED": "unverified",
        }.get(attempt.disposition)
        if counter:
            setattr(row, counter, getattr(row, counter) + 1)
        if attempt.disposition in {"ACCEPTED", "QUALITY_FAILURE"} and attempt.quality is not None:
            score = attempt.quality.overall_score
            if score is not None:
                row.quality_samples += 1
                row.quality_sum += score
                row.recent_quality = (row.recent_quality + [score])[-20:]
                row.last_quality_at = self.clock()
            if any(
                reason
                in {
                    "FABRICATED_CITATION",
                    "UNSUPPORTED_CITATION",
                    "UNSUPPORTED_CLAIM",
                    "CONTRADICTED_CLAIM",
                    "CLAIM_OVER_CONFLICTING_EVIDENCE",
                }
                for reason in attempt.quality.reject_reasons
            ):
                row.hallucination_events += 1

    def describe(self, row):
        recent = row.recent_quality
        count = row.quality_samples - len(recent)
        baseline = (row.quality_sum - sum(recent)) / count if count > 0 else None
        average = sum(recent) / len(recent) if recent else None
        enough = count >= 20 and len(recent) >= 5
        drop = max(0, baseline - average) if enough else None
        drift = (
            "INSUFFICIENT_DATA"
            if not enough
            else "DEGRADED"
            if drop >= self.settings.drift_drop_points
            else "STABLE"
        )
        tested = row.last_quality_at
        age_days = (
            max(
                0,
                (
                    self.clock()
                    - (
                        tested.astimezone(UTC)
                        if tested and tested.tzinfo
                        else tested.replace(tzinfo=UTC)
                    )
                ).total_seconds()
                / 86400,
            )
            if tested
            else None
        )
        confidence = (
            row.quality_samples
            / (row.quality_samples + 10)
            * 0.5 ** (age_days / self.settings.confidence_half_life_days)
            if age_days is not None
            else 0
        )
        return {
            "drift_state": drift,
            "quality_drop_points": drop,
            "observation_confidence": confidence,
        }

    def scores(self, provider_id, model_id, task_class, quality_prior=None, client_id=None):
        row = self._stats.get((provider_id, model_id, task_class))
        if row is None:
            return quality_prior if quality_prior is not None else 0.5, 0.5
        prior = quality_prior if quality_prior is not None else 0.5
        quality = (row.quality_sum / 100 + 5 * prior) / (row.quality_samples + 5)
        observation = self.describe(row)
        if observation["drift_state"] == "DEGRADED":
            quality = min(
                quality,
                (sum(row.recent_quality) / 100 + 5 * prior) / (len(row.recent_quality) + 5),
            )
        if quality_prior is not None:
            quality = min(quality_prior, quality)
        observed = row.attempts - row.quota_failures
        reliability = (observed - row.infra_failures + 2.5) / (observed + 5)
        return quality, reliability
