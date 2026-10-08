"""What a provider says about tokens is taken at its word, and no further.

FAIR counts requests. A provider that also meters tokens refuses in two ways FAIR
used to misread:

- A per-minute limit that had cleared in seconds held the model for the whole
  cooldown, because the cooldown is a floor under any wait. A large answer spends
  most of a per-minute token allowance, so that was six idle minutes after each one.
- A request larger than the route allows came back as HTTP 413 and was counted as
  an outage. Three of them in a minute opened the circuit on a provider that had
  answered every one.

Neither fix keeps a token count. Nothing here is a limit FAIR assumed: the window,
the wait and the refused size all come from the provider's own refusal.
"""

import pytest
from pydantic import SecretStr

from fair.config import RoutingSettings
from fair.embedded.quota import MemoryQuotaGovernor
from fair.embedded.router import EmbeddedRouter
from fair.providers.base import (
    ProviderUnavailable,
    RateLimited,
    RequestNotSupported,
    RequestTooLarge,
)
from fair.providers.registry import Registry
from fair.schemas.api import SolveRequest
from fair.schemas.domain import NormalizedModelRequest, NormalizedModelResponse, ProviderSpec
from fair.security.adapter import CredentialedAdapter

FIRST, SECOND = "alpha", "beta"
ARITHMETIC = {"kind": "arithmetic", "expression": "15*23"}


