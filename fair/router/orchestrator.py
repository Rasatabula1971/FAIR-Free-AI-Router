import asyncio
from time import monotonic
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from fair.benchmarks.registry import BenchmarkRegistry
from fair.cache.exact import ExactCache
from fair.classifier.task_profiler import model_task, profile_task
from fair.governor.kill_switch import KillSwitch
from fair.governor.policy import admit_provider
from fair.performance.registry import PerformanceRegistry
from fair.providers.base import AuthenticationFailed, BillingViolation, QuotaExceeded, RateLimited
from fair.quality.consensus import compare, independent
from fair.quality.engine import acceptable, evaluate
from fair.quality.source_reviews import (
    SourcePolicyBlocked,
    SourceReviewRegistry,
    SourceReviewUnavailable,
)
from fair.quality.thresholds import validate_thresholds
from fair.quota.governor import QuotaGovernor
from fair.router.scheduler import FairScheduler, SchedulingRejected
from fair.router.selector import Selector
from fair.schemas.api import SolveResponse
from fair.schemas.db import AuditEvent, EscalationRecord, QualityRecord, RoutingAttempt, TaskRequest
from fair.schemas.db import TaskProfile as ProfileRow
from fair.schemas.domain import (
    Attempt,
    CrossCheckReport,
    NormalizedModelRequest,
    NormalizedModelResponse,
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
        self.performance = PerformanceRegistry(sessions, settings)
        self.selector = Selector(registry, self.quota, settings, self.performance, self.benchmarks)
        self.kill_switch = KillSwitch(sessions)
        self.scheduler = FairScheduler(settings.scheduler)
        self.inflight = set()
        self.shadow_tasks = set()
        self.cache = ExactCache(self)

    @property
    def stopped(self):
        return self.kill_switch.stopped

    @stopped.setter
    def stopped(self, value):
        self.kill_switch.set(value)
        if value:
            self.scheduler.reject_pending("SYSTEM_STOPPED")

    def audit(self, session, request_id, client_id, event_type, payload):
        session.add(
            AuditEvent(
                request_id=request_id,
                actor_id=client_id,
                event_type=event_type,
                payload_json=payload,
            )
        )

    async def _persist_attempt(self, request_id, request, profile, attempt):
        def _do():
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
        await asyncio.to_thread(_do)

    async def _attempt(
        self, request_id, request, profile, route, number, role, benchmark_checks, shadow_of=None
    ):
        if self._source_report(request).state == "BLOCKED":
            raise SourcePolicyBlocked()
        score, spec, model = route
        if shadow_of is not None and not await self._shadow_candidate(shadow_of, spec, model):
            return None, None, False
        if self.settings.benchmark_policy is not None:
            check = self.benchmarks.assess(
                request, profile, spec, model, self.settings.benchmark_policy
            )
            benchmark_checks[(spec.provider_id, model.model_id)] = check
            if check.state != "PASSED":
                return None, None, False
        admit_provider(spec)
        reserved, remaining = await self.quota.reserve_with_info(spec)
        if not reserved:
            return None, None, False
        start = monotonic()
        quality = response = error_type = None
        validator_failed = cancelled = False
        billing_violation = False

        def _audit_executing():
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
        await asyncio.to_thread(_audit_executing)
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
            if response.quota is not None:
                await self.quota.observe(spec, response.quota)
            await self.quota.success(spec.provider_id)
        except BillingViolation:
            billing_violation = True
            await self.quota.block_security(spec.provider_id)
            self.stopped = True
            disposition, error_type = "INFRA_FAILURE", "PROVIDER_COST_POLICY_VIOLATION"

            def _audit_billing():
                with self.sessions.begin() as session:
                    self.audit(
                        session,
                        request_id,
                        request.client_id,
                        "BILLING_POLICY_VIOLATION",
                        {"provider_id": spec.provider_id, "system_stopped": True},
                    )
            await asyncio.to_thread(_audit_billing)
        except QuotaExceeded as error:
            await self.quota.exhaust(spec.provider_id, reset_at=error.reset_at)
            disposition, error_type = "QUOTA_FAILURE", "QUOTA_EXHAUSTED"
        except RateLimited as error:
            await self.quota.throttle(spec.provider_id, retry_after=error.retry_after)
            disposition, error_type = "QUOTA_FAILURE", "RATE_LIMITED"
        except AuthenticationFailed:
            await self.quota.block_security(spec.provider_id)
            disposition, error_type = "INFRA_FAILURE", "AUTHENTICATION_FAILED"
        except asyncio.CancelledError:
            cancelled = True
            await self.quota.failure(spec.provider_id)
            disposition, error_type = "CANCELLED", "REQUEST_CANCELLED"
        except Exception:
            await self.quota.failure(spec.provider_id)
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
        await self._persist_attempt(request_id, request, profile, attempt)
        if billing_violation:
            raise BillingViolation("PROVIDER_COST_POLICY_VIOLATION")
        if cancelled:
            def _mark_cancelled():
                with self.sessions.begin() as session:
                    session.get(TaskRequest, request_id).status = "CANCELLED"
                    self.audit(session, request_id, request.client_id, "CANCELLED", {})
            await asyncio.to_thread(_mark_cancelled)
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
                for route in await self.selector.candidates(
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
            break

        def _audit_cross_check():
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
        await asyncio.to_thread(_audit_cross_check)
        return report, disagreement

    async def _shadow_candidate(self, parent, spec, model):
        primary = self.registry.providers.get(parent.provider_id)
        original = (
            next((item for item in primary.models if item.model_id == parent.model_id), None)
            if primary
            else None
        )
        if original is None or spec.request_limit is None:
            return False
        if model.model_id.strip().casefold() == original.model_id.strip().casefold():
            return False
        if (model.independence_group or model.model_id).strip().casefold() == (
            original.independence_group or original.model_id
        ).strip().casefold():
            return False
        if (spec.provider_id, model.model_id) == (parent.provider_id, parent.model_id):
            return False
        remaining = await self.quota.remaining(spec)
        return remaining / spec.request_limit > self.settings.shadow_min_headroom

    async def _shadow(self, request, parent):
        if self.scheduler.closed or self.stopped:
            return

        def _count():
            with self.sessions() as session:
                foreground = session.scalar(
                    select(func.count())
                    .select_from(TaskRequest)
                    .where(
                        TaskRequest.client_id == request.client_id,
                        TaskRequest.execution_kind == "PRIMARY",
                        TaskRequest.status == "ACCEPTED",
                        TaskRequest.id.in_(select(RoutingAttempt.request_id)),
                    )
                )
                spent = session.scalar(
                    select(func.count())
                    .select_from(TaskRequest)
                    .where(
                        TaskRequest.client_id == request.client_id,
                        TaskRequest.execution_kind == "SHADOW",
                    )
                )
                return foreground, spent
        foreground, spent = await asyncio.to_thread(_count)
        if spent + 1 > foreground * self.settings.shadow_max_rate:
            return
        profile = profile_task(request, self.thresholds)
        if not await self.selector.candidates(
            request,
            profile,
            set(),
            eligible=lambda spec, model: self._shadow_candidate(parent, spec, model),
        ):
            return
        await self.solve(
            request.model_copy(update={"priority": "P4", "cross_check_required": False}),
            shadow_of=parent,
        )

    def _queue_shadow(self, request, result):
        if (
            not self.settings.shadow_enabled
            or self.scheduler.closed
            or result.status != "ACCEPTED"
            or result.cache_hit
            or request.privacy_class != "PUBLIC"
            or request.evidence
            or request.quality_level == "high_impact_support"
        ):
            return
        task = asyncio.create_task(self._shadow(request, result.model_copy(deep=True)))
        self.shadow_tasks.add(task)

        def finished(done):
            self.shadow_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def close(self):
        self.scheduler.closed = True
        tasks = list(self.shadow_tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
        await self.scheduler.close()
        for adapter in self.registry.adapters.values():
            if hasattr(adapter, "close"):
                await adapter.close()

    async def solve(self, request, *, shadow_of=None):
        request = request.model_copy(deep=True)
        request_id = str(uuid4())
        profile = profile_task(request, self.thresholds)

        def _create_request():
            with self.sessions.begin() as session:
                session.add(
                    TaskRequest(
                        id=request_id,
                        client_id=request.client_id,
                        status="RECEIVED",
                        execution_kind="SHADOW" if shadow_of is not None else "PRIMARY",
                        parent_request_id=shadow_of.request_id if shadow_of is not None else None,
                        profile_json=profile.model_dump(mode="json"),
                    )
                )
        await asyncio.to_thread(_create_request)
        ticket = None
        self.inflight.add(request_id)
        try:
            if self.stopped:
                raise SchedulingRejected("SYSTEM_STOPPED")
            ticket = self.scheduler.submit(
                request.client_id, request.priority, len(request.model_dump_json().encode("utf-8"))
            )
            queued_at = monotonic()

            def _mark_queued():
                with self.sessions.begin() as session:
                    session.get(TaskRequest, request_id).status = "QUEUED"
                    self.audit(
                        session,
                        request_id,
                        request.client_id,
                        "QUEUED",
                        {"priority": request.priority},
                    )
            await asyncio.to_thread(_mark_queued)
            await self.scheduler.wait(ticket)

            def _audit_scheduled():
                with self.sessions.begin() as session:
                    self.audit(
                        session,
                        request_id,
                        request.client_id,
                        "SCHEDULED",
                        {
                            "priority": request.priority,
                            "wait_ms": (monotonic() - queued_at) * 1000,
                        },
                    )
            await asyncio.to_thread(_audit_scheduled)
            if shadow_of is not None:
                return await self._solve(request, request_id, profile, shadow_of=shadow_of)
            result = await self._solve(request, request_id, profile)
            self._queue_shadow(request, result)
            return result
        except SchedulingRejected as error:
            required = (
                request.cross_check_required or request.quality_level == "high_impact_support"
            )
            result = SolveResponse(
                request_id=request_id,
                execution_kind="SHADOW" if shadow_of is not None else "PRIMARY",
                parent_request_id=shadow_of.request_id if shadow_of is not None else None,
                status="FAILED",
                reason_code=error.reason,
                attempts=[],
                minimum_required=profile.minimum_quality_score,
                cross_check=CrossCheckReport(
                    required=required, state="NOT_RUN" if required else "NOT_REQUESTED"
                ),
            )

            def _persist_rejected():
                with self.sessions.begin() as session:
                    row = session.get(TaskRequest, request_id)
                    row.status, row.result_json = result.status, result.model_dump(mode="json")
                    self.audit(
                        session,
                        request_id,
                        request.client_id,
                        "FAILED",
                        {"reason_code": error.reason},
                    )
            await asyncio.to_thread(_persist_rejected)
            return result
        except BaseException as error:
            status = "CANCELLED" if isinstance(error, asyncio.CancelledError) else "FAILED"

            def _persist_error():
                with self.sessions.begin() as session:
                    row = session.get(TaskRequest, request_id)
                    if row.status != status:
                        row.status = status
                        self.audit(session, request_id, request.client_id, status, {})
            await asyncio.to_thread(_persist_error)
            raise
        finally:
            if ticket is not None:
                self.scheduler.release(ticket)
            self.inflight.discard(request_id)

    def _source_report(self, request):
        try:
            return self.source_reviews.evaluate(request, self.settings.source_policy)
        except Exception as error:
            raise SourceReviewUnavailable("SOURCE_REVIEW_SERVICE_FAILED") from error

    async def _solve(self, request, request_id, profile, shadow_of=None):
        attempts, tried = [], set()
        benchmark_checks = {}
        required = request.cross_check_required or request.quality_level == "high_impact_support"
        cross_check = CrossCheckReport(
            required=required, state="NOT_RUN" if required else "NOT_REQUESTED"
        )
        disagreement = "NOT_ASSESSED"

        def _mark_profiled():
            with self.sessions.begin() as session:
                session.get(TaskRequest, request_id).status = "PROFILED"
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
        await asyncio.to_thread(_mark_profiled)
        reason = "NO_ELIGIBLE_FREE_MODELS"
        if shadow_of is None:
            cached = await self.cache.get(request, profile, request_id)
            if cached is not None:
                return cached
        accepted_response = accepted_quality = None
        validator_failed = False
        for _ in range(1 if shadow_of is not None else self.settings.max_attempts):
            if self.stopped:
                reason = "SYSTEM_STOPPED"
                break
            candidates = await self.selector.candidates(
                request,
                profile,
                tried,
                benchmark_checks,
                **(
                    {
                        "eligible": lambda spec, model: self._shadow_candidate(
                            shadow_of, spec, model
                        )
                    }
                    if shadow_of is not None
                    else {}
                ),
            )
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
                    shadow_of=shadow_of,
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
            execution_kind="SHADOW" if shadow_of is not None else "PRIMARY",
            parent_request_id=shadow_of.request_id if shadow_of is not None else None,
            status="FAILED"
            if validator_failed
            else "ACCEPTED"
            if accepted_response
            else "ESCALATION_REQUIRED",
            reason_code=reason,
            attempts=attempts,
            minimum_required=profile.minimum_quality_score,
            best_quality_score=max(scored) if scored else None,
            output=accepted_response.text if accepted_response and shadow_of is None else None,
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

        def _persist_result():
            with self.sessions.begin() as session:
                row = session.get(TaskRequest, request_id)
                row.status, row.result_json = result.status, result.model_dump(mode="json")
                if shadow_of is not None:
                    agreement, basis = (
                        compare(
                            request,
                            NormalizedModelResponse(
                                provider_id=shadow_of.provider_id,
                                model_id=shadow_of.model_id,
                                text=shadow_of.output,
                            ),
                            accepted_response,
                            True,
                        )
                        if accepted_response
                        else (None, "NOT_ASSESSED")
                    )
                    self.audit(
                        session,
                        request_id,
                        request.client_id,
                        "SHADOW_COMPARISON",
                        {
                            "parent_request_id": shadow_of.request_id,
                            "agreement": agreement,
                            "basis": basis,
                            "output_withheld": True,
                        },
                    )
                    self.audit(
                        session,
                        shadow_of.request_id,
                        request.client_id,
                        "SHADOW_COMPLETED",
                        {
                            "shadow_request_id": request_id,
                            "status": result.status,
                            "agreement": agreement,
                            "basis": basis,
                            "output_withheld": True,
                        },
                    )
                if benchmark_checks:
                    self.audit(
                        session,
                        request_id,
                        request.client_id,
                        "BENCHMARK_QUALIFICATION_CHECKED",
                        {
                            "checks": [
                                check.model_dump(mode="json")
                                for check in benchmark_checks.values()
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
        await asyncio.to_thread(_persist_result)
        if shadow_of is None:
            try:
                await self.cache.put(request, profile, result)
            except SQLAlchemyError:
                try:
                    def _audit_cache_fail():
                        with self.sessions.begin() as session:
                            self.audit(
                                session, request_id, request.client_id, "CACHE_WRITE_FAILED", {}
                            )
                    await asyncio.to_thread(_audit_cache_fail)
                except SQLAlchemyError:
                    pass
        return result
