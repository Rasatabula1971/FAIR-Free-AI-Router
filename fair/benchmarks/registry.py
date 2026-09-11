"""Server-managed workload qualification; clients cannot submit their own ratings."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import yaml

from fair.benchmarks.contracts import BenchmarkFile
from fair.benchmarks.runner import fingerprint
from fair.quality.version import ENGINE_VERSION
from fair.schemas.domain import BenchmarkCheck

logger = logging.getLogger(__name__)


class BenchmarkRegistry:
    def __init__(self, approvals=(), clock=None):
        data = BenchmarkFile(approvals=approvals).model_copy(deep=True)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.approvals = {}
        for approval in data.approvals:
            run = approval.run
            for client in approval.client_ids:
                for task in {metric.task_class for metric in run.metrics}:
                    key = (client, run.provider_id, run.model_id, task)
                    if key in self.approvals:
                        raise ValueError(
                            "Overlapping benchmark approvals; explicitly replace the old run"
                        )
                    self.approvals[key] = approval

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        if path.stat().st_size > 5_000_000:
            raise ValueError("Benchmark approval file exceeds budget")
        return cls(
            BenchmarkFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).approvals
        )

    def assess(self, request, profile, provider, model, policy):
        result = BenchmarkCheck(
            provider_id=provider.provider_id, model_id=model.model_id, task_class=profile.task_class
        )
        if policy is None:
            return result
        result.minimum_required = profile.minimum_quality_score
        try:
            return self._assess(result, request, model, policy)
        except Exception:
            logger.warning(
                "Benchmark assessment failed for %s/%s", result.provider_id, result.model_id, exc_info=True
            )
            result.state = "SERVICE_FAILED"
            return result

    def _assess(self, result, request, model, policy):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Benchmark clock must be timezone-aware")
        result.checked_at = now
        approval = self.approvals.get(
            (request.client_id, result.provider_id, result.model_id, result.task_class)
        )
        if approval is None:
            result.state = "UNAVAILABLE"
            return result
        run = approval.run
        if run.purpose != "WORKLOAD":
            result.state = "FIXTURE"
        elif not approval.representative_workload:
            result.state = "UNREVIEWED"
        elif (
            not model.model_revision
            or model.model_revision != run.model_revision
            or run.engine_version != ENGINE_VERSION
        ):
            result.state = "VERSION_MISMATCH"
        elif (
            now < approval.approved_at
            or now >= approval.expires_at
            or (now - run.observed_at).total_seconds() > policy.max_age_seconds
        ):
            result.state = "EXPIRED"
        else:
            metrics = {row.split: row for row in run.metrics if row.task_class == result.task_class}
            canonical = approval.model_dump(mode="json")
            canonical["client_ids"] = sorted(approval.client_ids)
            result.benchmark_fingerprint = fingerprint(canonical)
            calibration, holdout = metrics.get("calibration"), metrics.get("holdout")
            result.calibration_samples = calibration.samples if calibration else 0
            result.holdout_samples = holdout.samples if holdout else 0
            if (
                not calibration
                or not holdout
                or min(calibration.samples, holdout.samples) < policy.min_samples
            ):
                result.state = "INSUFFICIENT"
            elif any(row.unverified or row.validator_failures for row in metrics.values()):
                result.state = "VALIDATION_GAP"
            elif any(row.false_accepts for row in metrics.values()):
                result.state = "FALSE_ACCEPTANCE"
            else:
                result.conservative_score = 100 * min(
                    row.estimate()["lower_95"] for row in metrics.values()
                )
                result.state = (
                    "PASSED"
                    if result.conservative_score >= result.minimum_required
                    else "BELOW_THRESHOLD"
                )
        return result
