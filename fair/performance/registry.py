import asyncio
import logging
from datetime import UTC, timedelta

from sqlalchemy import select

from fair.config import RoutingSettings
from fair.constants import SECONDS_IN_DAY
from fair.schemas.db import FeedbackEvent, ModelTaskPerformance, utcnow

logger = logging.getLogger(__name__)


class PerformanceRegistry:
    def __init__(self, sessions, settings=None, clock=utcnow):
        self.sessions = sessions
        self.settings = settings or RoutingSettings()
        self.clock = clock

    def record(self, session, attempt, task_class):
        key = (attempt.provider_id, attempt.model_id, task_class)
        row = session.get(ModelTaskPerformance, key)
        if row is None:
            row = ModelTaskPerformance(provider_id=key[0], model_id=key[1], task_class=key[2])
            session.add(row)
            session.flush()
        row.attempts += 1
        row.latency_sum_ms += attempt.latency_ms
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
        row.updated_at = utcnow()

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
        if tested is not None and tested.tzinfo is None:
            logger.warning("Naive datetime in last_quality_at for %s/%s — assuming UTC",
                           row.provider_id, row.model_id)
            tested = tested.replace(tzinfo=UTC)
        age_days = (
            max(0, (self.clock() - tested.astimezone(UTC)).total_seconds() / SECONDS_IN_DAY)
            if tested is not None
            else None
        )
        confidence = (
            row.quality_samples
            / (row.quality_samples + 10)
            * 0.5 ** (age_days / self.settings.confidence_half_life_days)
            if age_days is not None
            else 0
        )
        outcomes = row.recent_outcomes
        return {
            "recent_quality_samples": len(recent),
            "recent_average_quality": average,
            "baseline_quality_samples": count,
            "baseline_average_quality": baseline,
            "drift_state": drift,
            "quality_drop_points": drop,
            "benchmark_review_required": drift == "DEGRADED",
            "observation_confidence": confidence,
            "last_quality_at": tested.isoformat() if tested else None,
            "recent_attempts": len(outcomes),
            "recent_infra_failures": sum(
                item["disposition"] == "INFRA_FAILURE" for item in outcomes
            ),
            "recent_quota_failures": sum(
                item["disposition"] == "QUOTA_FAILURE" for item in outcomes
            ),
            "recent_average_latency_ms": sum(item["latency_ms"] for item in outcomes)
            / len(outcomes)
            if outcomes
            else None,
        }

    async def scores(self, provider_id, model_id, task_class, quality_prior=None, client_id=None):
        def _do():
            with self.sessions() as session:
                row = session.get(ModelTaskPerformance, (provider_id, model_id, task_class))
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
                if client_id is not None and self.settings.feedback_weight:
                    feedback = list(
                        session.scalars(
                            select(FeedbackEvent.score)
                            .where(
                                FeedbackEvent.client_id == client_id,
                                FeedbackEvent.provider_id == provider_id,
                                FeedbackEvent.model_id == model_id,
                                FeedbackEvent.task_class == task_class,
                                FeedbackEvent.created_at
                                >= self.clock()
                                - timedelta(days=self.settings.feedback_max_age_days),
                            )
                            .order_by(FeedbackEvent.created_at.desc(), FeedbackEvent.id.desc())
                            .limit(20)
                        )
                    )
                    if feedback:
                        weight = self.settings.feedback_weight
                        observations = min(20, row.quality_samples) + 5
                        quality = (quality * observations + sum(feedback) / 100 * weight) / (
                            observations + len(feedback) * weight
                        )
                if quality_prior is not None:
                    quality = min(quality_prior, quality)
                observed = row.attempts - row.quota_failures
                reliability = (observed - row.infra_failures + 2.5) / (observed + 5)
                return quality, reliability
        return await asyncio.to_thread(_do)
