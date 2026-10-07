"""A limit a provider counts per model benches that model, not the provider.

Before this, any rate limit benched every model at the provider for the cooldown,
and one Gemini model reaching its daily cap took the other out until midnight
Pacific. The provider-wide reading stays the default: a refusal is treated as
being about one model only when the provider's own error says so.
"""

import pytest
from pydantic import SecretStr

from fair.classifier.task_profiler import profile_task
from fair.config import RoutingSettings
from fair.embedded.module import FAIR
from fair.embedded.performance import MemoryPerformanceRegistry
from fair.embedded.quota import MemoryQuotaGovernor, SharedQuotaLedger
from fair.embedded.router import EmbeddedRouter
from fair.embedded.selector import MemorySelector
from fair.providers.base import QuotaExceeded, RateLimited
from fair.providers.registry import Registry
from fair.quality.thresholds import DEFAULT_THRESHOLDS
from fair.schemas.api import SolveRequest
from fair.schemas.domain import NormalizedModelRequest, NormalizedModelResponse, ProviderSpec
from fair.security.adapter import CredentialedAdapter

FIRST, SECOND = "alpha", "beta"
ARITHMETIC = {"kind": "arithmetic", "expression": "15*23"}


def _spec(name="p", models=(FIRST, SECOND), **overrides):
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
                "model_id": model_id,
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
            for model_id in models
        ],
    )
    return ProviderSpec(**(values | overrides))


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _governor(clock=None, **settings):
    return MemoryQuotaGovernor(RoutingSettings(**settings), clock=clock or Clock())


class PerModel:
    """An adapter whose answer, or refusal, depends on which model was asked."""

    def __init__(self, provider_id, outcomes):
        self.provider_id = provider_id
        self.outcomes = outcomes
        self.calls = []

    async def complete(self, request):
        self.calls.append(request.model_id)
        outcome = self.outcomes[request.model_id]
        if isinstance(outcome, Exception):
            raise outcome
        return NormalizedModelResponse(
            provider_id=self.provider_id, model_id=request.model_id, text=outcome
        )


def _router(outcomes, spec=None, **settings):
    spec = spec or _spec()
    registry = Registry()
    adapter = PerModel(spec.provider_id, outcomes)
    registry.register(spec, adapter)
    thresholds = {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}
    return EmbeddedRouter(registry, RoutingSettings(**settings), thresholds), adapter, spec


def _request(**overrides):
    fields = {"client_id": "c", "task": "15*23", "validation": ARITHMETIC}
    return SolveRequest.model_validate(fields | overrides)


class TestGovernor:
    def test_a_throttled_model_leaves_its_provider_and_siblings_alone(self):
        governor, spec = _governor(), _spec()
        governor.throttle_model("p", FIRST)
        assert governor.model_available("p", FIRST) is False
        assert governor.model_available("p", SECOND) is True
        assert governor.available(spec) is True
        assert governor.effective_status(spec) == "ACTIVE"
        assert governor.state("p").throttled_until == 0

    def test_a_model_block_belongs_to_one_provider(self):
        governor = _governor()
        governor.throttle_model("p", FIRST)
        assert governor.model_available("q", FIRST) is True

    @pytest.mark.parametrize(
        "retry_after, expected",
        [(None, 360), (30, 360), (900, 900), (float("inf"), 360), (-5, 360), ("7", 360)],
    )
    def test_the_bench_lasts_the_cooldown_or_the_providers_wait_if_longer(
        self, retry_after, expected
    ):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        governor.throttle_model("p", FIRST, retry_after=retry_after)
        assert governor.benched_models("p") == {FIRST: clock.now + expected}
        clock.now += expected - 1
        assert governor.model_available("p", FIRST) is False
        clock.now += 1
        assert governor.model_available("p", FIRST) is True
        assert governor.benched_models("p") == {}

    def test_a_later_shorter_refusal_never_shortens_the_bench(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=10)
        governor.throttle_model("p", FIRST, retry_after=600)
        governor.throttle_model("p", FIRST, retry_after=20)
        assert governor.benched_models("p") == {FIRST: clock.now + 600}

    def test_a_spent_model_returns_when_the_provider_says_it_resets(self):
        clock = Clock()
        governor, spec = _governor(clock), _spec()
        governor.exhaust_model(spec, FIRST, reset_at=clock.now + 5000)
        assert governor.benched_models("p") == {FIRST: clock.now + 5000}
        assert governor.state("p").exhausted is False
        assert governor.available(spec) is True

    @pytest.mark.parametrize("reset_at", [None, 0, float("nan"), float("inf")])
    def test_without_a_usable_reset_the_window_or_a_bounded_wait_applies(self, reset_at):
        clock = Clock()
        governor = _governor(clock)
        governor.exhaust_model(_spec(), FIRST, reset_at=reset_at)
        windowless = governor.benched_models("p")[FIRST]
        assert clock.now < windowless <= clock.now + 86400

        daily = _spec("d", request_limit=100)
        governor.exhaust_model(daily, FIRST, reset_at=reset_at)
        until = governor.benched_models("d")[FIRST]
        assert clock.now < until <= clock.now + 86400
        assert until % 86400 == 0  # the next UTC midnight, as a provider-wide exhaustion uses

    def test_a_provider_with_every_model_benched_is_reported_throttled(self):
        governor, spec = _governor(), _spec()
        governor.throttle_model("p", FIRST)
        assert governor.effective_status(spec) == "ACTIVE"
        governor.throttle_model("p", SECOND)
        assert governor.effective_status(spec) == "THROTTLED"

    async def test_the_async_status_agrees(self):
        governor, spec = _governor(), _spec()
        governor.throttle_model("p", FIRST)
        governor.throttle_model("p", SECOND)
        assert await governor.effective_status_async(spec) == "THROTTLED"

    def test_an_inactive_model_does_not_keep_a_provider_looking_routable(self):
        governor = _governor()
        spec = _spec()
        spec.models[1].active = False
        governor.throttle_model("p", FIRST)
        assert governor.effective_status(spec) == "THROTTLED"

    def test_a_model_block_spends_nothing_from_the_shared_pool(self, tmp_path):
        ledger = SharedQuotaLedger(str(tmp_path / "quota.sqlite3"))
        clock = Clock()
        one = MemoryQuotaGovernor(RoutingSettings(), clock=clock, shared_ledger=ledger)
        two = MemoryQuotaGovernor(RoutingSettings(), clock=clock, shared_ledger=ledger)
        spec = _spec(request_limit=100)
        one.exhaust_model(spec, FIRST, reset_at=clock.now + 5000)
        assert one.available(spec) is True
        # Local, like a throttle: the other application still has the whole pool.
        assert two.available(spec) is True
        assert two.model_available("p", FIRST) is True


