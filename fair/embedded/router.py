"""In-memory router — same solve flow as fair.router.orchestrator, zero SQL."""

import asyncio
from time import monotonic
from uuid import uuid4

from fair.classifier.task_profiler import model_task, profile_task
from fair.embedded.cache import MemoryCache
from fair.embedded.performance import MemoryPerformanceRegistry
from fair.embedded.quota import MemoryQuotaGovernor
from fair.embedded.selector import MemorySelector
from fair.providers.base import AuthenticationFailed, BillingViolation, QuotaExceeded, RateLimited
from fair.quality.consensus import compare, independent
from fair.quality.engine import acceptable, evaluate
from fair.quality.thresholds import validate_thresholds
from fair.schemas.api import SolveResponse
from fair.schemas.domain import (
    Attempt,
    CrossCheckReport,
    NormalizedModelRequest,
)


class EmbeddedRouter:
    def __init__(self, registry, settings, thresholds, *, on_event=None):
        self.registry = registry
        self.settings = settings
        self.thresholds = validate_thresholds(thresholds)
        self.on_event = on_event
        self.stopped = False
        self.quota = MemoryQuotaGovernor(settings)
        self.performance = MemoryPerformanceRegistry(settings)
        self.selector = MemorySelector(registry, self.quota, settings, self.performance)
        self.cache = MemoryCache(self)

    def _emit(self, event_type, payload):
        if self.on_event is not None:
            try:
                self.on_event(event_type, payload)
            except Exception:
                pass

    async def _attempt(self, request_id, request, profile, route, number, role):
        score, spec, model = route
        remaining = self.quota.remaining(spec)
        if not self.quota.reserve(spec):
            return None, None, False
        start = monotonic()
        quality = response = error_type = None
        validator_failed = cancelled = False
        billing_violation = False
        self._emit(
            "EXECUTING",
            {
                "provider_id": spec.provider_id,
                "model_id": model.model_id,
                "attempt_number": number,
                "role": role,
            },
        )
        try:
            response = await asyncio.wait_for(
                self.registry.adapters[spec.provider_id].complete(
                    NormalizedModelRequest(
                        task=model_task(request),
                        model_id=model.model_id,
                        request_id=request_id,
                        client_id=request.client_id,
                        task_class=profile.task_class,
                        expected_json_schema=request.expected_schema,
                        max_output_tokens=request.max_output_tokens,
                    )
                ),
                timeout=self.settings.timeout_seconds,
            )
            if response.provider_id != spec.provider_id or response.model_id != model.model_id:
                raise ValueError("Response identity mismatch")
            if response.quota is not None:
                self.quota.observe(spec, response.quota)
            self.quota.success(spec.provider_id)
        except BillingViolation:
            billing_violation = True
            self.quota.block_security(spec.provider_id)
            self.stopped = True
            disposition, error_type = "INFRA_FAILURE", "PROVIDER_COST_POLICY_VIOLATION"
        except QuotaExceeded as error:
            self.quota.exhaust(spec.provider_id, reset_at=error.reset_at)
            disposition, error_type = "QUOTA_FAILURE", "QUOTA_EXHAUSTED"
        except RateLimited as error:
            self.quota.throttle(spec.provider_id, retry_after=error.retry_after)
            disposition, error_type = "QUOTA_FAILURE", "RATE_LIMITED"
        except AuthenticationFailed:
            self.quota.block_security(spec.provider_id)
            disposition, error_type = "INFRA_FAILURE", "AUTHENTICATION_FAILED"
        except asyncio.CancelledError:
            cancelled = True
            self.quota.failure(spec.provider_id)
            disposition, error_type = "CANCELLED", "REQUEST_CANCELLED"
        except Exception:
            self.quota.failure(spec.provider_id)
            disposition, error_type = "INFRA_FAILURE", "PROVIDER_UNAVAILABLE"
        else:
            try:
                quality = evaluate(request, profile, response)
            except asyncio.CancelledError:
                cancelled = True
                quality = None
                error_type = "REQUEST_CANCELLED"
            except Exception:
                quality = None
                validator_failed = True
                error_type = "VALIDATION_SERVICE_FAILED"
            if cancelled:
                disposition = "CANCELLED"
            elif acceptable(quality, profile):
                disposition = "ACCEPTED"
            elif quality is not None and (quality.hard_reject or quality.overall_score is not None):
                disposition = "QUALITY_FAILURE"
            else:
                disposition = "UNVERIFIED"
        attempt = Attempt(
            attempt_number=number,
            provider_id=spec.provider_id,
            model_id=model.model_id,
            selection_score=score,
            quota_remaining=remaining,
            disposition=disposition,
            latency_ms=(monotonic() - start) * 1000,
            error_type=error_type,
            quality=quality,
            role=role,
        )
        if attempt.disposition != "CANCELLED":
            self.performance.record(attempt, profile.task_class)
        self._emit("ATTEMPT_COMPLETED", attempt.model_dump(mode="json"))
        if billing_violation:
            raise BillingViolation("PROVIDER_COST_POLICY_VIOLATION")
        if cancelled:
            raise asyncio.CancelledError
        return attempt, response, validator_failed

    async def _cross_check(
        self, request_id, request, profile, primary_route, primary_response, primary_attempt,
        attempts, tried,
    ):
        report = CrossCheckReport(
            required=True,
            state="UNAVAILABLE",
            primary_attempt_number=primary_attempt.attempt_number,
        )
        disagreement = "NOT_ASSESSED"
        for _ in range(self.settings.max_verification_attempts):
            if self.stopped:
                report.state = "STOPPED"
                break
            candidates = [
                route
                for route in self.selector.candidates(
                    request, profile, tried,
                    eligible=lambda spec, model: independent(primary_route, (0, spec, model)),
                )
                if independent(primary_route, route)
            ]
            if not candidates:
                break
            route = candidates[0]
            if not independent(primary_route, route):
                break
            try:
                attempt, response, failed = await self._attempt(
                    request_id, request, profile, route, len(attempts) + 1, "CROSS_CHECK",
                )
            except (BillingViolation, asyncio.CancelledError):
                raise
            except Exception:
                report.state = "SERVICE_FAILED"
                break
            if attempt is None:
                continue
            tried.add((attempt.provider_id, attempt.model_id))
            attempts.append(attempt)
            report.attempts_count += 1
            report.verification_attempt_number = attempt.attempt_number
            if self.stopped:
                report.state = "STOPPED"
                break
            if failed:
                report.state = "SERVICE_FAILED"
                break
            if attempt.disposition in {"INFRA_FAILURE", "QUOTA_FAILURE"}:
                continue
            try:
                agreement, basis = compare(
                    request, primary_response, response, attempt.disposition == "ACCEPTED"
                )
            except Exception:
                report.state = "SERVICE_FAILED"
                break
            report.agreement_basis = basis
            disagreement = (
                "DETECTED" if agreement is False else "NONE" if agreement else "NOT_ASSESSED"
            )
            if agreement is False:
                report.state = "DISAGREEMENT"
            elif attempt.disposition == "ACCEPTED" and agreement is True:
                report.state = "PASSED"
            else:
                report.state = "REJECTED"
            break
        self._emit("CROSS_CHECK_COMPLETED", report.model_dump(mode="json"))
        return report, disagreement

    async def solve(self, request):
        request = request.model_copy(deep=True)
        request_id = str(uuid4())
        profile = profile_task(request, self.thresholds)
        self._emit("PROFILED", profile.model_dump(mode="json"))
        required = request.cross_check_required or request.quality_level == "high_impact_support"
        cross_check = CrossCheckReport(
            required=required, state="NOT_RUN" if required else "NOT_REQUESTED"
        )
        disagreement = "NOT_ASSESSED"
        cached = self.cache.get(request, profile, request_id)
        if cached is not None:
            return cached
        attempts, tried = [], set()
        reason = "NO_ELIGIBLE_FREE_MODELS"
        accepted_response = accepted_quality = None
        validator_failed = False
        for _ in range(self.settings.max_attempts):
            if self.stopped:
                reason = "SYSTEM_STOPPED"
                break
            candidates = self.selector.candidates(request, profile, tried)
            if not candidates:
                break
            route = candidates[0]
            try:
                attempt, response, validator_failed = await self._attempt(
                    request_id, request, profile, route, len(attempts) + 1, "PRIMARY",
                )
            except (BillingViolation, asyncio.CancelledError):
                raise
            except Exception:
                validator_failed = True
                break
            if attempt is None:
                continue
            tried.add((attempt.provider_id, attempt.model_id))
            attempts.append(attempt)
            if validator_failed:
                break
            if self.stopped and not required:
                reason = "SYSTEM_STOPPED"
                break
            if attempt.disposition != "ACCEPTED":
                continue
            if required:
                cross_check, disagreement = await self._cross_check(
                    request_id, request, profile, route, response, attempt, attempts, tried,
                )
                validator_failed = cross_check.state == "SERVICE_FAILED"
                if cross_check.state != "PASSED":
                    reason = {
                        "STOPPED": "SYSTEM_STOPPED",
                        "DISAGREEMENT": "MODEL_DISAGREEMENT",
                        "REJECTED": "CROSS_CHECK_REJECTED",
                        "UNAVAILABLE": "INDEPENDENT_VERIFIER_UNAVAILABLE",
                        "SERVICE_FAILED": "VALIDATION_SERVICE_FAILED",
                    }.get(cross_check.state, "CROSS_CHECK_REJECTED")
                    break
            accepted_response, accepted_quality = response, attempt.quality
            reason = "QUALITY_THRESHOLD_MET"
            break
        if accepted_response is None and reason == "NO_ELIGIBLE_FREE_MODELS" and attempts:
            if any(a.disposition == "UNVERIFIED" for a in attempts):
                reason = "QUALITY_VERIFICATION_UNAVAILABLE"
            elif any(a.disposition == "QUALITY_FAILURE" for a in attempts):
                reason = "ALL_FREE_MODELS_FAILED_QUALITY"
            else:
                reason = "ALL_FREE_MODELS_UNAVAILABLE"
        if validator_failed:
            reason = "VALIDATION_SERVICE_FAILED"
        scored = [
            a.quality.overall_score
            for a in attempts
            if a.quality is not None and a.quality.overall_score is not None
        ]
        result = SolveResponse(
            request_id=request_id,
            status="FAILED"
            if validator_failed
            else "ACCEPTED"
            if accepted_response
            else "ESCALATION_REQUIRED",
            reason_code=reason,
            attempts=attempts,
            minimum_required=profile.minimum_quality_score,
            best_quality_score=max(scored) if scored else None,
            output=accepted_response.text if accepted_response else None,
            provider_id=accepted_response.provider_id if accepted_response else None,
            model_id=accepted_response.model_id if accepted_response else None,
            quality=accepted_quality,
            verification_state=accepted_quality.verification_state if accepted_quality else "UNVERIFIED",
            cross_check=cross_check,
            model_disagreement=disagreement,
        )
        self._emit(result.status, {"reason_code": reason, "attempts_count": len(attempts)})
        self.cache.put(request, profile, result)
        return result

    async def close(self):
        self.stopped = True
