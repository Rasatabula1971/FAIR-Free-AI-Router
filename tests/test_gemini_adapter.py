import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml
from conftest import provider, qualification
from pydantic import SecretStr, ValidationError

from fair.governor.policy import AdmissionDenied
from fair.providers.base import (
    AuthenticationFailed,
    MalformedResponse,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.live import GeminiAdapter, LiveSettings, register_live
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.schemas.api import SolveRequest
from fair.schemas.domain import NormalizedModelRequest
from fair.security.credentials import CredentialConfigurationError

MODEL = "gemini-test-001"
PROVIDER = "google_gemini_api"
SECRET = "offline-gemini-secret"


def spec(**changes):
    models = [{"model_id": MODEL, "context_window": 4096, "model_revision": "001"}]
    models = changes.get("models", models)
    return provider(
        PROVIDER,
        **(
            {
                "access_class": "FREE_RECURRING",
                "terms_last_verified": datetime.now(UTC) - timedelta(seconds=1),
                "models": models,
                "qualification": qualification(PROVIDER, models),
            }
            | changes
        ),
    )


def settings(**changes):
    return LiveSettings(**({"enabled": True, "gemini_free_tier_confirmed": True} | changes))


def metadata(**changes):
    return {
        "name": "models/" + MODEL,
        "version": "001",
        "supportedGenerationMethods": ["generateContent", "countTokens"],
        "inputTokenLimit": 8192,
        "outputTokenLimit": 8192,
    } | changes


def response(**changes):
    return {
        "modelVersion": MODEL,
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "4"}]}, "finishReason": "STOP"}
        ],
    } | changes


def request(**changes):
    return NormalizedModelRequest(
        **(
            {
                "task": "Compute 2 + 2. Return only the number.",
                "model_id": MODEL,
                "request_id": "private-request",
                "client_id": "private-client",
                "task_class": "commodity",
                "max_output_tokens": 32,
            }
            | changes
        )
    )


def adapter(handler, **changes):
    return GeminiAdapter(
        changes.get("spec", spec()),
        changes.get("settings", settings()),
        credential=SecretStr(SECRET),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("budget", [4097, 8192, 65536])
async def test_large_output_budget_is_independent_of_input_capacity(budget):
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(outputTokenLimit=65536))
        assert json.loads(req.content)["generationConfig"]["maxOutputTokens"] == budget
        return httpx.Response(200, json=response())

    assert (await adapter(handler).complete(request(max_output_tokens=budget))).text == "4"


async def test_configured_output_ceiling_rejects_before_network():
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=metadata() if req.method == "GET" else response())

    live = adapter(handler, settings=settings(gemini_max_output_tokens=8192))
    with pytest.raises(MalformedResponse, match="OUTPUT_BUDGET_INVALID"):
        await live.complete(request(max_output_tokens=8193))
    assert calls == []
    assert (await live.complete(request(max_output_tokens=8192))).text == "4"
    assert calls == ["GET", "POST"]


@pytest.mark.parametrize("budget", [0, 65537, True, 8192.0, "8192"])
def test_output_budget_requires_bounded_integer(budget):
    with pytest.raises(ValidationError):
        settings(gemini_max_output_tokens=budget)
    with pytest.raises(ValidationError):
        SolveRequest(client_id="alice", task="Calculate 2 + 2", max_output_tokens=budget)


async def test_router_forwards_large_budgets_and_cache_separates_them(make_router):
    budgets = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(outputTokenLimit=65536))
        budgets.append(json.loads(req.content)["generationConfig"]["maxOutputTokens"])
        return httpx.Response(200, json=response())

    reviewed = spec()
    live = adapter(handler, spec=reviewed)
    router = make_router([(reviewed, live)], cache_enabled=True)
    base = SolveRequest(
        client_id="alice",
        task="Compute 2 + 2. Return only the number.",
        validation={"kind": "arithmetic", "expression": "2 + 2"},
    )
    assert base.max_output_tokens == 1024
    for budget in [8192, 65536]:
        solve = base.model_copy(update={"max_output_tokens": budget})
        first = await router.solve(solve)
        again = await router.solve(solve)
        assert first.status == again.status == "ACCEPTED"
        assert not first.cache_hit and again.cache_hit
    assert budgets == [8192, 65536]