class TestSelector:
    def _candidates(self, governor, spec):
        registry = Registry()
        registry.register(spec, PerModel(spec.provider_id, {}))
        settings = RoutingSettings()
        selector = MemorySelector(registry, governor, settings, MemoryPerformanceRegistry(settings))
        request = _request()
        return selector, request, profile_task(request, dict(DEFAULT_THRESHOLDS))

    def test_a_benched_model_is_not_offered_and_its_sibling_is(self):
        governor, spec = _governor(), _spec()
        selector, request, profile = self._candidates(governor, spec)
        governor.throttle_model("p", FIRST)
        offered = [model.model_id for _, _, model in selector.candidates(request, profile, set())]
        assert offered == [SECOND]

    async def test_the_async_selector_agrees(self):
        governor, spec = _governor(), _spec()
        selector, request, profile = self._candidates(governor, spec)
        governor.exhaust_model(spec, SECOND)
        offered = [
            model.model_id
            for _, _, model in await selector.candidates_async(request, profile, set())
        ]
        assert offered == [FIRST]


class TestRouter:
    @pytest.mark.parametrize(
        "refusal, error_type",
        [
            (RateLimited("RATE_LIMITED", retry_after=30, model_scoped=True), "RATE_LIMITED"),
            (QuotaExceeded("DAILY", reset_at=None, model_scoped=True), "QUOTA_EXHAUSTED"),
        ],
    )
    async def test_the_sibling_answers_and_the_provider_stays_in_rotation(
        self, refusal, error_type
    ):
        router, adapter, spec = _router({FIRST: refusal, SECOND: "345"})
        result = await router.solve(_request())
        assert [(a.model_id, str(a.disposition), a.error_type) for a in result.attempts] == [
            (FIRST, "QUOTA_FAILURE", error_type),
            (SECOND, "ACCEPTED", None),
        ]
        assert (result.status, result.model_id) == ("ACCEPTED", SECOND)
        state = router.quota.state("p")
        assert (state.exhausted, state.throttled_until) == (False, 0)
        assert router.quota.effective_status(spec) == "ACTIVE"
        assert list(router.quota.benched_models("p")) == [FIRST]

        # The benched model is not asked again while the bench holds.
        await router.solve(_request(task="15*23 again"))
        assert adapter.calls == [FIRST, SECOND, SECOND]

    @pytest.mark.parametrize(
        "refusal, status",
        [
            (RateLimited("RATE_LIMITED", retry_after=30), "THROTTLED"),
            (QuotaExceeded("QUOTA_EXHAUSTED"), "QUOTA_EXHAUSTED"),
        ],
    )
    async def test_a_refusal_of_unknown_scope_still_benches_the_provider(self, refusal, status):
        router, adapter, spec = _router({FIRST: refusal, SECOND: "345"})
        result = await router.solve(_request())
        assert result.status == "ESCALATION_REQUIRED"
        assert adapter.calls == [FIRST]
        assert router.quota.effective_status(spec) == status
        assert router.quota.benched_models("p") == {}

    async def test_a_named_refusal_during_a_recovery_probe_is_no_verdict_on_the_provider(self):
        """The provider answered. Its circuit reopens for the next probe, uncharged."""
        refusal = RateLimited("RATE_LIMITED", model_scoped=True)
        router, adapter, _ = _router({FIRST: refusal, SECOND: "345"})
        state = router.quota.state("p")
        state.circuit_state, state.blocked_until = "OPEN", 0
        result = await router.solve(_request())
        assert (result.status, result.model_id) == ("ACCEPTED", SECOND)
        assert adapter.calls == [FIRST, SECOND]
        assert router.quota.state("p").circuit_state == "CLOSED"

    async def test_an_unscoped_refusal_during_a_recovery_probe_reopens_the_circuit(self):
        router, adapter, _ = _router({FIRST: RateLimited("RATE_LIMITED"), SECOND: "345"})
        state = router.quota.state("p")
        state.circuit_state, state.blocked_until = "OPEN", 0
        result = await router.solve(_request())
        assert result.status == "ESCALATION_REQUIRED"
        assert adapter.calls == [FIRST]
        assert router.quota.state("p").circuit_state == "OPEN"

    async def test_every_model_benched_ends_the_solve_without_another_request(self):
        refusal = RateLimited("RATE_LIMITED", model_scoped=True)
        router, adapter, spec = _router({FIRST: refusal, SECOND: refusal})
        result = await router.solve(_request())
        assert result.status == "ESCALATION_REQUIRED"
        assert adapter.calls == [FIRST, SECOND]
        assert router.quota.effective_status(spec) == "THROTTLED"
        await router.solve(_request(task="15*23 again"))
        assert adapter.calls == [FIRST, SECOND]


