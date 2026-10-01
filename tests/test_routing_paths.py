"""Router failure handling, selector eligibility, grounding and qualification.

The router's job on a bad attempt is to record why, adjust the provider's quota
state and move on without leaking anything. These are the branches that decide
that: router.py was at 77%, selector.py 82%, grounding.py 71%,
qualification.py 83%.
"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fair.config import RoutingSettings
from fair.embedded.performance import MemoryPerformanceRegistry
from fair.embedded.quota import MemoryQuotaGovernor, SharedQuotaLedger
from fair.embedded.router import EmbeddedRouter, _failure_detail
from fair.embedded.selector import MemorySelector
from fair.governor.qualification import qualified
from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ProviderError,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.contracts import Evidence, GroundedValidation
from fair.quality.grounding import grounded_result, resolve_pointer
from fair.schemas.api import SolveRequest
from fair.schemas.domain import ProviderSpec
from fair.schemas.qualification import ModelQualification, ProviderQualification

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def _spec(name="a", **overrides):
    if "request_limit" in overrides:
        overrides.setdefault("request_limit_window", "DAILY_UTC")
    values = dict(
        provider_id=name,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        models=[
            {
                "model_id": "model",
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
        ],
    )
    return ProviderSpec(**(values | overrides))


def _router(entries=None, **settings_kw):
    registry = Registry()
    for spec, adapter in entries or [(_spec(), MockAdapter("a"))]:
        registry.register(spec, adapter)
    thresholds = {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}
    return EmbeddedRouter(registry, RoutingSettings(**settings_kw), thresholds)


class TestOutputBudgetResolution:
    """max_output_tokens is headroom wanted; min_output_tokens is the floor."""

    def test_no_floor_means_the_whole_budget_is_the_floor(self):
        request = _request(max_output_tokens=4096)
        assert request.output_floor == 4096
        assert request.output_budget(4096) == 4096
        # Exactly the refusal every route faced before the field existed.
        assert request.output_budget(4095) is None

    def test_a_route_at_or_above_the_floor_is_asked_for_what_it_can_give(self):
        request = _request(max_output_tokens=32768, min_output_tokens=4096)
        assert request.output_floor == 4096
        assert request.output_budget(4096) == 4096
        assert request.output_budget(16384) == 16384
        assert request.output_budget(32768) == 32768
        # Never more than was asked for, however large the route's ceiling.
        assert request.output_budget(131072) == 32768

    def test_a_route_below_the_floor_cannot_do_the_work(self):
        request = _request(max_output_tokens=32768, min_output_tokens=4096)
        assert request.output_budget(4095) is None

    def test_a_floor_above_the_budget_is_refused(self):
        with pytest.raises(ValidationError, match="min_output_tokens cannot exceed"):
            _request(max_output_tokens=1024, min_output_tokens=2048)

    def test_a_floor_equal_to_the_budget_is_allowed(self):
        assert _request(max_output_tokens=1024, min_output_tokens=1024).output_floor == 1024


def _request(**overrides):
    return SolveRequest.model_validate({"client_id": "c", "task": "hello"} | overrides)


# ── How an attempt's failure is recorded ────────────────────────────────


class TestFailureDetail:
    def test_a_provider_error_keeps_its_own_code(self):
        assert _failure_detail(ProviderUnavailable("HTTP_503")) == "HTTP_503"
        assert _failure_detail(MalformedResponse("PROVIDER_ERROR_ENVELOPE")) == (
            "PROVIDER_ERROR_ENVELOPE"
        )

    def test_an_empty_provider_error_falls_back_to_its_class(self):
        assert _failure_detail(ProviderUnavailable("")) == "ProviderUnavailable"

    def test_a_long_provider_code_is_truncated(self):
        assert len(_failure_detail(ProviderError("A" * 500))) == 120

    def test_any_other_exception_is_reduced_to_its_class_name(self):
        """An arbitrary exception's text can carry a URL with a credential in it."""
        secret = "sk-secret-0123456789"
        assert _failure_detail(RuntimeError(f"https://x.invalid?key={secret}")) == "RuntimeError"