@pytest.mark.parametrize("json_mode", [False, True])
async def test_native_request_shape_auth_and_identity(json_mode):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.host == "generativelanguage.googleapis.com"
        assert req.url.scheme == "https" and not req.url.query
        assert req.headers["x-goog-api-key"] == SECRET
        assert "authorization" not in req.headers
        assert SECRET not in str(req.url) and SECRET.encode() not in req.content
        if req.method == "GET":
            assert req.url.path == "/v1beta/models/" + MODEL
            return httpx.Response(200, json=metadata())
        assert req.url.path == "/v1beta/models/" + MODEL + ":generateContent"
        payload = json.loads(req.content)
        assert set(payload) == {"contents", "generationConfig"}
        assert payload["contents"] == [{"role": "user", "parts": [{"text": request().task}]}]
        config = {"candidateCount": 1, "maxOutputTokens": 32}
        if json_mode:
            config.update(
                responseMimeType="application/json", responseJsonSchema={"type": "integer"}
            )
        assert payload["generationConfig"] == config
        assert b"private-" not in req.content
        return httpx.Response(200, json=response())

    live = adapter(handler)
    result = await live.complete(
        request(expected_json_schema={"type": "integer"} if json_mode else None)
    )
    assert result.text == "4" and result.model_id == MODEL and result.provider_id == PROVIDER
    assert result.finish_reason == "stop"
    assert [req.method for req in calls] == ["GET", "POST"]
    assert (await live.quota()).quota_remaining_estimate is None


@pytest.mark.parametrize("flag", ["enabled", "gemini_free_tier_confirmed"])
def test_opt_in_flags_required(flag):
    with pytest.raises(AuthenticationFailed):
        adapter(lambda req: pytest.fail("unexpected network"), settings=settings(**{flag: False}))


def test_missing_key_or_qualification_cannot_register(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(CredentialConfigurationError):
        register_live(Registry(), [spec()], settings())
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    with pytest.raises(AdmissionDenied):
        register_live(Registry(), [spec(qualification=None)], settings())


@pytest.mark.parametrize(
    "model",
    [
        "models/gemini-test",
        "../gemini-test",
        "gemini-a?key=x",
        "gemini-a%2F",
        "tunedModels/x",
        "gemma-4",
    ],
)
def test_paths_and_unrelated_model_families_blocked(model):
    with pytest.raises(AuthenticationFailed):
        adapter(
            lambda req: pytest.fail("unexpected network"),
            spec=spec(models=[{"model_id": model, "context_window": 4096}]),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"name": "models/unreviewed"},
        {"version": "002"},
        {"supportedGenerationMethods": ["embedContent"]},
        {"supportedGenerationMethods": "generateContent"},
        {"inputTokenLimit": 1024},
        {"inputTokenLimit": True},
        {"outputTokenLimit": 0},
        {"outputTokenLimit": None},
        {"outputTokenLimit": "8192"},
    ],
)
async def test_changed_or_ineligible_metadata_never_generates(change):
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=metadata(**change))

    with pytest.raises(AuthenticationFailed):
        await adapter(handler).complete(request())
    assert calls == ["GET"]


async def test_discovery_only_fetches_reviewed_active_models():
    calls = []
    models = [
        {"model_id": MODEL, "context_window": 4096},
        {"model_id": "gemini-inactive", "context_window": 4096, "active": False},
    ]

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=metadata())

    found = await adapter(handler, spec=spec(models=models)).list_models()
    assert [m.model_id for m in found] == [MODEL]
    assert calls == ["/v1beta/models/" + MODEL]


@pytest.mark.parametrize(
    "change",
    [
        {"model_id": "gemini-unreviewed"},
        {"max_output_tokens": 0},
        {"max_output_tokens": 65537},
        {"task": "x" * 4096},
        {"expected_json_schema": {"description": "x" * 4096}},
    ],
)
async def test_unreviewed_model_and_oversized_requests_stop_before_network(change):
    with pytest.raises(AuthenticationFailed if "model_id" in change else MalformedResponse):
        await adapter(lambda req: pytest.fail("unexpected network")).complete(request(**change))


async def test_model_output_limit_enforced_before_generation():
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=metadata(outputTokenLimit=16))

    with pytest.raises(MalformedResponse):
        await adapter(handler).complete(request())
    assert calls == ["GET"]


@pytest.mark.parametrize(
    "change",
    [
        {"modelVersion": "gemini-different"},
        {"modelVersion": None},
        {"candidates": []},
        {"candidates": [{}, {}]},
        {"candidates": None},
        {"promptFeedback": {"blockReason": "SAFETY"}},
    ],
)
async def test_invalid_envelope_or_identity_withholds_output(change):
    with pytest.raises(MalformedResponse):
        await adapter(
            lambda req: httpx.Response(
                200, json=metadata() if req.method == "GET" else response(**change)
            )
        ).complete(request())


@pytest.mark.parametrize(
    "part",
    [
        {"text": ""},
        {"text": "4", "functionCall": {}},
        {"inlineData": {"data": "test"}},
        {"executableCode": {}},
        {"text": "thought only", "thought": True},
        {"text": "4", "thought": "false"},
        None,
    ],
)
async def test_nontext_or_empty_output_rejected(part):
    data = response()
    data["candidates"][0]["content"]["parts"] = [part]
    with pytest.raises(MalformedResponse):
        await adapter(
            lambda req: httpx.Response(200, json=metadata() if req.method == "GET" else data)
        ).complete(request())


