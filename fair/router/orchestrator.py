import asyncio
from time import monotonic
from uuid import uuid4

from fair.benchmarks.registry import BenchmarkRegistry
from fair.classifier.task_profiler import model_task, profile_task
from fair.governor.kill_switch import KillSwitch
from fair.governor.policy import admit_provider
from fair.performance.registry import PerformanceRegistry
from fair.providers.base import AuthenticationFailed, QuotaExceeded, RateLimited
from fair.quality.consensus import compare, independent
from fair.quality.engine import acceptable, evaluate
from fair.quality.source_reviews import (
    SourcePolicyBlocked,
    SourceReviewRegistry,
    SourceReviewUnavailable,
)
from fair.quality.thresholds import validate_thresholds
from fair.quota.governor import QuotaGovernor
from fair.router.selector import Selector
from fair.schemas.api import SolveResponse
from fair.schemas.db import AuditEvent, EscalationRecord, QualityRecord, RoutingAttempt, TaskRequest
from fair.schemas.db import TaskProfile as ProfileRow
from fair.schemas.domain import (
    Attempt,
    CrossCheckReport,
    NormalizedModelRequest,
    SourcePolicyReport,
)


class Router:
    def __init__(
        self,
        registry,
        settings,
        thresholds,
        sessions,
        sandbox=None,
        source_reviews=None,
        benchmarks=None,
    ):
        self.registry = registry
        self.settings = settings
        self.thresholds = validate_thresholds(thresholds)
        self.sessions = sessions
        self.sandbox = sandbox
        self.source_reviews = source_reviews or SourceReviewRegistry()
        self.benchmarks = benchmarks or BenchmarkRegistry()
        registry.persist(sessions)
        self.quota = QuotaGovernor(settings, sessions)
        self.performance = PerformanceRegistry(sessions)
        self.selector = Selector(registry, self.quota, settings, self.performance, self.benchmarks)
        self.kill_switch = KillSwitch(sessions)
        # One worker until the shared scheduler/dispatch coordination is implemented.
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

    def _persist_attempt(self, request_id, request, profile, attempt):
        with self.sessions.begin() as session:
            detail = attempt.model_dump(mode="json")
            row = RoutingAttempt(request_id=request_id, detail_json=detail)
            session.add(row)
            session.flush()
            quality = attempt.quality
            if quality is not None:
                session.add(
                    QualityRecord(
                        attempt_id=row.id,
                        overall_score=quality.overall_score,
                        hard_reject=quality.hard_reject,
                        verification_state=quality.verification_state,
                        report_json=quality.model_dump(mode="json"),
                    )
                )
            if attempt.disposition != "CANCELLED":
                self.performance.record(session, attempt, profile.task_class)
            self.audit(session, request_id, request.client_id, "ATTEMPT_COMPLETED", detail)

    async def _attempt(self, request_id, request, profile, route, number, role, benchmark_checks):
        if self._source_report(request).state == "BLOCKED":
            raise SourcePolicyBlocked()
        score, spec, model = route
        if self.settings.benchmark_policy is not None:
            check = self.benchmarks.assess(
                request, profile, spec, model, self.settings.benchmark_policy
            )
            benchmark_checks[(spec.provider_id, model.model_id)] = check
            if check.state != "PASSED":
                return None, None, False
        # Shared by production attempts and cross-checks: no second execution authority.
        admit_provider(spec)
        remaining = self.quota.remaining(spec)
        if not self.quota.reserve(spec):
            return None, None, False
        start = monotonic()
        quality = response = error_type = None
        validator_failed = cancelled = False
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
                    "role": role,
                    "selection_score": score,
                    "quota_remaining": remaining,
                },
            )
        try:
            # Both roles receive the same task/evidence. No producer answer, reference answer
            # or host test case is exposed to the independently solving provider.
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
            cancelled = True
            self.quota.failure(spec.provider_id)
            disposition, error_type = "CANCELLED", "REQUEST_CANCELLED"
        except Exception:
            # Never persist raw exception text or provider payloads.
            self.quota.failure(spec.provider_id)
            disposition, error_type = "INFRA_FAILURE", "PROVIDER_UNAVAILABLE"
        else:
            try:
                source_report = self._source_report(request)
                quality = (
                    evaluate(request, profile, response, source_review=source_report)
                    if source_report.state != "NOT_REQUESTED"
                    else evaluate(request, profile, response)
                )
                if (
                    request.validation is not None
                    and request.validation.kind == "native_python_function"
                    and self.sandbox is not None
                    and not quality.hard_reject
                ):
                    native_result = await self.sandbox.validate(response.text, request.validation)
                    quality = evaluate(request, profile, response, native_result=native_result)
                    quality.validator_results["sandbox_image_id"] = self.sandbox.image_id
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
        self._persist_attempt(request_id, request, profile, attempt)
        if cancelled:
            with self.sessions.begin() as session:
                session.get(TaskRequest, request_id).status = "CANCELLED"
                self.audit(session, request_id, request.client_id, "CANCELLED", {})
            raise asyncio.CancelledError
        return attempt, response, validator_failed

    async def _cross_check(
        self,
        request_id,
        request,
        profile,
        primary_route,
        primary_response,
        primary_attempt,
        attempts,
        tried,
        benchmark_checks,
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
            checker_qualifications = {}
            candidates = [
                route
                for route in self.selector.candidates(
                    request,
                    profile,
                    tried,
                    checker_qualifications,
                    lambda spec, model: independent(primary_route, (0, spec, model)),
                )
                if independent(primary_route, route)
            ]
            benchmark_checks.update(checker_qualifications)
            if not candidates:
                if any(check.state != "PASSED" for check in checker_qualifications.values()):
                    report.state = "BENCHMARK_BLOCKED"
                break
            route = candidates[0]
            # Recheck at the dispatch boundary, even if candidate selection changes later.
            if not independent(primary_route, route):
                break
            try:
                attempt, response, failed = await self._attempt(
                    request_id,
                    request,
                    profile,
                    route,
                    len(attempts) + 1,
                    "CROSS_CHECK",
                    benchmark_checks,
                )
            except SourcePolicyBlocked:
                report.state = "SOURCE_BLOCKED"
                break
            except SourceReviewUnavailable:
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
            # Never shop for agreement after a measured rejection/disagreement.
            break
        with self.sessions.begin() as session:
            self.audit(
                session,
                request_id,
                request.client_id,
                "CROSS_CHECK_COMPLETED",
                {
                    **report.model_dump(mode="json"),
                    "model_disagreement": disagreement,
                },
            )
        return report, disagreement

    async def solve(self, request):
        async with self.lock:
            return await self._solve(request)

    def _source_report(self, request):
        try:
            return self.source_reviews.evaluate(request, self.settings.source_policy)
        except Exception as error:
            raise SourceReviewUnavailable("SOURCE_REVIEW_SERVICE_FAILED") from error

    async def _solve(self, request):
        request_id = str(uuid4())
        profile = profile_task(request, self.thresholds)
        attempts, tried = [], set()
        benchmark_checks = {}
        required = request.cross_check_required or request.quality_level == "high_impact_support"
        cross_check = CrossCheckReport(
            required=required, state="NOT_RUN" if required else "NOT_REQUESTED"
        )
        disagreement = "NOT_ASSESSED"
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
                session,
                request_id,
                request.client_id,
                "PROFILED",
                {
                    **profile.model_dump(mode="json"),
                    "cross_check_required": required,
                    "max_primary_attempts": self.settings.max_attempts,
                    "max_verification_attempts": self.settings.max_verification_attempts,
                },
            )
        reason = "NO_ELIGIBLE_FREE_MODELS"
        accepted_response = accepted_quality = None
        validator_failed = False
        for _ in range(self.settings.max_attempts):
            if self.stopped:
                reason = "SYSTEM_STOPPED"
                break
            candidates = self.selector.candidates(request, profile, tried, benchmark_checks)
            if not candidates:
                break
            route = candidates[0]
            try:
                attempt, response, validator_failed = await self._attempt(
                    request_id,
                    request,
                    profile,
                    route,
                    len(attempts) + 1,
                    "PRIMARY",
                    benchmark_checks,
                )
            except SourcePolicyBlocked:
                reason = "SOURCE_POLICY_UNSATISFIED"
                break
            except SourceReviewUnavailable:
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
                    request_id,
                    request,
                    profile,
                    route,
                    response,
                    attempt,
                    attempts,
                    tried,
                    benchmark_checks,
                )
                validator_failed = cross_check.state == "SERVICE_FAILED"
                if cross_check.state != "PASSED":
                    reason = {
                        "STOPPED": "SYSTEM_STOPPED",
                        "DISAGREEMENT": "MODEL_DISAGREEMENT",
                        "REJECTED": "CROSS_CHECK_REJECTED",
                        "UNAVAILABLE": "INDEPENDENT_VERIFIER_UNAVAILABLE",
                        "SERVICE_FAILED": "VALIDATION_SERVICE_FAILED",
                        "SOURCE_BLOCKED": "SOURCE_POLICY_UNSATISFIED",
                        "BENCHMARK_BLOCKED": "BENCHMARK_QUALIFICATION_UNSATISFIED",
                    }[cross_check.state]
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
        if self.settings.benchmark_policy is not None:
            # Recheck every locally accepted role before releasing the primary answer.
            # An expired checker qualification cannot be rescued by the producer's rating.
            for attempt in attempts:
                if attempt.disposition == "ACCEPTED":
                    spec = self.registry.providers[attempt.provider_id]
                    model = next(
                        model for model in spec.models if model.model_id == attempt.model_id
                    )
                    check = self.benchmarks.assess(
                        request, profile, spec, model, self.settings.benchmark_policy
                    )
                    benchmark_checks[(spec.provider_id, model.model_id)] = check
                    if check.state != "PASSED" and accepted_response is not None:
                        accepted_response = accepted_quality = None
                        reason = "BENCHMARK_QUALIFICATION_UNSATISFIED"
            blocked = [check for check in benchmark_checks.values() if check.state != "PASSED"]
            if blocked and reason == "NO_ELIGIBLE_FREE_MODELS":
                reason = "BENCHMARK_QUALIFICATION_UNSATISFIED"
            if reason == "BENCHMARK_QUALIFICATION_UNSATISFIED" and any(
                check.state == "SERVICE_FAILED" for check in blocked
            ):
                validator_failed = True
                reason = "VALIDATION_SERVICE_FAILED"
        try:
            source_report = self._source_report(request)
        except Exception:
            source_report = SourcePolicyReport(
                state="SERVICE_FAILED", reasons=["SOURCE_REVIEW_SERVICE_FAILED"]
            )
            validator_failed = True
            reason = "VALIDATION_SERVICE_FAILED"
            accepted_response = accepted_quality = None
        if source_report.state == "BLOCKED":
            accepted_response = accepted_quality = None
            if not validator_failed and reason != "SYSTEM_STOPPED":
                reason = "SOURCE_POLICY_UNSATISFIED"
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
            verification_state=accepted_quality.verification_state
            if accepted_quality
            else "UNVERIFIED",
            cross_check=cross_check,
            source_policy=source_report,
            benchmark_checks=list(benchmark_checks.values()),
            model_disagreement=disagreement,
        )
        with self.sessions.begin() as session:
            row = session.get(TaskRequest, request_id)
            row.status, row.result_json = result.status, result.model_dump(mode="json")
            if benchmark_checks:
                self.audit(
                    session,
                    request_id,
                    request.client_id,
                    "BENCHMARK_QUALIFICATION_CHECKED",
                    {
                        "checks": [
                            check.model_dump(mode="json") for check in benchmark_checks.values()
                        ]
                    },
                )
            if source_report.state != "NOT_REQUESTED":
                self.audit(
                    session,
                    request_id,
                    request.client_id,
                    "SOURCE_POLICY_CHECKED",
                    source_report.model_dump(mode="json"),
                )
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