class TestScopeSurvivesTheCredentialGuard:
    def _guarded(self, error):
        inner = PerModel("p", {FIRST: error})
        return CredentialedAdapter("p", inner, SecretStr("not-in-any-message"))

    def _ask(self):
        return NormalizedModelRequest(
            task="ping", model_id=FIRST, request_id="r", client_id="c", task_class="general"
        )

    async def test_a_scoped_rate_limit_keeps_its_scope_and_says_so(self):
        guarded = self._guarded(RateLimited("anything", retry_after=12, model_scoped=True))
        with pytest.raises(RateLimited, match="^MODEL_RATE_LIMITED$") as raised:
            await guarded.complete(self._ask())
        assert (raised.value.model_scoped, raised.value.retry_after) == (True, 12)

    async def test_a_scoped_exhaustion_keeps_its_scope_and_says_so(self):
        guarded = self._guarded(QuotaExceeded("anything", reset_at=99.0, model_scoped=True))
        with pytest.raises(QuotaExceeded, match="^MODEL_QUOTA_EXHAUSTED$") as raised:
            await guarded.complete(self._ask())
        assert (raised.value.model_scoped, raised.value.reset_at) == (True, 99.0)

    @pytest.mark.parametrize(
        "error, code",
        [(RateLimited("x"), "RATE_LIMITED"), (QuotaExceeded("x"), "QUOTA_EXHAUSTED")],
    )
    async def test_an_unscoped_refusal_is_reported_as_before(self, error, code):
        guarded = self._guarded(error)
        with pytest.raises(type(error), match=f"^{code}$") as raised:
            await guarded.complete(self._ask())
        assert raised.value.model_scoped is False

    async def test_only_a_literal_true_counts_as_scoped(self):
        error = RateLimited("x")
        error.model_scoped = "yes"
        with pytest.raises(RateLimited, match="^RATE_LIMITED$") as raised:
            await self._guarded(error).complete(self._ask())
        assert raised.value.model_scoped is False


class TestOperatorView:
    async def test_benched_models_are_listed_with_when_they_return(self):
        spec = _spec()
        refusal = RateLimited("RATE_LIMITED", retry_after=900, model_scoped=True)
        fair = FAIR(
            providers=[(spec, PerModel("p", {FIRST: refusal, SECOND: "345"}))],
            cache_enabled=False,
        )
        assert fair.providers()[0]["benched_models"] == {}
        result = await fair.solve("15*23", validation=ARITHMETIC)
        assert result.model_id == SECOND
        for listed in (fair.providers()[0], (await fair.providers_async())[0]):
            assert listed["status"] == "ACTIVE"
            assert list(listed["benched_models"]) == [FIRST]
        await fair.close()