def _spec(name="p", models=(FIRST, SECOND)):
    return ProviderSpec(
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


def _router(outcomes, **settings):
    """A router on a clock the test moves, so a bench can be seen to end."""
    spec, registry, clock = _spec(), Registry(), Clock()
    adapter = PerModel(spec.provider_id, outcomes)
    registry.register(spec, adapter)
    thresholds = {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}
    router = EmbeddedRouter(registry, RoutingSettings(**settings), thresholds)
    router.quota.clock = clock
    return router, adapter, spec, clock


def _request(**overrides):
    fields = {"client_id": "c", "task": "15*23", "validation": ARITHMETIC}
    return SolveRequest.model_validate(fields | overrides)


class TestAPerMinuteLimitClearsWithinTheMinute:
    @pytest.mark.parametrize(
        "retry_after, expected",
        [
            (7.5, 7.5),  # the provider's wait, though far under the cooldown
            (900, 900),  # and still its wait when that is longer
            (None, 60),  # none stated: the window itself
            (0, 60),
            (-5, 60),
            (float("inf"), 60),
            (float("nan"), 60),
            (86401, 60),
            ("7", 60),
            (True, 60),
        ],
    )
    def test_a_model_is_benched_for_the_stated_wait_or_else_the_window(self, retry_after, expected):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        governor.throttle_model("p", FIRST, retry_after=retry_after, per_minute=True)
        assert governor.benched_models("p") == {FIRST: clock.now + expected}
        clock.now += expected
        assert governor.model_available("p", FIRST) is True

    def test_the_window_is_a_minute_whatever_the_cooldown_is_set_to(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=5)
        governor.throttle_model("p", FIRST, per_minute=True)
        assert governor.benched_models("p") == {FIRST: clock.now + 60}

    def test_a_limit_whose_window_is_not_named_keeps_the_cooldown(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        governor.throttle_model("p", FIRST, retry_after=7.5)
        assert governor.benched_models("p") == {FIRST: clock.now + 360}

    def test_it_never_shortens_a_longer_bench_already_held(self):
        """A daily token limit and then a per-minute one: the daily block stands."""
        clock = Clock()
        governor = _governor(clock)
        governor.throttle_model("p", FIRST, retry_after=5000)
        governor.throttle_model("p", FIRST, retry_after=3, per_minute=True)
        assert governor.benched_models("p") == {FIRST: clock.now + 5000}

    def test_a_provider_wide_per_minute_limit_is_held_for_its_wait_too(self):
        clock = Clock()
        governor, spec = _governor(clock, cooldown_seconds=360), _spec()
        governor.throttle("p", retry_after=12, per_minute=True)
        assert governor.state("p").throttled_until == clock.now + 12
        clock.now += 12
        assert governor.available(spec) is True

    def test_a_provider_wide_limit_of_unknown_window_keeps_the_cooldown(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        governor.throttle("p", retry_after=12)
        assert governor.state("p").throttled_until == clock.now + 360

    async def test_the_router_brings_the_model_back_after_the_stated_wait(self):
        refusal = RateLimited("RATE_LIMITED", retry_after=8, model_scoped=True, per_minute=True)
        router, adapter, _, clock = _router({FIRST: refusal, SECOND: "345"})
        result = await router.solve(_request())
        assert (result.status, result.model_id) == ("ACCEPTED", SECOND)
        assert router.quota.benched_models("p") == {FIRST: clock.now + 8}

        # With its sibling out of the way, only the bench decides whether it is asked.
        adapter.outcomes[FIRST] = "345"
        router.quota.throttle_model("p", SECOND)
        clock.now += 7
        await router.solve(_request(task="15*23 again"))
        assert adapter.calls == [FIRST, SECOND]
        clock.now += 1
        result = await router.solve(_request(task="15*23 once more"))
        assert (result.status, result.model_id) == ("ACCEPTED", FIRST)

    async def test_the_router_passes_the_window_on_for_a_provider_wide_refusal(self):
        refusal = RateLimited("RATE_LIMITED", retry_after=8, per_minute=True)
        router, adapter, spec, clock = _router({FIRST: refusal, SECOND: "345"})
        await router.solve(_request())
        assert router.quota.state("p").throttled_until == clock.now + 8
        assert router.quota.effective_status(spec) == "THROTTLED"

    async def test_only_a_literal_true_counts_as_per_minute(self):
        refusal = RateLimited("RATE_LIMITED", retry_after=8, model_scoped=True)
        refusal.per_minute = "yes"
        router, _, _, clock = _router({FIRST: refusal, SECOND: "345"}, cooldown_seconds=360)
        await router.solve(_request())
        assert router.quota.benched_models("p") == {FIRST: clock.now + 360}


class TestAStatedWaitIsTrustedOnce:
    """If the next request is refused the same way, the wait did not hold: another
    application is filling the window, or the request can never fit it. Each further
    try is a request spent for nothing, so the hold grows back toward the cooldown."""

    def _refuse(self, governor, clock, wait=2):
        governor.throttle_model("p", FIRST, retry_after=wait, per_minute=True)
        return governor.benched_models("p")[FIRST] - clock.now

    def test_refusals_in_a_row_hold_for_the_wait_then_the_window_then_the_cooldown(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        holds = []
        for _ in range(4):
            holds.append(self._refuse(governor, clock))
            clock.now += holds[-1]  # asked again the moment the bench ends
        assert holds == [2, 60, 360, 360]

    def test_a_longer_stated_wait_is_never_cut_down(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        assert self._refuse(governor, clock, wait=900) == 900
        clock.now += 900
        assert self._refuse(governor, clock, wait=900) == 900
        clock.now += 900
        assert self._refuse(governor, clock, wait=900) == 900

    def test_an_answer_in_between_starts_the_count_again(self):
        """A model working at its limit is refused, waits, answers, and is refused
        again. That is the limit doing its job, and it keeps the short waits."""
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        for _ in range(5):
            hold = self._refuse(governor, clock, wait=7)
            assert hold == 7
            clock.now += hold
            governor.answered("p", FIRST)

    def test_a_refusal_long_after_the_last_bench_ended_is_a_new_episode(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        assert self._refuse(governor, clock) == 2
        clock.now += 2 + 60  # still inside the window after the bench: the same episode
        assert self._refuse(governor, clock) == 60
        clock.now += 60 + 61  # more than a window after it: a new one
        assert self._refuse(governor, clock) == 2

    def test_each_model_and_the_provider_keep_their_own_count(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        governor.throttle_model("p", FIRST, retry_after=2, per_minute=True)
        governor.throttle_model("p", SECOND, retry_after=2, per_minute=True)
        governor.throttle("p", retry_after=2, per_minute=True)
        assert governor.benched_models("p") == {FIRST: clock.now + 2, SECOND: clock.now + 2}
        assert governor.state("p").throttled_until == clock.now + 2
        clock.now += 2
        governor.throttle("p", retry_after=2, per_minute=True)
        assert governor.state("p").throttled_until == clock.now + 60
        # An answer from any model clears the provider's count and that model's own.
        clock.now += 60
        governor.answered("p", FIRST)
        governor.throttle("p", retry_after=2, per_minute=True)
        assert governor.state("p").throttled_until == clock.now + 2
        governor.throttle_model("p", SECOND, retry_after=2, per_minute=True)
        assert governor.benched_models("p")[SECOND] == clock.now + 60

    def test_a_limit_with_no_named_window_is_not_counted(self):
        clock = Clock()
        governor = _governor(clock, cooldown_seconds=360)
        governor.throttle_model("p", FIRST, retry_after=2)
        clock.now += 360
        assert self._refuse(governor, clock) == 2

    async def test_a_model_that_always_refuses_is_not_asked_every_two_seconds(self):
        """Measured before this: 180 requests in six minutes to a model that kept
        answering 'per minute, try again in 2s'. An unnamed limit gets one."""
        refusal = RateLimited("RATE_LIMITED", retry_after=2, model_scoped=True, per_minute=True)
        router, adapter, _, clock = _router({FIRST: refusal, SECOND: "345"})
        router.quota.throttle_model("p", SECOND, retry_after=10_000)  # only FIRST is routable
        for second in range(360):
            await router.solve(_request(task=f"15*23 #{second}"))
            clock.now += 1
        assert adapter.calls.count(FIRST) == 3

    async def test_the_router_reports_an_answer_so_short_waits_survive_real_use(self):
        refusal = RateLimited("RATE_LIMITED", retry_after=7, model_scoped=True, per_minute=True)
        router, adapter, _, clock = _router({FIRST: refusal, SECOND: "345"})
        router.quota.throttle_model("p", SECOND, retry_after=10_000)  # only FIRST is routable
        for _ in range(3):
            adapter.outcomes[FIRST] = refusal
            await router.solve(_request(task=f"refused at {clock.now}"))
            assert router.quota.benched_models("p")[FIRST] == clock.now + 7
            clock.now += 7
            adapter.outcomes[FIRST] = "345"
            result = await router.solve(_request(task=f"answered at {clock.now}"))
            assert (result.status, result.model_id) == ("ACCEPTED", FIRST)
            clock.now += 1


class TestARequestTooLargeIsAboutTheRequest:
    async def test_the_next_route_answers_and_the_provider_is_not_blamed(self):
        refusal = RequestTooLarge("REQUEST_TOO_LARGE_FOR_MODEL")
        router, adapter, spec, _ = _router({FIRST: refusal, SECOND: "345"})
        result = await router.solve(_request())
        first, second = result.attempts
        assert (str(first.disposition), first.error_type, first.error_detail) == (
            "CAPABILITY_MISMATCH",
            "REQUEST_TOO_LARGE_FOR_ROUTE",
            "REQUEST_TOO_LARGE_FOR_MODEL",
        )
        assert (result.status, result.model_id) == ("ACCEPTED", SECOND)
        state = router.quota.state("p")
        assert (state.failures, state.circuit_state, state.throttled_until) == ([], "CLOSED", 0)
        assert router.quota.benched_models("p") == {}
        assert router.quota.effective_status(spec) == "ACTIVE"
        # Nothing about how the model performs was learned from a refused size.
        assert [key[1] for key in router.performance._stats] == [SECOND]

    async def test_oversized_requests_never_open_the_circuit(self):
        """As HTTP_413 they did: three failures inside the window."""
        too_large = RequestTooLarge("HTTP_413")
        router, adapter, spec, _ = _router({FIRST: too_large, SECOND: too_large})
        for number in range(4):
            await router.solve(_request(task=f"15*23 #{number}"))
        assert len(adapter.calls) == 8
        assert router.quota.state("p").circuit_state == "CLOSED"
        assert router.quota.available(spec) is True

        outage = ProviderUnavailable("HTTP_503")
        router, adapter, spec, _ = _router({FIRST: outage, SECOND: outage})
        for number in range(4):
            await router.solve(_request(task=f"15*23 #{number}"))
        assert router.quota.state("p").circuit_state == "OPEN"

    async def test_the_request_was_made_so_its_charge_stands(self):
        router, _, _, _ = _router({FIRST: RequestTooLarge("HTTP_413"), SECOND: "345"})
        await router.solve(_request())
        assert router.quota.state("p").used == 2

        # Against FAIR's own refusal before dispatch, which is given back.
        router, _, _, _ = _router({FIRST: RequestNotSupported("TOO_BIG"), SECOND: "345"})
        await router.solve(_request())
        assert router.quota.state("p").used == 1

    async def test_it_spends_neither_attempt_budget(self):
        router, adapter, _, _ = _router(
            {FIRST: RequestTooLarge("HTTP_413"), SECOND: "345"},
            max_attempts=1,
            max_unanswered_attempts=0,
        )
        result = await router.solve(_request())
        assert (result.status, result.model_id) == ("ACCEPTED", SECOND)

    async def test_during_a_recovery_probe_it_is_no_verdict_on_the_provider(self):
        router, adapter, _, _ = _router({FIRST: RequestTooLarge("HTTP_413"), SECOND: "345"})
        state = router.quota.state("p")
        state.circuit_state, state.blocked_until = "OPEN", 0
        result = await router.solve(_request())
        assert (result.status, result.model_id) == ("ACCEPTED", SECOND)
        assert adapter.calls == [FIRST, SECOND]
        assert router.quota.state("p").circuit_state == "CLOSED"


class TestTheCredentialGuardKeepsBoth:
    def _guarded(self, error):
        inner = PerModel("p", {FIRST: error})
        return CredentialedAdapter("p", inner, SecretStr("not-in-any-message"))

    def _ask(self):
        return NormalizedModelRequest(
            task="ping", model_id=FIRST, request_id="r", client_id="c", task_class="general"
        )

    @pytest.mark.parametrize(
        "scoped, code",
        [(True, "MODEL_RATE_LIMITED_PER_MINUTE"), (False, "RATE_LIMITED_PER_MINUTE")],
    )
    async def test_a_per_minute_limit_keeps_its_window_and_says_so(self, scoped, code):
        error = RateLimited("anything", retry_after=4, model_scoped=scoped, per_minute=True)
        with pytest.raises(RateLimited, match=f"^{code}$") as raised:
            await self._guarded(error).complete(self._ask())
        assert (raised.value.per_minute, raised.value.model_scoped) == (True, scoped)
        assert raised.value.retry_after == 4

    async def test_a_limit_with_no_named_window_is_reported_as_before(self):
        error = RateLimited("anything", model_scoped=True)
        error.per_minute = "yes"
        with pytest.raises(RateLimited, match="^MODEL_RATE_LIMITED$") as raised:
            await self._guarded(error).complete(self._ask())
        assert raised.value.per_minute is False

    async def test_a_size_refusal_is_not_mistaken_for_one_fair_never_sent(self):
        """Re-raised as its parent, the router would refund a request that was made."""
        with pytest.raises(RequestTooLarge, match="^REQUEST_TOO_LARGE_FOR_MODEL$"):
            await self._guarded(RequestTooLarge("REQUEST_TOO_LARGE_FOR_MODEL")).complete(
                self._ask()
            )

    async def test_upstream_text_never_rides_out_on_a_size_refusal(self):
        error = RequestTooLarge("Request too large for model `x` in organization `org_secret`")
        with pytest.raises(RequestTooLarge, match="^REQUEST_TOO_LARGE$"):
            await self._guarded(error).complete(self._ask())