class TestAttemptFailureHandling:
    @pytest.mark.asyncio
    async def test_a_quota_exhaustion_exhausts_the_provider(self):
        error = QuotaExceeded("QUOTA_EXHAUSTED", reset_at=None)
        router = _router(entries=[(_spec(), MockAdapter("a", error=error))])
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "QUOTA_EXHAUSTED"
        assert router.quota.state("a").exhausted is True

    @pytest.mark.asyncio
    async def test_a_rate_limit_throttles_the_provider(self):
        error = RateLimited("RATE_LIMITED", retry_after=30)
        router = _router(entries=[(_spec(), MockAdapter("a", error=error))], cooldown_seconds=10)
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "RATE_LIMITED"
        assert router.quota.effective_status(_spec()) == "THROTTLED"

    @pytest.mark.asyncio
    async def test_an_authentication_failure_blocks_the_provider(self):
        error = AuthenticationFailed("AUTHENTICATION_FAILED")
        router = _router(entries=[(_spec(), MockAdapter("a", error=error))])
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "AUTHENTICATION_FAILED"
        assert router.quota.state("a").security_blocked is True

    @pytest.mark.asyncio
    async def test_a_billing_violation_blocks_only_that_provider(self):
        error = BillingViolation("PROVIDER_REPORTED_NONZERO_OR_INVALID_COST")
        router = _router(
            entries=[
                (_spec("a"), MockAdapter("a", error=error)),
                (_spec("b"), MockAdapter("b", text="345")),
            ]
        )
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
        )
        assert result.status == "ACCEPTED"
        assert result.provider_id == "b"
        assert router.stopped is False
        assert router.quota.state("a").security_blocked is True

    @pytest.mark.asyncio
    async def test_client_cancellation_does_not_penalize_provider_health(self):
        router = _router(
            entries=[(_spec(), MockAdapter("a", error=asyncio.CancelledError()))],
            circuit_failures=1,
        )
        with pytest.raises(asyncio.CancelledError):
            await router.solve(_request())
        state = router.quota.state("a")
        assert state.failures == []
        assert state.circuit_state == "CLOSED"
        assert router.quota.effective_status(_spec()) == "ACTIVE"

    @pytest.mark.asyncio
    async def test_an_unexpected_exception_counts_as_a_provider_failure(self):
        router = _router(entries=[(_spec(), MockAdapter("a", error=RuntimeError("boom")))])
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "PROVIDER_UNAVAILABLE"
        assert result.attempts[0].error_detail == "RuntimeError"

    @pytest.mark.asyncio
    async def test_an_adapter_answering_for_another_model_is_rejected(self):
        class Impostor(MockAdapter):
            async def complete(self, request):
                response = await super().complete(request)
                return response.model_copy(update={"model_id": "other"})

        router = _router(entries=[(_spec(), Impostor("a"))])
        result = await router.solve(_request())
        assert result.status == "ESCALATION_REQUIRED"
        assert result.attempts[0].error_type == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_a_validator_crash_fails_the_request_rather_than_accepting(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("validator exploded")

        # Patched by dotted path: importing the module here as well as importing
        # names from it at the top would import fair.embedded.router both ways.
        monkeypatch.setattr("fair.embedded.router.evaluate", boom)
        router = _router(entries=[(_spec(), MockAdapter("a", text="345"))])
        result = await router.solve(_request())
        assert result.status == "FAILED"
        assert result.reason_code == "VALIDATION_SERVICE_FAILED"

    @pytest.mark.asyncio
    async def test_a_source_review_crash_fails_closed(self):
        class Exploding:
            def evaluate(self, request):
                raise RuntimeError("review service down")

        registry = Registry()
        registry.register(_spec(), MockAdapter("a", text="345"))
        router = EmbeddedRouter(
            registry,
            RoutingSettings(),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            source_reviews=Exploding(),
        )
        result = await router.solve(_request())
        assert result.status == "FAILED"
        assert result.reason_code == "VALIDATION_SERVICE_FAILED"

    @pytest.mark.asyncio
    async def test_an_event_callback_that_raises_does_not_break_the_request(self):
        registry = Registry()
        registry.register(_spec(), MockAdapter("a", text="345"))
        router = EmbeddedRouter(
            registry,
            RoutingSettings(),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            on_event=lambda event, payload: (_ for _ in ()).throw(RuntimeError("callback")),
        )
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ACCEPTED"


# ── Selector eligibility ────────────────────────────────────────────────


class TestSelectorEligibility:
    def _selector(self, entries):
        registry = Registry()
        for spec, adapter in entries:
            registry.register(spec, adapter)
        settings = RoutingSettings()
        quota = MemoryQuotaGovernor(settings)
        return (
            MemorySelector(registry, quota, settings, MemoryPerformanceRegistry(settings)),
            quota,
        )

    def _profile(self, **overrides):
        from fair.classifier.task_profiler import profile_task

        return profile_task(
            _request(**overrides),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
        )

    def test_a_provider_with_no_adapter_is_not_a_candidate(self):
        registry = Registry()
        registry.register(_spec())
        settings = RoutingSettings()
        selector = MemorySelector(
            registry, MemoryQuotaGovernor(settings), settings, MemoryPerformanceRegistry(settings)
        )
        assert selector.candidates(_request(), self._profile(), set()) == []

    def test_a_provider_that_no_longer_admits_is_not_a_candidate(self):
        selector, _ = self._selector([(_spec(), MockAdapter("a"))])
        selector.registry.providers["a"].requires_paid_subscription = True
        assert selector.candidates(_request(), self._profile(), set()) == []

    def test_an_exhausted_provider_is_not_a_candidate(self):
        selector, quota = self._selector([(_spec(), MockAdapter("a"))])
        quota.exhaust("a")
        assert selector.candidates(_request(), self._profile(), set()) == []

    def test_a_tried_model_is_not_offered_again(self):
        selector, _ = self._selector([(_spec(), MockAdapter("a"))])
        assert selector.candidates(_request(), self._profile(), {("a", "model")}) == []

    def test_an_inactive_model_is_not_offered(self):
        selector, _ = self._selector([(_spec(), MockAdapter("a"))])
        selector.registry.providers["a"].models[0].active = False
        assert selector.candidates(_request(), self._profile(), set()) == []

    def test_a_model_without_a_required_capability_is_not_offered(self):
        spec = _spec()
        spec.models[0].capabilities = {"reasoning"}
        selector, _ = self._selector([(spec, MockAdapter("a"))])
        profile = self._profile(required_capabilities={"structured_output"})
        assert selector.candidates(_request(), profile, set()) == []

    def test_a_model_with_too_small_output_budget_is_not_offered(self):
        spec = _spec()
        spec.models[0].max_output_tokens = 64
        selector, _ = self._selector([(spec, MockAdapter("a"))])
        request = _request(max_output_tokens=65)
        assert selector.candidates(request, self._profile(max_output_tokens=65), set()) == []

    def test_a_model_below_the_floor_is_not_offered(self):
        """A declared floor is the one budget a route really must reach."""
        spec = _spec()
        spec.models[0].max_output_tokens = 64
        selector, _ = self._selector([(spec, MockAdapter("a"))])
        overrides = {"max_output_tokens": 4096, "min_output_tokens": 65}
        request = _request(**overrides)
        assert selector.candidates(request, self._profile(**overrides), set()) == []

    def test_a_model_under_the_wanted_budget_but_over_the_floor_is_offered(self):
        """The case the single dial could not express: headroom wanted is 4096, the
        answer needs 64, and a 64-token route can give exactly that."""
        spec = _spec()
        spec.models[0].max_output_tokens = 64
        selector, _ = self._selector([(spec, MockAdapter("a"))])
        overrides = {"max_output_tokens": 4096, "min_output_tokens": 64}
        request = _request(**overrides)
        assert len(selector.candidates(request, self._profile(**overrides), set())) == 1

    def test_a_task_beyond_the_context_window_is_not_offered(self):
        spec = _spec()
        spec.models[0].context_window = 16
        selector, _ = self._selector([(spec, MockAdapter("a"))])
        request = _request(task="x" * 5000)
        assert selector.candidates(request, self._profile(task="x" * 5000), set()) == []

    def test_an_eligibility_filter_can_exclude_a_model(self):
        selector, _ = self._selector([(_spec(), MockAdapter("a"))])
        assert (
            selector.candidates(
                _request(), self._profile(), set(), eligible=lambda spec, model: False
            )
            == []
        )
        assert (
            len(
                selector.candidates(
                    _request(), self._profile(), set(), eligible=lambda spec, model: True
                )
            )
            == 1
        )

    def test_quota_headroom_contributes_to_the_score(self):
        selector, quota = self._selector(
            [(_spec("a", request_limit=100), MockAdapter("a")), (_spec("b"), MockAdapter("b"))]
        )
        for _ in range(90):
            quota.reserve(selector.registry.providers["a"])
        scores = {
            spec.provider_id: score
            for score, spec, _ in selector.candidates(_request(), self._profile(), set())
        }
        assert scores["a"] < scores["b"]


# ── JSON pointer resolution ─────────────────────────────────────────────


class TestResolvePointer:
    DOCUMENT = {"a": {"b": [10, 20]}, "with/slash": 1, "with~tilde": 2, "": 3}

    def test_an_empty_pointer_is_the_whole_document(self):
        assert resolve_pointer(self.DOCUMENT, "") == self.DOCUMENT

    @pytest.mark.parametrize(
        "pointer,expected",
        [
            ("/a/b/0", 10),
            ("/a/b/1", 20),
            ("/with~1slash", 1),
            ("/with~0tilde", 2),
            ("/", 3),
        ],
    )
    def test_resolved_targets(self, pointer, expected):
        assert resolve_pointer(self.DOCUMENT, pointer) == expected

    @pytest.mark.parametrize(
        "pointer,message",
        [
            ("a/b", "Invalid JSON pointer"),
            ("/" + "x" * 512, "Invalid JSON pointer"),
            ("/" + "a/" * 21, "depth budget"),
            ("/with~2escape", "escape"),
            ("/a/b/2", "index not found"),
            ("/a/b/999999999", "index not found"),
            ("/a/b/x", "target not found"),
            ("/missing", "target not found"),
            ("/a/b/0/deeper", "target not found"),
        ],
    )
    def test_refused_pointers(self, pointer, message):
        with pytest.raises(ValueError, match=message):
            resolve_pointer(self.DOCUMENT, pointer)


class TestGroundedResult:
    def test_fields_resolve_with_their_provenance(self):
        contract = GroundedValidation.model_validate(
            {
                "kind": "grounded_json",
                "fields": [{"output_key": "total", "source_id": "s1", "pointer": "/total"}],
            }
        )
        evidence = [Evidence.model_validate({"source_id": "s1", "text": '{"total": 5}'})]
        assert grounded_result(contract, evidence) == {
            "answer": {"total": 5},
            "sources": {"total": {"source_id": "s1", "pointer": "/total"}},
        }

    def test_a_missing_source_is_refused(self):
        contract = GroundedValidation.model_validate(
            {
                "kind": "grounded_json",
                "fields": [{"output_key": "total", "source_id": "absent", "pointer": "/total"}],
            }
        )
        evidence = [Evidence.model_validate({"source_id": "s1", "text": '{"total": 5}'})]
        with pytest.raises(ValueError, match="Grounding source not supplied"):
            grounded_result(contract, evidence)

    def test_one_source_is_parsed_once_for_several_fields(self):
        contract = GroundedValidation.model_validate(
            {
                "kind": "grounded_json",
                "fields": [
                    {"output_key": "a", "source_id": "s1", "pointer": "/a"},
                    {"output_key": "b", "source_id": "s1", "pointer": "/b"},
                ],
            }
        )
        evidence = [Evidence.model_validate({"source_id": "s1", "text": '{"a": 1, "b": 2}'})]
        assert grounded_result(contract, evidence)["answer"] == {"a": 1, "b": 2}


# ── Provider qualification ──────────────────────────────────────────────


def _qualification(**overrides):
    reviewed = NOW - timedelta(days=1)
    values = dict(
        provider_id="a",
        free_status="verified_free_plan",
        production_allowed=True,
        billing_enabled=False,
        payment_method_required=False,
        payment_method_present=False,
        can_auto_bill=False,
        reviewed_at=reviewed,
        expires_at=reviewed + timedelta(days=29),
        reviewer_reference="t",
        billing_reference="t",
        terms_reference="t",
        privacy_reference="t",
        limits_reference="t",
        models=[_model_qualification()],
    )
    return ProviderQualification(**(values | overrides))


def _model_qualification(**overrides):
    reviewed = NOW - timedelta(days=1)
    values = dict(
        model_id="model",
        input_price_per_million=0,
        output_price_per_million=0,
        request_price=0,
        pricing_reference="t",
        paid_tools_enabled=False,
        live_test_passed=True,
        live_test_at=reviewed,
        live_test_reference="t",
        zero_charge_verified=True,
        zero_charge_reference="t",
    )
    return ModelQualification(**(values | overrides))


class TestQualified:
    def test_a_local_provider_needs_no_review_evidence(self):
        assert qualified(_spec(access_class="FREE_LOCAL"), NOW)

    def test_a_remote_provider_without_evidence_is_unqualified(self):
        spec = _spec(access_class="FREE_RECURRING", terms_last_verified=NOW - timedelta(days=1))
        assert not qualified(spec, NOW)

    def test_a_complete_review_qualifies(self):
        spec = _spec(access_class="FREE_RECURRING", qualification=_qualification())
        assert qualified(spec, NOW)

    def test_a_free_status_that_does_not_match_the_access_class_is_unqualified(self):
        spec = _spec(
            access_class="FREE_DYNAMIC",
            qualification=_qualification(free_status="verified_free_plan"),
        )
        assert not qualified(spec, NOW)

    def test_a_review_for_another_provider_is_unqualified(self):
        spec = _spec(access_class="FREE_RECURRING", qualification=_qualification(provider_id="b"))
        assert not qualified(spec, NOW)

    def test_duplicate_model_reviews_cannot_be_constructed(self):
        """qualified() also guards this, but the schema refuses it first."""
        with pytest.raises(ValueError, match="Duplicate model qualification"):
            _qualification(models=[_model_qualification(), _model_qualification()])

    def test_a_spec_with_no_active_model_is_unqualified(self):
        spec = _spec(access_class="FREE_RECURRING", qualification=_qualification())
        spec.models[0].active = False
        assert not qualified(spec, NOW)

    def test_a_model_with_no_review_is_unqualified(self):
        spec = _spec(access_class="FREE_RECURRING", qualification=_qualification())
        spec.models.append(spec.models[0].model_copy(update={"model_id": "unreviewed"}))
        assert not qualified(spec, NOW)

    def test_a_priced_model_review_is_unqualified(self):
        qualification = _qualification(models=[_model_qualification(input_price_per_million=1)])
        spec = _spec(access_class="FREE_RECURRING", qualification=qualification)
        assert not qualified(spec, NOW)

    def test_a_live_test_dated_after_the_review_is_unqualified(self):
        qualification = _qualification(models=[_model_qualification(live_test_at=NOW)])
        spec = _spec(access_class="FREE_RECURRING", qualification=qualification)
        assert not qualified(spec, NOW)

    def test_an_expired_review_is_unqualified(self):
        spec = _spec(access_class="FREE_RECURRING", qualification=_qualification())
        assert not qualified(spec, NOW + timedelta(days=40))

    def test_a_review_dated_in_the_future_is_unqualified(self):
        spec = _spec(access_class="FREE_RECURRING", qualification=_qualification())
        assert not qualified(spec, NOW - timedelta(days=2))


# ── Whose fault was it ───────────────────────────────────────────────────


class TestAFaultIsAttributedToWhoeverOwnsIt:
    """A bug in FAIR must not be recorded as a provider being down."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "defect",
        [
            AttributeError("'NoneType' object has no attribute 'text'"),
            NameError("name 'asyncio' is not defined"),
            TypeError("unsupported operand type"),
            KeyError("provider_id"),
        ],
    )
    async def test_fairs_own_defect_is_named_as_one(self, defect):
        router = _router(entries=[(_spec(), MockAdapter("a", error=defect))])
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "ROUTER_INTERNAL_DEFECT"
        assert result.attempts[0].error_detail == type(defect).__name__

    @pytest.mark.asyncio
    async def test_fairs_own_defect_does_not_open_a_providers_circuit(self):
        """The provider never misbehaved; a bug here must not take it offline."""
        spec = _spec()
        router = _router(
            entries=[(spec, MockAdapter("a", error=AttributeError("boom")))],
            circuit_failures=1,
            cooldown_seconds=600,
        )
        await router.solve(_request())
        state = router.quota.state("a")
        assert state.circuit_state == "CLOSED"
        assert state.failures == []
        assert router.quota.available(spec) is True

    @pytest.mark.asyncio
    async def test_a_provider_fault_still_counts_against_the_provider(self):
        """The contrast: an unrecognised error is the provider's until shown otherwise."""
        spec = _spec()
        router = _router(
            entries=[(spec, MockAdapter("a", error=RuntimeError("connection reset")))],
            circuit_failures=1,
            cooldown_seconds=600,
        )
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "PROVIDER_UNAVAILABLE"
        assert router.quota.state("a").circuit_state == "OPEN"

    @pytest.mark.asyncio
    async def test_a_provider_answering_for_another_model_gets_a_code(self):
        class Impostor(MockAdapter):
            async def complete(self, request):
                response = await super().complete(request)
                return response.model_copy(update={"model_id": "other"})

        router = _router(entries=[(_spec(), Impostor("a", text="345"))])
        result = await router.solve(_request())
        # Still the provider's fault, but "ValueError" said nothing about what
        # it had done wrong.
        assert result.attempts[0].error_type == "PROVIDER_UNAVAILABLE"
        assert result.attempts[0].error_detail == "PROVIDER_IDENTITY_MISMATCH"

    @pytest.mark.asyncio
    async def test_a_validator_crash_records_what_crashed(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("validator exploded")

        monkeypatch.setattr("fair.embedded.router.evaluate", boom)
        router = _router(entries=[(_spec(), MockAdapter("a", text="345"))])
        result = await router.solve(_request())
        assert result.attempts[0].error_type == "VALIDATION_SERVICE_FAILED"
        # The attempt used to record that validation failed and nothing about how.
        assert result.attempts[0].error_detail == "RuntimeError"

    @pytest.mark.asyncio
    async def test_an_escape_from_an_attempt_is_not_a_validation_failure(self):
        class Broken(MemoryQuotaGovernor):
            async def reserve_async(self, spec, application_id=None):
                raise RuntimeError("governor defect")

        registry = Registry()
        registry.register(_spec(), MockAdapter("a", text="345"))
        router = EmbeddedRouter(
            registry,
            RoutingSettings(),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
        )
        router.quota.__class__ = Broken
        result = await router.solve(_request())
        assert result.status == "FAILED"
        # It sent operators to read the quality engine looking for a router bug.
        assert result.reason_code == "ROUTER_INTERNAL_ERROR"


class _Impatient(SharedQuotaLedger):
    """The real ledger with the wait removed, so contention is observed at once."""

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=0)
        database.execute("PRAGMA busy_timeout=0")
        database.execute("PRAGMA foreign_keys=ON")
        return database


class TestABusyLedgerIsNotASpentQuota:
    """Fail-closed is right; reporting it as an exhausted free tier is not."""

    def _router_on(self, ledger):
        registry = Registry()
        registry.register(_spec(request_limit=50), MockAdapter("a", text="345"))
        return EmbeddedRouter(
            registry,
            RoutingSettings(),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            quota_ledger=ledger,
        )

    @pytest.mark.asyncio
    async def test_a_locked_ledger_is_reported_as_itself(self, tmp_path):
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        router = self._router_on(ledger)
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            result = await router.solve(_request())
        assert result.status == "FAILED"
        assert result.reason_code == "QUOTA_LEDGER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_a_locked_ledger_dispatches_nothing(self, tmp_path):
        """FAIR cannot account for the request, so it must not make it."""
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        registry = Registry()
        adapter = MockAdapter("a", text="345")
        registry.register(_spec(request_limit=50), adapter)
        router = EmbeddedRouter(
            registry,
            RoutingSettings(),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            quota_ledger=ledger,
        )
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            await router.solve(_request())
        assert adapter.calls == 0

    @pytest.mark.asyncio
    async def test_the_free_tier_is_untouched_by_the_refusal(self, tmp_path):
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        router = self._router_on(ledger)
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            await router.solve(_request())
        assert ledger.remaining("a", 50, 1_000.0) == 50

    @pytest.mark.asyncio
    async def test_a_working_ledger_still_reports_a_spent_quota_as_one(self, tmp_path):
        """The contrast: an actually exhausted pool must not read as a broken ledger."""
        ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
        registry = Registry()
        registry.register(_spec(request_limit=1), MockAdapter("a", text="345"))
        router = EmbeddedRouter(
            registry,
            RoutingSettings(),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            quota_ledger=ledger,
        )
        verifiable = dict(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        first = await router.solve(_request(**verifiable))
        assert first.status == "ACCEPTED"
        result = await router.solve(_request(**verifiable, client_id="second"))
        assert result.reason_code == "NO_ELIGIBLE_FREE_MODELS"
        assert result.status == "ESCALATION_REQUIRED"
        assert router.quota.ledger_failures == 0
