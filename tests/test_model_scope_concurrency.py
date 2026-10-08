"""Two solves at once must not turn one model's limit into the provider's.

Both cases were found by an independent audit of the per-model change and are
driven here through a real GroqAdapter, the credential guard and the router, with
the interleaving forced rather than hoped for.

- One model's daily refusal arrives while a sibling is mid-stream. The adapter
  used to keep the refusal's zero-remaining snapshot as its own, and the sibling's
  answer carried it back to the governor as the provider's allowance.
- A model is benched after another solve selected it but before that solve
  reserved its request. With a shared ledger the reservation is a thread hop
  away, and only the selector had checked the bench.
"""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from fair.config import RoutingSettings
from fair.embedded.module import _CLOUD_PROVIDERS
from fair.embedded.quota import SharedQuotaLedger
from fair.embedded.router import EmbeddedRouter
from fair.providers.live import GroqAdapter, LiveSettings
from fair.providers.registry import Registry
from fair.quality.thresholds import DEFAULT_THRESHOLDS
from fair.schemas.api import SolveRequest
from fair.schemas.domain import ProviderSpec
from fair.schemas.qualification import ModelQualification, ProviderQualification
from fair.security.adapter import CredentialedAdapter

BIG, SMALL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
CATALOG = {"data": [{"id": m, "context_window": 131072, "active": True} for m in (BIG, SMALL)]}
ASK = {
    "client_id": "c",
    "task": "15*23",
    "validation": {"kind": "arithmetic", "expression": "15*23"},
}
REFUSED = (
    "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service "
    "tier `on_demand` on {limit}: Limit 1000, Used 1000, Requested 1. Please try again in 30s."
)


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC).timestamp()

    def __call__(self):
        return self.now


def _spec():
    reviewed = datetime.now(UTC) - timedelta(seconds=10)
    return ProviderSpec(
        provider_id="groq",
        access_class="FREE_RECURRING",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        terms_last_verified=reviewed,
        request_limit=1000,
        request_limit_window="DAILY_UTC",
        models=[
            {
                "model_id": model,
                "context_window": 131072,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
            for model in (BIG, SMALL)
        ],
        qualification=ProviderQualification(
            provider_id="groq",
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
            models=[
                ModelQualification(
                    model_id=model,
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
                for model in (BIG, SMALL)
            ],
        ),
    )


def _frames(model):
    events = [
        {"model": model, "choices": [{"delta": {"role": "assistant"}}]},
        {"model": model, "choices": [{"delta": {"content": "345"}}]},
        {"model": model, "choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    return [f"data: {json.dumps(event)}\n\n".encode() for event in events] + [b"data: [DONE]\n\n"]


def _routed(handler, ledger=None):
    """A real Groq adapter behind the credential guard, on a clock the test owns."""
    clock, spec = Clock(), _spec()
    adapter = GroqAdapter(
        spec,
        LiveSettings(enabled=True, confirmed_providers=set(_CLOUD_PROVIDERS)),
        credential=SecretStr("k"),
        transport=httpx.MockTransport(handler),
        clock=clock,
    )
    registry = Registry()
    registry.register(spec, CredentialedAdapter("groq", adapter, SecretStr("not-in-any-message")))
    router = EmbeddedRouter(
        registry, RoutingSettings(), dict(DEFAULT_THRESHOLDS), quota_ledger=ledger
    )
    router.quota.clock = clock
    return router, adapter, spec


async def test_a_daily_refusal_on_one_model_does_not_ride_a_siblings_answer_to_the_provider():
    streaming, finish, refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    asked = {BIG: 0, SMALL: 0}
    spent = {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "2h3m",
    }
    healthy = {
        "content-type": "text/event-stream",
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-requests": "900",
        "x-ratelimit-reset-requests": "1h",
    }

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=CATALOG)
        model = json.loads(request.content)["model"]
        asked[model] += 1
        if model == BIG:
            if asked[BIG] == 1:
                # Already in flight when its cap is hit: the refusal lands while
                # another solve is part-way through an answer from the sibling.
                await streaming.wait()
            body = {"error": {"message": REFUSED.format(limit="requests per day (RPD)")}}
            return httpx.Response(429, json=body, headers=spent)
        if asked[SMALL] > 1:
            await asyncio.Event().wait()  # the earlier solve's own fallback stays out of it

        async def body():
            frames = _frames(SMALL)
            yield frames[0]
            streaming.set()
            await finish.wait()
            for frame in frames[1:]:
                yield frame

        return httpx.Response(200, headers=healthy, content=body())

    router, adapter, spec = _routed(handler)
    original = adapter._error

    def watched(status, headers, **scope):
        try:
            original(status, headers, **scope)
        finally:
            if streaming.is_set():
                refused.set()

    adapter._error = watched

    earlier = asyncio.create_task(router.solve(SolveRequest.model_validate(ASK)))
    await asyncio.sleep(0)
    later = asyncio.create_task(router.solve(SolveRequest.model_validate(ASK)))
    await refused.wait()
    finish.set()
    result = await later
    earlier.cancel()

    assert (result.status, result.model_id) == ("ACCEPTED", SMALL)
    assert list(router.quota.benched_models("groq")) == [BIG]
    assert router.quota.state("groq").exhausted is False
    assert router.quota.effective_status(spec) == "ACTIVE"
    assert router.quota.available(spec) is True


async def test_a_model_benched_before_its_request_is_reserved_is_not_sent_one(tmp_path):
    ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
    in_flight, let_refuse = asyncio.Event(), asyncio.Event()
    armed, held_up, go_on = threading.Event(), threading.Event(), threading.Event()
    sent = []
    router = None

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=CATALOG)
        model = json.loads(request.content)["model"]
        sent.append((model, BIG in router.quota.benched_models("groq")))
        if model == BIG:
            if not in_flight.is_set():
                in_flight.set()
                await let_refuse.wait()
            body = {"error": {"message": REFUSED.format(limit="tokens per minute (TPM)")}}
            return httpx.Response(429, json=body, headers={"retry-after": "30"})
        return httpx.Response(
            200, content=b"".join(_frames(SMALL)), headers={"content-type": "text/event-stream"}
        )

    router, _, spec = _routed(handler, ledger)
    reserve = ledger.reserve

    def slow_reserve(*args):
        # One ordinary SQLite write that happens to take a while.
        if armed.is_set() and not held_up.is_set():
            held_up.set()
            go_on.wait(10)
        return reserve(*args)

    ledger.reserve = slow_reserve

    first = asyncio.create_task(router.solve(SolveRequest.model_validate(ASK)))
    await in_flight.wait()
    armed.set()
    second = asyncio.create_task(router.solve(SolveRequest.model_validate(ASK)))
    while not held_up.is_set():
        await asyncio.sleep(0.005)
    let_refuse.set()  # the first solve's refusal lands while the second is reserving
    while BIG not in router.quota.benched_models("groq"):
        await asyncio.sleep(0.005)
    go_on.set()
    one, two = await asyncio.gather(first, second)

    assert (BIG, True) not in sent, "a request went to a model that was already benched"
    assert [model for model, _ in sent] == [BIG, SMALL, SMALL]
    assert (one.model_id, two.model_id) == (SMALL, SMALL)
    assert [attempt.model_id for attempt in two.attempts] == [SMALL]
    # The reservation for the request that was never sent was given back: three
    # were sent, three are counted, here and in the shared ledger.
    assert router.quota.state("groq").used == 3
    assert ledger.remaining("groq", 1000, router.quota.clock()) == 997
