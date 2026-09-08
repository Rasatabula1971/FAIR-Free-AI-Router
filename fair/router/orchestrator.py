import asyncio
from time import monotonic
from uuid import uuid4

from fair.classifier.task_profiler import model_task, profile_task
from fair.governor.kill_switch import KillSwitch
from fair.governor.policy import admit_provider
from fair.performance.registry import PerformanceRegistry
from fair.providers.base import AuthenticationFailed, QuotaExceeded, RateLimited
from fair.quality.engine import acceptable, evaluate
from fair.quota.governor import QuotaGovernor
from fair.router.selector import Selector
from fair.schemas.api import SolveResponse
from fair.schemas.db import AuditEvent, EscalationRecord, QualityRecord, RoutingAttempt, TaskRequest
from fair.schemas.db import TaskProfile as ProfileRow
from fair.schemas.domain import Attempt, NormalizedModelRequest


class Router:
    def __init__(self, registry, settings, thresholds, sessions):
        self.registry = registry
        self.settings = settings
        self.thresholds = thresholds
        self.sessions = sessions
        registry.persist(sessions)
        self.quota = QuotaGovernor(settings, sessions)
        self.performance = PerformanceRegistry(sessions)
        self.selector = Selector(registry, self.quota, settings, self.performance)
        self.kill_switch = KillSwitch(sessions)
        # Milestone A intentionally serializes requests to make reservations/probes atomic.
        self.lock = asyncio.Lock()

    @property
    def stopped(self):
        return self.kill_switch.stopped

    @stopped.setter
    def stopped(self, value):
        self.kill_switch.set(value)

    def audit(self, session, request_id, client_id, event_type, payload):
        session.add(
            AuditEvent(
                request_id=request_id,
                actor_id=client_id,
                event_type=event_type,
                payload_json=payload,
            )
        )

    async def solve(self, request):
        async with self.lock:
            return await self._solve(request)

    async def _solve(self, request):
        request_id = str(uuid4())
        profile = profile_task(request, self.thresholds)
        attempts = []
        tried = set()
        with self.sessions.begin() as session:
            session.add(
                TaskRequest(
                    id=request_id,
                    client_id=request.client_id,
                    status="PROFILED",
                    profile_json=profile.model_dump(mode="json"),
                )
            )
            session.flush()
            session.add(ProfileRow(request_id=request_id, **profile.model_dump(mode="json")))
            self.audit(
                session, request_id, request.client_id, "PROFILED", profile.model_dump(mode="json")
            )
        reason = "NO_ELIGIBLE_FREE_MODELS"
        accepted_response = None
        validator_failed = False
        for number in range(1, self.settings.max_attempts + 1):
            if self.stopped:
                reason = "SYSTEM_STOPPED"
                break
            candidates = self.selector.candidates(request, profile, tried)
            if not candidates:
                break
            score, spec, model = candidates[0]
            # A faulty selector cannot bypass admission at the execution boundary.
            admit_provider(spec)
            remaining = self.quota.remaining(spec)
            if not self.quota.reserve(spec):
                continue
            tried.add((spec.provider_id, model.model_id))
            start = monotonic()
            quality = None
            error_type = None
            with self.sessions.begin() as session:
                self.audit(
                    session,
                    request_id,
                    request.client_id,
                    "EXECUTING",
                    {
                        "provider_id": spec.provider_id,
                        "model_id": model.model_id,
                        "attempt_number": number,
                        "selection_score": score,
                        "quota_remaining": remaining,
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
                        )
                    ),
                    timeout=self.settings.timeout_seconds,
                )
                if response.provider_id != spec.provider_id or response.model_id != model.model_id:
                    raise ValueError("Response identity mismatch")
                self.quota.success(spec.provider_id)
            except QuotaExceeded as error:
                self.quota.exhaust(spec.provider_id, reset_at=error.reset_at)
                disposition, error_type = "QUOTA_FAILURE", "QUOTA_EXHAUSTED"
            except RateLimited:
                self.quota.throttle(spec.provider_id)
                disposition, error_type = "QUOTA_FAILURE", "RATE_LIMITED"
            except AuthenticationFailed:
                self.quota.block_security(spec.provider_id)
                disposition, error_type = "INFRA_FAILURE", "AUTHENTICATION_FAILED"
            except asyncio.CancelledError:
                self.quota.failure(spec.provider_id)
                with self.sessions.begin() as session:
                    row = session.get(TaskRequest, request_id)
                    row.status = "CANCELLED"
                    self.audit(session, request_id, request.client_id, "CANCELLED", {})
                raise
            except Exception:
                # Log only normalized codes, never provider exception text or raw responses.
                self.quota.failure(spec.provider_id)
                disposition, error_type = "INFRA_FAILURE", "PROVIDER_UNAVAILABLE"
            else:
                # Validator defects are service errors, never provider outages or quality evidence.
                try:
                    quality = evaluate(request, profile, response)
                except Exception:
                    validator_failed = True
                    error_type = "VALIDATION_SERVICE_FAILED"
                if acceptable(quality, profile):
                    disposition = "ACCEPTED"
                    accepted_response = response
                    reason = "QUALITY_THRESHOLD_MET"
                elif quality is not None and (
                    quality.hard_reject or quality.overall_score is not None
                ):
                    disposition = "QUALITY_FAILURE"
                    reason = "ALL_FREE_MODELS_FAILED_QUALITY"
                else:
                    disposition = "UNVERIFIED"
                    reason = "QUALITY_VERIFICATION_UNAVAILABLE"
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
            )
            attempts.append(attempt)
            with self.sessions.begin() as session:
                detail = attempt.model_dump(mode="json")
                attempt_row = RoutingAttempt(request_id=request_id, detail_json=detail)
                session.add(attempt_row)
                session.flush()
                if quality is not None:
                    session.add(
                        QualityRecord(
                            attempt_id=attempt_row.id,
                            overall_score=quality.overall_score,
                            hard_reject=quality.hard_reject,
                            verification_state=quality.verification_state,
                            report_json=quality.model_dump(mode="json"),
                        )
                    )
                self.performance.record(session, attempt, profile.task_class)
                self.audit(session, request_id, request.client_id, "ATTEMPT_COMPLETED", detail)
            if accepted_response is not None or validator_failed:
                break
        if accepted_response is None and reason != "SYSTEM_STOPPED" and attempts:
            if any(a.disposition == "UNVERIFIED" for a in attempts):
                reason = "QUALITY_VERIFICATION_UNAVAILABLE"
            elif any(a.disposition == "QUALITY_FAILURE" for a in attempts):
                reason = "ALL_FREE_MODELS_FAILED_QUALITY"
            else:
                reason = "ALL_FREE_MODELS_UNAVAILABLE"
        scored = [
            a.quality.overall_score
            for a in attempts
            if a.quality is not None and a.quality.overall_score is not None
        ]
        if validator_failed:
            reason = "VALIDATION_SERVICE_FAILED"
        result = SolveResponse(
            request_id=request_id,
            status="FAILED"
            if validator_failed
            else "ACCEPTED"
            if accepted_response is not None
            else "ESCALATION_REQUIRED",
            reason_code=reason,
            attempts=attempts,
            minimum_required=profile.minimum_quality_score,
            best_quality_score=max(scored) if scored else None,
            output=accepted_response.text if accepted_response is not None else None,
            provider_id=accepted_response.provider_id if accepted_response is not None else None,
            model_id=accepted_response.model_id if accepted_response is not None else None,
            quality=attempts[-1].quality if accepted_response is not None else None,
            verification_state=attempts[-1].quality.verification_state
            if accepted_response is not None
            else "UNVERIFIED",
        )
        with self.sessions.begin() as session:
            row = session.get(TaskRequest, request_id)
            row.status = result.status
            row.result_json = result.model_dump(mode="json")
            if result.status == "ESCALATION_REQUIRED":
                session.add(
                    EscalationRecord(
                        request_id=request_id,
                        reason_code=reason,
                        detail_json=result.model_dump(mode="json"),
                    )
                )
            self.audit(
                session,
                request_id,
                request.client_id,
                result.status,
                {
                    "reason_code": reason,
                    "attempts_count": len(attempts),
                    "paid_inference_executed": False,
                },
            )
        return result
