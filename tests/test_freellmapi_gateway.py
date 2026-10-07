"""FreeLLMAPI gateway adapter — no network, no credentials leave the process.

The wire shapes here were recorded from an unmodified FreeLLMAPI v0.13.6: the
``X-Routed-Via`` header on buffered and streamed answers, a body ``model`` rewritten
to the upstream's id, and the error codes its failover loop returns.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from fair.embedded.module import FAIR
from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ModelUnavailable,
    ProviderUnavailable,
    RateLimited,
    RequestNotSupported,
)
from fair.providers.live import FreeLLMAPIAdapter, LiveSettings
from fair.providers.mock import MockAdapter
from fair.schemas.domain import NormalizedModelRequest, ProviderSpec
from fair.schemas.gateway import GatewayReview, load_gateway_review
from fair.schemas.qualification import ModelQualification, ProviderQualification

MODEL = "gpt-oss-120b"
UPSTREAM = "openai/gpt-oss-120b"
ROUTE = f"groq/{UPSTREAM}"
KEY = "freellmapi-test-key"


def _today():
    return datetime.now(UTC).date()


def _review(*models, reviewed_at=None):
    return {
        "reviewed_at": (reviewed_at or _today()).isoformat(),
        "reviewer_reference": "test",
        "models": list(models) or [_model()],
    }


def _model(model_id=MODEL, routes=(ROUTE,), **extra):
    return {"model_id": model_id, "context_window": 131072, "routes": list(routes), **extra}


def _spec(*model_ids, max_data_class="PUBLIC"):
    reviewed = datetime.now(UTC) - timedelta(seconds=10)
    return ProviderSpec(
        provider_id="freellmapi",
        access_class="FREE_RECURRING",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        terms_last_verified=reviewed,
        max_data_class=max_data_class,
        models=[
            {"model_id": model_id, "context_window": 131072, "capabilities": {"reasoning"}}
            for model_id in model_ids
        ],
        qualification=ProviderQualification(
            provider_id="freellmapi",
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
                    model_id=model_id,
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
                for model_id in model_ids
            ],
        ),
    )


def _settings(**overrides):
    return LiveSettings(enabled=True, confirmed_providers={"freellmapi"}, **overrides)


def _request(model_id=MODEL, **overrides):
    fields = {
        "task": "ping",
        "model_id": model_id,
        "request_id": "r",
        "client_id": "c",
        "task_class": "general",
        "max_output_tokens": 64,
    }
    return NormalizedModelRequest(**(fields | overrides))


def _catalog(*entries):
    return {"object": "list", "data": list(entries) or [_listed()]}


def _listed(model_id=MODEL, available=True, execution_status="ready"):
    return {"id": model_id, "available": available, "execution_status": execution_status}


def _sse(upstream_model, content="pong", route=ROUTE):
    """A streamed answer as the gateway frames it, routed-via header included."""
    events = [
        {"model": upstream_model, "choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"model": upstream_model, "choices": [{"delta": {"content": content}}]},
        {"model": upstream_model, "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"model": upstream_model, "choices": [], "usage": {"prompt_tokens": 1}},
    ]
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    headers = {"content-type": "text/event-stream"}
    if route is not None:
        headers["x-routed-via"] = route
    return httpx.Response(200, content=text.encode(), headers=headers)


def _refusal(status, code=None, **headers):
    error = {"message": "refused", "type": "rate_limit_error"}
    if code is not None:
        error["code"] = code
    return httpx.Response(status, json={"error": error}, headers=headers)


def _adapter(completion, *, catalog=None, models=(MODEL,), routes=None, **kwargs):
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json=catalog or _catalog())
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            return completion(request) if callable(completion) else completion
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    adapter = FreeLLMAPIAdapter(
        kwargs.pop("spec", None) or _spec(*models),
        kwargs.pop("settings", None) or _settings(),
        routes=routes if routes is not None else {MODEL: [ROUTE]},
        credential=kwargs.pop("credential", SecretStr(KEY)),
        transport=httpx.MockTransport(handler),
    )
    return adapter, seen


class TestReviewedRoute:
    async def test_an_answer_from_a_reviewed_route_is_returned_under_the_id_fair_sent(self):
        adapter, seen = _adapter(_sse(UPSTREAM))
        response = await adapter.complete(_request())
        # The gateway named the upstream's id; FAIR's own id is what the router checks.
        assert (response.provider_id, response.model_id) == ("freellmapi", MODEL)
        assert response.text == "pong"
        assert adapter.safe_diagnostics()["last_route"] == {
            "model_id": MODEL,
            "routed_via": ROUTE,
            "verdict": "REVIEWED",
        }
        await adapter.close()

    async def test_the_request_pins_the_model_on_the_loopback_gateway(self):
        adapter, seen = _adapter(_sse(UPSTREAM))
        await adapter.complete(_request())
        sent = seen[-1]
        assert str(sent.url) == "http://127.0.0.1:3001/v1/chat/completions"
        assert sent.headers["authorization"] == f"Bearer {KEY}"
        payload = json.loads(sent.content)
        assert payload["model"] == MODEL
        assert payload["stream"] is True
        assert payload["max_tokens"] == 64
        await adapter.close()

    @pytest.mark.parametrize("route", ["cerebras/gpt-oss-120b", "cache", "idempotency", None])
    async def test_an_answer_from_any_other_route_is_a_cost_nobody_vouched_for(self, route):
        adapter, _ = _adapter(_sse(UPSTREAM, route=route))
        with pytest.raises(BillingViolation, match="UNREVIEWED_GATEWAY_ROUTE"):
            await adapter.complete(_request())
        observed = adapter.safe_diagnostics()["last_route"]
        assert observed["verdict"] == "UNREVIEWED"
        assert observed["routed_via"] == (route or "ABSENT")
        await adapter.close()

    async def test_a_route_reviewed_for_one_model_does_not_cover_another(self):
        adapter, _ = _adapter(
            _sse(UPSTREAM),
            models=(MODEL, "llama-3.3-70b"),
            catalog=_catalog(_listed(), _listed("llama-3.3-70b")),
            routes={MODEL: [ROUTE], "llama-3.3-70b": ["groq/llama-3.3-70b-versatile"]},
        )
        with pytest.raises(BillingViolation):
            await adapter.complete(_request("llama-3.3-70b"))
        await adapter.close()

    async def test_a_body_naming_another_model_than_the_reported_route_is_malformed(self):
        adapter, _ = _adapter(_sse("some/other-model"))
        with pytest.raises(MalformedResponse, match="INVALID_CHAT_COMPLETION"):
            await adapter.complete(_request())
        await adapter.close()

    async def test_concurrent_answers_are_each_checked_against_their_own_route(self):
        """One adapter serves many solves; a shared 'last header' would cross them."""
        slow, fast = "model-slow", "model-fast"

        async def slow_body():
            yield b'data: {"model": "up-slow", "choices": [{"delta": {"content": "s"}}]}\n\n'
            await asyncio.sleep(0.05)  # the fast answer's headers arrive in this gap
            yield (
                b'data: {"model": "up-slow", "choices": [{"delta": {}, "finish_reason":'
                b' "stop"}]}\n\ndata: [DONE]\n\n'
            )

        def completion(request):
            if json.loads(request.content)["model"] == slow:
                return httpx.Response(
                    200,
                    content=slow_body(),
                    headers={"content-type": "text/event-stream", "x-routed-via": "a/up-slow"},
                )
            return _sse("up-fast", content="f", route="b/up-fast")

        adapter, _ = _adapter(
            completion,
            models=(slow, fast),
            catalog=_catalog(_listed(slow), _listed(fast)),
            routes={slow: ["a/up-slow"], fast: ["b/up-fast"]},
        )
        await adapter.list_models()

        async def later(request):
            await asyncio.sleep(0.01)
            return await adapter.complete(request)

        first, second = await asyncio.gather(
            adapter.complete(_request(slow)), later(_request(fast))
        )
        assert (first.model_id, first.text) == (slow, "s")
        assert (second.model_id, second.text) == (fast, "f")
        await adapter.close()


class TestAdmission:
    @pytest.mark.parametrize("virtual", ["auto", "AUTO", "auto:coding", "auto:smart", "fusion"])
    def test_ids_that_route_across_the_whole_pool_are_refused(self, virtual):
        with pytest.raises(AuthenticationFailed, match="GATEWAY_VIRTUAL_MODEL_NOT_ALLOWED"):
            _adapter(_sse(UPSTREAM), models=(virtual,), routes={virtual: [ROUTE]})

    def test_a_model_without_a_reviewed_route_is_refused(self):
        with pytest.raises(AuthenticationFailed, match="GATEWAY_ROUTE_REVIEW_REQUIRED"):
            _adapter(_sse(UPSTREAM), routes={MODEL: []})

    def test_loopback_does_not_make_the_gateway_local(self):
        with pytest.raises(AuthenticationFailed, match="REMOTE_ADAPTER_PUBLIC_ONLY"):
            _adapter(_sse(UPSTREAM), spec=_spec(MODEL, max_data_class="RESTRICTED"))

    def test_the_gateway_key_is_required(self):
        with pytest.raises(AuthenticationFailed, match="PROVIDER_CREDENTIAL_REQUIRED"):
            _adapter(_sse(UPSTREAM), credential=SecretStr(" "))

    def test_the_operator_has_to_confirm_the_accounts_behind_it(self):
        settings = LiveSettings(enabled=True, confirmed_providers={"groq"})
        with pytest.raises(AuthenticationFailed, match="LIVE_ACCOUNT_REVIEW_REQUIRED"):
            _adapter(_sse(UPSTREAM), settings=settings)

    @pytest.mark.parametrize(
        "url",
        ["https://127.0.0.1:3001", "http://10.0.0.5:3001", "http://gateway.lan:3001"],
    )
    def test_the_gateway_has_to_be_a_literal_loopback_endpoint(self, url):
        with pytest.raises(ValidationError, match="FreeLLMAPI requires a literal loopback"):
            LiveSettings(freellmapi_url=url)


class TestCatalog:
    @pytest.mark.parametrize(
        "entry, reason",
        [
            (_listed(available=False, execution_status="needsKey"), "NO_USABLE_KEY_IN_GATEWAY"),
            (_listed(execution_status="exhausted"), "EXHAUSTED_IN_GATEWAY"),
            (_listed("something-else"), "ABSENT_FROM_CATALOG"),
        ],
    )
    async def test_a_model_the_gateway_cannot_serve_is_not_asked_for(self, entry, reason):
        adapter, seen = _adapter(_sse(UPSTREAM), catalog=_catalog(entry))
        with pytest.raises(ModelUnavailable):
            await adapter.complete(_request())
        assert adapter.safe_diagnostics()["catalog_drops"] == {MODEL: reason}
        assert [request.method for request in seen] == ["GET"]
        await adapter.close()

    async def test_the_same_id_listed_twice_is_ambiguous(self):
        adapter, _ = _adapter(_sse(UPSTREAM), catalog=_catalog(_listed(), _listed()))
        assert await adapter.list_models() == []
        assert adapter.safe_diagnostics()["catalog_drops"] == {MODEL: "AMBIGUOUS_IN_CATALOG"}
        await adapter.close()


class TestRefusals:
    @pytest.mark.parametrize(
        "status, code, expected",
        [
            (429, "rate_limit_exceeded", "GATEWAY_MODEL_EXHAUSTED"),
            (429, "quota_exceeded", "GATEWAY_MODEL_EXHAUSTED"),
            (429, "routing_exhausted", "GATEWAY_MODEL_EXHAUSTED"),
            (404, "model_not_found", "GATEWAY_MODEL_NOT_FOUND"),
            (503, "no_providers_configured", "GATEWAY_MODEL_HAS_NO_KEY"),
        ],
    )
    async def test_one_pinned_model_failing_does_not_implicate_the_gateway(
        self, status, code, expected
    ):
        adapter, _ = _adapter(_refusal(status, code))
        with pytest.raises(ModelUnavailable, match=expected):
            await adapter.complete(_request())
        await adapter.close()

    async def test_the_gateways_own_limiter_still_throttles_the_provider(self):
        adapter, _ = _adapter(_refusal(429, **{"retry-after": "7"}))
        with pytest.raises(RateLimited) as raised:
            await adapter.complete(_request())
        assert not isinstance(raised.value, ModelUnavailable)
        assert raised.value.retry_after == 7
        await adapter.close()

    async def test_a_request_too_large_for_every_route_is_not_a_provider_failure(self):
        adapter, _ = _adapter(_refusal(413, "context_length_exceeded"))
        with pytest.raises(RequestNotSupported, match="GATEWAY_CONTEXT_EXCEEDED"):
            await adapter.complete(_request())
        await adapter.close()

    async def test_an_upstream_failure_the_gateway_could_not_route_around(self):
        adapter, _ = _adapter(_refusal(502, "provider_authentication_failed"))
        with pytest.raises(ProviderUnavailable, match="HTTP_502"):
            await adapter.complete(_request())
        await adapter.close()

    async def test_a_rejected_gateway_key_is_an_authentication_failure(self):
        adapter, _ = _adapter(_refusal(401))
        with pytest.raises(AuthenticationFailed) as raised:
            await adapter.complete(_request())
        assert not isinstance(raised.value, ModelUnavailable)
        await adapter.close()


class TestHeldStream:
    def test_a_stream_is_given_the_whole_budget_not_a_per_chunk_one(self):
        """The gateway sends nothing while it fails over or validates a JSON answer."""
        adapter, _ = _adapter(_sse(UPSTREAM))
        timeout = adapter._completion_timeout(_request(max_output_tokens=4096), streaming=True)
        settings = adapter.settings
        assert timeout.read == pytest.approx(
            settings.read_timeout_seconds + 4096 / settings.output_tokens_per_second
        )
        assert timeout.read > settings.read_timeout_seconds


class TestReview:
    def test_a_review_names_models_and_the_routes_checked_for_each(self):
        review = load_gateway_review(_review(_model(structured_output=True)))
        assert review.models[0].routes == [ROUTE]
        assert review.reviewed == datetime(*_today().timetuple()[:3], tzinfo=UTC)

    @pytest.mark.parametrize("route", ["cache", "idempotency", "groq/", "/model", "a b/c", ""])
    def test_a_route_is_a_platform_and_an_upstream_model(self, route):
        with pytest.raises(ValidationError):
            GatewayReview.model_validate(_review(_model(routes=[route])))

    @pytest.mark.parametrize(
        "broken",
        [
            _review(_model(), _model()),
            _review(_model(routes=[ROUTE, ROUTE])),
            _review(_model(routes=[])),
            _review(_model(api_key="never-here")),
            {"reviewer_reference": "t", "models": [_model()]},
        ],
    )
    def test_an_incomplete_or_ambiguous_review_is_refused(self, broken):
        with pytest.raises(ValidationError):
            GatewayReview.model_validate(broken)

    def test_a_review_is_read_from_a_json_file(self, tmp_path):
        path = tmp_path / "freellmapi.review.json"
        path.write_text(json.dumps(_review()), encoding="utf-8")
        assert load_gateway_review(str(path)).models[0].model_id == MODEL


def _offline(provider_id="offline"):
    """A second, unrelated provider, so a skipped gateway is not a constructor error."""
    spec = ProviderSpec(
        provider_id=provider_id,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        models=[{"model_id": "m", "context_window": 8192, "capabilities": {"reasoning"}}],
    )
    return spec, MockAdapter(provider_id, text="345")


def _fair(**kwargs):
    fields = {
        "freellmapi_api_key": KEY,
        "freellmapi_review": _review(),
        "confirmed_free_providers": {"freellmapi"},
        "providers": [_offline()],
        "cache_enabled": False,
    }
    return FAIR(**(fields | kwargs))


def _serve(fair, completion, catalog=None):
    """Point the registered gateway adapter at a transport instead of a socket."""
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=catalog or _catalog())
        return completion(request) if callable(completion) else completion

    inner = fair._registry.adapters["freellmapi"]._adapter
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


ARITHMETIC = {"kind": "arithmetic", "expression": "15*23"}


class TestRegistration:
    def test_a_confirmed_gateway_with_a_current_review_registers(self):
        fair = _fair(freellmapi_review=_review(_model(structured_output=True)))
        listed = {entry["provider_id"]: entry for entry in fair.providers()}
        assert "freellmapi" not in fair.skipped
        assert listed["freellmapi"]["access_class"] == "FREE_RECURRING"
        assert listed["freellmapi"]["models"] == [MODEL]
        spec = fair._registry.providers["freellmapi"]
        assert spec.max_data_class == "PUBLIC"
        assert spec.request_limit is None
        assert "structured_output" in spec.models[0].capabilities

    def test_structured_output_is_off_until_a_review_says_otherwise(self):
        fair = _fair()
        capabilities = fair._registry.providers["freellmapi"].models[0].capabilities
        assert "structured_output" not in capabilities

    def test_a_key_alone_registers_nothing(self):
        fair = _fair(confirmed_free_providers=set())
        assert "confirmation required" in fair.skipped["freellmapi"]
        assert "freellmapi" not in fair._registry.providers

    def test_a_gateway_without_a_review_is_skipped(self):
        fair = _fair(freellmapi_review=None)
        assert "FREELLMAPI_REVIEW is required" in fair.skipped["freellmapi"]

    def test_an_invalid_review_is_skipped_without_quoting_it(self, tmp_path):
        path = tmp_path / "review.json"
        path.write_text('{"reviewed_at": "2026-10-07", "secret": "do-not-echo"}', encoding="utf-8")
        fair = _fair(freellmapi_review=str(path))
        assert fair.skipped["freellmapi"] == "gateway review is missing or invalid"
        assert _fair(freellmapi_review=str(tmp_path / "absent.json")).skipped["freellmapi"]

    @pytest.mark.parametrize("age_days", [31, -2])
    def test_a_stale_or_post_dated_review_is_skipped(self, age_days):
        fair = _fair(freellmapi_review=_review(reviewed_at=_today() - timedelta(days=age_days)))
        assert "expired or post-dated" in fair.skipped["freellmapi"]

    def test_a_gateway_off_this_host_is_skipped(self):
        fair = _fair(freellmapi_url="http://192.168.1.20:3001")
        assert "literal loopback" in fair.skipped["freellmapi"]

    @pytest.mark.parametrize("url", ["http://localhost:3001/v1", "http://127.0.0.1:3001/"])
    def test_the_usual_spellings_of_the_local_gateway_are_accepted(self, url):
        fair = _fair(freellmapi_url=url)
        assert fair._registry.adapters["freellmapi"]._adapter.base_url == "http://127.0.0.1:3001/v1"

    def test_a_virtual_model_in_the_review_is_skipped(self):
        fair = _fair(freellmapi_review=_review(_model("auto")))
        assert "never auto or fusion" in fair.skipped["freellmapi"]

    def test_the_gateway_is_configured_from_the_environment_file(self, tmp_path):
        review = tmp_path / "freellmapi.review.json"
        review.write_text(json.dumps(_review()), encoding="utf-8")
        env = tmp_path / ".env"
        env.write_text(
            f"FREELLMAPI_API_KEY={KEY}\n"
            "FREELLMAPI_URL=http://127.0.0.1:3901\n"
            f"FREELLMAPI_REVIEW={review}\n",
            encoding="utf-8",
        )
        fair = FAIR(
            env_file=str(env), confirmed_free_providers={"freellmapi"}, providers=[_offline()]
        )
        adapter = fair._registry.adapters["freellmapi"]._adapter
        assert adapter.base_url == "http://127.0.0.1:3901/v1"

    def test_an_unknown_confirmation_is_still_an_error(self):
        with pytest.raises(ValueError, match="Unknown confirmed free provider"):
            _fair(confirmed_free_providers={"freellmapi", "not-a-provider"})


class TestThroughTheRouter:
    async def test_a_verified_answer_comes_back_through_the_gateway(self):
        fair = _fair(providers=None)
        seen = _serve(fair, _sse(UPSTREAM, content="345"))
        result = await fair.solve("What is 15*23?", validation=ARITHMETIC)
        assert (result.status, result.provider_id, result.model_id) == (
            "ACCEPTED",
            "freellmapi",
            MODEL,
        )
        assert [request.method for request in seen] == ["GET", "POST"]
        await fair.close()

    async def test_nothing_above_public_reaches_the_gateway(self):
        fair = _fair(providers=None)
        seen = _serve(fair, _sse(UPSTREAM, content="345"))
        result = await fair.solve(
            "Summarise this internal note", privacy_class="CONFIDENTIAL", validation=ARITHMETIC
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert seen == []
        await fair.close()

    async def test_an_unreviewed_route_blocks_the_gateway_and_nothing_else(self):
        fair = _fair()
        _serve(fair, _sse(UPSTREAM, content="345", route="cerebras/gpt-oss-120b"))
        fair._router.performance.scores = lambda provider_id, *a, **k: (
            (1.0, 1.0) if provider_id == "freellmapi" else (0.0, 0.0)
        )
        result = await fair.solve("What is 15*23?", validation=ARITHMETIC)
        attempts = [(a.provider_id, str(a.disposition), a.error_type) for a in result.attempts]
        assert attempts[0] == ("freellmapi", "INFRA_FAILURE", "PROVIDER_COST_POLICY_VIOLATION")
        assert (result.status, result.provider_id) == ("ACCEPTED", "offline")
        status = {entry["provider_id"]: str(entry["status"]) for entry in fair.providers()}
        assert status == {"freellmapi": "SECURITY_BLOCKED", "offline": "ACTIVE"}
        await fair.close()

    async def test_an_exhausted_model_leaves_its_siblings_routable(self):
        review = _review(_model("a-first", ["groq/a"]), _model("b-second", ["groq/b"]))
        fair = _fair(providers=None, freellmapi_review=review)

        def completion(request):
            if json.loads(request.content)["model"] == "a-first":
                return _refusal(429, "rate_limit_exceeded", **{"retry-after": "600"})
            return _sse("b", content="345", route="groq/b")

        _serve(fair, completion, _catalog(_listed("a-first"), _listed("b-second")))
        result = await fair.solve("What is 15*23?", validation=ARITHMETIC)
        attempts = [(a.model_id, str(a.disposition), a.error_detail) for a in result.attempts]
        assert attempts == [
            ("a-first", "INFRA_FAILURE", "GATEWAY_MODEL_EXHAUSTED"),
            ("b-second", "ACCEPTED", None),
        ]
        assert str(fair.providers()[0]["status"]) == "ACTIVE"
        await fair.close()

    async def test_valid_json_of_the_wrong_shape_is_refused_by_fair_not_the_gateway(self):
        """The gateway only checks that an answer parses; the schema is FAIR's to hold."""
        fair = _fair(providers=None, freellmapi_review=_review(_model(structured_output=True)))
        _serve(fair, _sse(UPSTREAM, content='{"answer": "looks good"}'))
        schema = {
            "type": "object",
            "properties": {"concept_id": {"type": "string"}, "score": {"type": "integer"}},
            "required": ["concept_id", "score"],
            "additionalProperties": False,
        }
        result = await fair.solve(
            "Score the concept", expected_schema=schema, quality_level="commodity"
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert [str(a.disposition) for a in result.attempts] == ["QUALITY_FAILURE"]
        await fair.close()