@pytest.mark.parametrize(
    "finish", ["SAFETY", "RECITATION", "MALFORMED_FUNCTION_CALL", "OTHER", None]
)
async def test_blocked_finish_reasons_are_not_accepted(finish):
    data = response()
    data["candidates"][0]["finishReason"] = finish
    with pytest.raises(MalformedResponse):
        await adapter(
            lambda req: httpx.Response(200, json=metadata() if req.method == "GET" else data)
        ).complete(request())


async def test_thought_parts_are_withheld_and_final_text_joined():
    data = response()
    data["candidates"][0]["content"]["parts"] = [
        {"text": "private reasoning", "thought": True},
        {"text": "4"},
        {"text": "2"},
    ]
    data["candidates"][0]["finishReason"] = "MAX_TOKENS"
    result = await adapter(
        lambda req: httpx.Response(200, json=metadata() if req.method == "GET" else data)
    ).complete(request())
    assert result.text == "42" and result.finish_reason == "length"


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, ProviderUnavailable),
        (401, AuthenticationFailed),
        (403, AuthenticationFailed),
        (404, ProviderUnavailable),
        (429, RateLimited),
        (402, QuotaExceeded),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
        (302, AuthenticationFailed),
    ],
)
async def test_errors_do_not_retry_or_expose_provider_body(status, expected):
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(
            status,
            headers={"retry-after": "30", "location": "https://example.org"},
            json={"error": {"code": status, "message": SECRET}},
        )

    with pytest.raises(expected) as caught:
        await adapter(handler).complete(request())
    assert SECRET not in str(caught.value)
    assert calls == ["GET"]
    if status == 429:
        assert caught.value.retry_after == 30


async def test_transport_timeout_is_sanitized():
    def handler(req):
        raise httpx.ReadTimeout(SECRET)

    with pytest.raises(ProviderUnavailable) as caught:
        await adapter(handler).complete(request())
    assert SECRET not in str(caught.value)


async def test_cancelling_generation_closes_stream():
    started, closed = asyncio.Event(), asyncio.Event()

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            closed.set()

    live = adapter(
        lambda req: (
            httpx.Response(200, json=metadata())
            if req.method == "GET"
            else httpx.Response(200, stream=HangingStream())
        )
    )
    work = asyncio.create_task(live.complete(request()))
    await asyncio.wait_for(started.wait(), 1)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert closed.is_set()


async def test_smoke_runs_through_scoped_registry_router_and_validator(tmp_path, monkeypatch):
    import fair.providers.smoke as smoke_module

    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setenv("GROQ_API_KEY", "other-provider-secret")
    (tmp_path / "providers.yaml").write_text(
        yaml.safe_dump({"providers": [spec().model_dump(mode="json")]}), encoding="utf-8"
    )
    (tmp_path / "live_adapters.yaml").write_text(
        yaml.safe_dump(settings().model_dump()), encoding="utf-8"
    )
    calls = []

    def handler(req):
        calls.append(req.method)
        assert req.headers["x-goog-api-key"] == SECRET
        assert b"other-provider-secret" not in req.content
        return httpx.Response(200, json=metadata() if req.method == "GET" else response())

    monkeypatch.setattr(
        smoke_module,
        "register_live",
        lambda registry, specs, config: register_live(
            registry, specs, config, httpx.MockTransport(handler)
        ),
    )
    result = await smoke_module.smoke(tmp_path, PROVIDER, MODEL)
    assert result["status"] == "ACCEPTED"
    assert result["verification_state"] == "DETERMINISTIC_ARITHMETIC"
    assert result["attempts"] == 1 and result["paid_inference_executed"] is False
    assert calls == ["GET", "POST"]


@pytest.mark.parametrize("status", [429, 503])
async def test_generation_failure_fails_over_without_repeating_gemini(make_router, status):
    calls = []

    def handler(req):
        calls.append(req.method)
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        return httpx.Response(
            status,
            headers={"retry-after": "30"},
            json={"error": {"code": status, "status": "RESOURCE_EXHAUSTED"}},
        )

    live = adapter(handler)
    fallback = MockAdapter("zz-local", text="4")
    router = make_router([(spec(), live), (provider("zz-local"), fallback)])
    result = await router.solve(
        SolveRequest(
            client_id="alice",
            task="Compute 2 + 2. Return only the number.",
            validation={"kind": "arithmetic", "expression": "2 + 2"},
        )
    )
    assert result.status == "ACCEPTED"
    assert [attempt.provider_id for attempt in result.attempts] == [PROVIDER, "zz-local"]
    assert calls == ["GET", "POST"]
    if status == 429:
        state = router.quota.state(PROVIDER)
        assert state.blocked_until > datetime.now(UTC).timestamp()
        assert state.reset_at is None
