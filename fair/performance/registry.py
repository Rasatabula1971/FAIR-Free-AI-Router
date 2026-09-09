from fair.schemas.db import ModelTaskPerformance, utcnow


class PerformanceRegistry:
    def __init__(self, sessions):
        self.sessions = sessions

    def record(self, session, attempt, task_class):
        key = (attempt.provider_id, attempt.model_id, task_class)
        row = session.get(ModelTaskPerformance, key)
        if row is None:
            row = ModelTaskPerformance(provider_id=key[0], model_id=key[1], task_class=key[2])
            session.add(row)
            session.flush()
        row.attempts += 1
        row.latency_sum_ms += attempt.latency_ms
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

    def scores(self, provider_id, model_id, task_class, quality_prior=None):
        with self.sessions() as session:
            row = session.get(ModelTaskPerformance, (provider_id, model_id, task_class))
            if row is None:
                return quality_prior if quality_prior is not None else 0.5, 0.5
            # Five neutral prior observations dampen sparse samples. Infrastructure and
            # quota outcomes never enter the quality numerator or denominator.
            prior = quality_prior if quality_prior is not None else 0.5
            quality = (row.quality_sum / 100 + 5 * prior) / (row.quality_samples + 5)
            if quality_prior is not None:
                # Production failures can reduce ranking, but accumulated history cannot
                # promote a model above its independently measured qualification bound.
                quality = min(quality_prior, quality)
            observed = row.attempts - row.quota_failures
            reliability = (observed - row.infra_failures + 2.5) / (observed + 5)
            return quality, reliability
