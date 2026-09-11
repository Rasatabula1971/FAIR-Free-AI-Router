import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from apps.api.main import create_app
from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.live import (
    GroqAdapter,
    LiveSettings,
    OllamaLocalAdapter,
    OpenRouterFreeAdapter,
    duration,
    register_live,
    retry_seconds,
)
from fair.providers.registry import Registry
from fair.schemas.db import AuditEvent, TaskRequest
from fair.schemas.domain import NormalizedModelRequest, QuotaSnapshot
from fair.security.adapter import CredentialedAdapter
from fair.security.credentials import CredentialConfigurationError

NOW = datetime(2026, 9, 9, tzinfo=UTC)
CLASSES = {
    "groq": GroqAdapter,
    "openrouter_free": OpenRouterFreeAdapter,
    "ollama_local": OllamaLocalAdapter,
}


def spec_for(name="groq", **changes):
    return provider(
        name,
        **(
            {
                "access_class": {
                    "groq": "FREE_RECURRING",
                    "openrouter_free": "FREE_DYNAMIC",
                    "ollama_local": "FREE_LOCAL",
                }[name],
                "terms_last_verified": NOW,
                "models": [
                    {
                        "model_id": "owner/model:free" if name == "openrouter_free" else "model",
                        "context_window": 4096,
                    }
                ],
            }
            | changes
        ),
    )


def settings(**changes):
    return LiveSettings(
        **(
            {
                "enabled": True,
                "groq_free_plan_confirmed": True,
                "openrouter_free_account_confirmed": True,
                "ollama_local_only_confirmed": True,
            }
            | changes
        )
    )


def request(model="model", **changes):
    return NormalizedModelRequest(
        **(
            {
                "task": "Return only 4",
                "model_id": model,
                "request_id": "private-request",
                "client_id": "private-client",
                "task_class": "commodity",
                "max_output_tokens": 32,
            }
            | changes
        )
    )


def completion(model="model", **changes):
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": "4"}, "finish_reason": "stop"}],
        "usage": {"cost": 0},
    } | changes


def catalog(model="model"):
    return {
        "data": [
            {
                "id": model,
                "context_window": 8192,
                "context_length": 8192,
                "pricing": {"prompt": "0", "completion": "0", "request": "0"},
            }
        ]
    }


def adapter(name="groq", handler=None, spec=None, config=None):
    return CLASSES[name](
        spec or spec_for(name),
        config or settings(),
        credential=None if name == "ollama_local" else SecretStr("test-provider-secret"),
        transport=httpx.MockTransport(handler) if handler else None,
        clock=lambda: NOW.timestamp(),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:11434",
        "http://localhost:11434",
        "http://example.org",
        "http://192.168.1.2",
        "http://127.0.0.1@evil.org",
        "http://u:p@127.0.0.1",
        "http://127.0.0.1/api",
        "http://127.0.0.1?x=y",
        "http://127.0.0.1#x",
    ],
)
def test_local_endpoint_rejects_remote_or_ambiguous_urls(url):
    with pytest.raises(ValidationError):
        LiveSettings(ollama_url=url)


@pytest.mark.parametrize("name", CLASSES)
def test_live_opt_in_required(name):
    with pytest.raises(AuthenticationFailed):
        adapter(name, config=LiveSettings())
    registry = Registry()
    register_live(registry, [spec_for(name)], LiveSettings())
    assert not registry.adapters


@pytest.mark.parametrize(
    "changes",
    [
        {"terms_last_verified": NOW - timedelta(days=31)},
        {"terms_last_verified": NOW + timedelta(seconds=1)},
        {"terms_last_verified": NOW.replace(tzinfo=None)},
        {"max_data_class": "INTERNAL"},
        {"access_class": "FREE_LOCAL"},
        {"models": []},
        {
            "models": [
                {"model_id": "model", "context_window": 4096, "capabilities": ["tool_calling"]}
            ]
        },
    ],
)
def test_unreviewed_cloud_settings_fail_before_network(changes):
    with pytest.raises(AuthenticationFailed):
        adapter(spec=spec_for(**changes))


def test_wrong_adapter_and_missing_credential_denied():
    with pytest.raises(AuthenticationFailed):
        GroqAdapter(spec_for("openrouter_free"), settings(), credential=SecretStr("key"))
    with pytest.raises(AuthenticationFailed):
        GroqAdapter(spec_for(), settings())


@pytest.mark.parametrize(
    "model", ["owner/model", "openrouter/auto:free", "owner/model:paid", "owner/model:free/extra"]
)
def test_openrouter_requires_explicit_free_model(model):
    with pytest.raises(AuthenticationFailed):
        adapter(
            "openrouter_free",
            spec=spec_for("openrouter_free", models=[{"model_id": model, "context_window": 4096}]),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["groq", "openrouter_free"])
async def test_cloud_request_shape_and_allowlist(name):
    model = spec_for(name).models[0].model_id
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.scheme == "https"
        assert req.headers["authorization"] == "Bearer test-provider-secret"
        if req.url.path.endswith("/models"):
            body = catalog(model)
            body["data"].append({"id": "unreviewed"})
            return httpx.Response(200, json=body)
        if req.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"is_free_tier": True}})
        body = json.loads(req.content)
        assert body["messages"] == [{"role": "user", "content": "Return only 4"}]
        assert "private-" not in req.content.decode()
        assert body["stream"] is False
        assert body["response_format"]["json_schema"]["schema"] == {"type": "integer"}
        if name == "groq":
            assert body["max_completion_tokens"] == 32 and "max_tokens" not in body
        else:
            assert body["provider"] == {
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
            }
        return httpx.Response(200, json=completion(model))

    live = adapter(name, handler)
    result = await live.complete(request(model, expected_json_schema={"type": "integer"}))
    assert result.text == "4"
    assert len(calls) == (3 if name == "openrouter_free" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pricing",
    [
        {"prompt": "0", "completion": "0"},
        {"prompt": "0", "completion": "0", "request": "0.001"},
        {"prompt": "0", "completion": "0", "request": "0", "web_search": "1"},
        {"prompt": "NaN", "completion": "0", "request": "0"},
        None,
    ],
)
async def test_nonzero_or_unknown_catalog_prices_prevent_inference(pricing):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        body = catalog("owner/model:free")
        body["data"][0]["pricing"] = pricing
        return httpx.Response(200, json=body)

    with pytest.raises(AuthenticationFailed):
        await adapter("openrouter_free", handler).complete(request("owner/model:free"))
    assert calls == ["/api/v1/models"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, None, "true", 1])
async def test_openrouter_paid_or_unknown_account_prevents_chat(flag):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(
            200,
            json=catalog("owner/model:free")
            if req.url.path.endswith("/models")
            else {"data": {"is_free_tier": flag}},
        )

    with pytest.raises(AuthenticationFailed):
        await adapter("openrouter_free", handler).complete(request("owner/model:free"))
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [1, "0.0001", "NaN", None, True])
async def test_cost_uncertainty_raises_billing_violation(cost):
    def handler(req):
        body = (
            catalog("owner/model:free")
            if req.url.path.endswith("/models")
            else {"data": {"is_free_tier": True}}
            if req.url.path.endswith("/key")
            else completion("owner/model:free", usage={"cost": cost})
        )
        return httpx.Response(200, json=body)

    with pytest.raises(BillingViolation):
        await adapter("openrouter_free", handler).complete(request("owner/model:free"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (301, AuthenticationFailed),
        (401, AuthenticationFailed),
        (403, AuthenticationFailed),
        (402, QuotaExceeded),
        (429, RateLimited),
        (503, ProviderUnavailable),
    ],
)
async def test_errors_normalized_without_provider_text_or_redirect(status, expected):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status,
            headers={"location": "https://evil.org", "retry-after": "120"},
            text="sensitive-provider-detail",
        )

    with pytest.raises(expected) as caught:
        await adapter(handler=handler).list_models()
    assert len(calls) == 1 and "sensitive" not in str(caught.value)
    if status == 429:
        assert caught.value.retry_after == 120


@pytest.mark.asyncio
async def test_groq_request_headers_exhaust_but_token_headers_only_throttle():
    headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "2m59.56s",
    }
    live = adapter(handler=lambda req: httpx.Response(429, headers=headers))
    with pytest.raises(QuotaExceeded) as caught:
        await live.list_models()
    assert caught.value.reset_at == pytest.approx(NOW.timestamp() + 179.56)
    live = adapter(
        handler=lambda req: httpx.Response(
            429, headers={"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1m"}
        )
    )
    with pytest.raises(RateLimited):
        await live.list_models()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [b'{"data":[],"data":[]}', b'{"data":NaN}', b"[]", b"\xff", b" " * 4_000_001],
    ids=["duplicate", "nonfinite", "array", "utf8", "oversized"],
)
async def test_malformed_and_oversized_json_rejected(body):
    with pytest.raises(MalformedResponse):
        await adapter(handler=lambda req: httpx.Response(200, content=body)).list_models()


@pytest.mark.asyncio
async def test_http_200_error_and_transport_timeout():
    with pytest.raises(RateLimited):
        await adapter(
            handler=lambda req: httpx.Response(200, json={"error": {"code": 429}})
        ).list_models()

    def timeout(req):
        raise httpx.ReadTimeout("secret in upstream diagnostic")

    with pytest.raises(ProviderUnavailable, match="^PROVIDER_TRANSPORT_FAILED$"):
        await adapter(handler=timeout).list_models()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"model": "other"},
        {"choices": []},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "4", "tool_calls": [{}]},
                    "finish_reason": "stop",
                }
            ]
        },
    ],
)
async def test_bad_completion_envelopes_are_rejected(changes):
    with pytest.raises(MalformedResponse):
        await adapter(
            handler=lambda req: httpx.Response(
                200, json=catalog() if req.url.path.endswith("/models") else completion(**changes)
            )
        ).complete(request())


def local_tags(**changes):
    return {
        "models": [
            {"name": "model", "size": 200, "digest": "abc", "details": {"format": "gguf"}} | changes
        ]
    }


def local_info(**changes):
    return {
        "details": {"format": "gguf"},
        "capabilities": ["completion"],
        "model_info": {"general.architecture": "llama", "llama.context_length": 8192},
    } | changes


@pytest.mark.asyncio
async def test_local_chat_only_uses_existing_weights_and_unloads():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        assert req.url.host == "127.0.0.1" and "authorization" not in req.headers
        if req.url.path == "/api/tags":
            return httpx.Response(200, json=local_tags())
        if req.url.path == "/api/show":
            return httpx.Response(200, json=local_info())
        body = json.loads(req.content)
        assert body["options"] == {"num_predict": 32, "num_ctx": 4096}
        assert body["keep_alive"] == 0 and body["stream"] is False
        return httpx.Response(
            200,
            json={
                "model": "model",
                "message": {"role": "assistant", "content": "4"},
                "done": True,
                "done_reason": "stop",
            },
        )

    result = await adapter("ollama_local", handler).complete(request())
    assert result.text == "4" and calls == ["/api/tags", "/api/show", "/api/chat"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"remote_host": "https://ollama.com"},
        {"remote_model": "model"},
        {"details": None},
        {"size": 0},
        {"digest": "changed"},
    ],
)
async def test_remote_or_changed_local_weights_prevent_chat(changes):
    spec = spec_for(
        "ollama_local",
        models=[{"model_id": "model", "context_window": 4096, "model_revision": "abc"}],
    )
    with pytest.raises(AuthenticationFailed):
        await adapter(
            "ollama_local", lambda req: httpx.Response(200, json=local_tags(**changes)), spec=spec
        ).complete(request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"remote_host": "cloud"},
        {"details": None},
        {"capabilities": None},
        {"capabilities": ["embedding"]},
        {"model_info": None},
        {"model_info": {"general.architecture": "llama", "llama.context_length": 10}},
    ],
)
async def test_local_show_must_confirm_completion_and_context(changes):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(
            200, json=local_tags() if req.url.path == "/api/tags" else local_info(**changes)
        )

    with pytest.raises(AuthenticationFailed):
        await adapter("ollama_local", handler).complete(request())
    assert calls == ["/api/tags", "/api/show"]


@pytest.mark.asyncio
async def test_cancellation_closes_response_stream():
    started, closed = asyncio.Event(), asyncio.Event()

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            closed.set()

    live = adapter(handler=lambda req: httpx.Response(200, stream=HangingStream()))
    task = asyncio.create_task(live.list_models())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def test_retry_parsing_is_bounded():
    assert duration("2m59.56s") == pytest.approx(179.56)
    assert duration("20ms") == 0.02
    assert duration("unknown") is None
    for value in [None, "nan", "inf", "-1", "999999999"]:
        assert retry_seconds(value, NOW.timestamp()) is None
    assert retry_seconds("Wed, 09 Sep 2026 00:02:00 GMT", NOW.timestamp()) == 120


async def test_observations_only_reduce_budget_and_persist_retry(make_router):
    spec = provider(request_limit=10)
    router = make_router([(spec, None)])
    router.quota.clock = lambda: 1000
    assert await router.quota.reserve(spec)
    await router.quota.observe(
        spec,
        QuotaSnapshot(provider_id="a", quota_limit=100, quota_remaining_estimate=2, reset_at=1060),
    )
    assert await router.quota.remaining(spec) == 2
    await router.quota.observe(
        spec, QuotaSnapshot(provider_id="a", quota_limit=100, quota_remaining_estimate=99)
    )
    assert await router.quota.remaining(spec) == 2
    await router.quota.throttle("a", retry_after=120)
    assert (await router.quota.state("a")).blocked_until == 1120
    with pytest.raises(ValueError):
        await router.quota.observe(spec, QuotaSnapshot(provider_id="wrong"))


def test_registry_requires_scoped_credentials(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(CredentialConfigurationError):
        register_live(Registry(), [spec_for()], settings())


async def test_billing_violation_stops_router_without_false_zero_spend_claim(make_router):
    spec = spec_for("openrouter_free")

    def handler(req):
        body = (
            catalog("owner/model:free")
            if req.url.path.endswith("/models")
            else {"data": {"is_free_tier": True}}
            if req.url.path.endswith("/key")
            else completion("owner/model:free", usage={"cost": "0.01"})
        )
        return httpx.Response(200, json=body)

    live = adapter("openrouter_free", handler, spec=spec)
    live = CredentialedAdapter(spec.provider_id, live, SecretStr("test-provider-secret"))
    router = make_router([(spec, live)])
    app = create_app(router, {"client": "client-key"}, "admin-key")
    with TestClient(app) as client:
        assert client.get("/healthz").json()["live_inference_enabled"] is True
        response = client.post(
            "/v1/solve",
            headers={"x-api-key": "client-key"},
            json={"client_id": "client", "task": "2 + 2"},
        )
        assert response.status_code == 503, response.text
        assert response.json()["paid_inference_executed"] is None
        assert router.stopped and (await router.quota.state(spec.provider_id)).security_blocked
        assert client.get("/healthz").json()["live_inference_enabled"] is False
    with router.sessions() as session:
        row = session.scalar(select(TaskRequest))
        assert row.status == "FAILED" and row.result_json is None
        assert (
            session.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "BILLING_POLICY_VIOLATION")
            )
            is not None
        )


@pytest.mark.asyncio
async def test_schema_counts_toward_context_before_chat():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=catalog())

    with pytest.raises(MalformedResponse, match="CONTEXT_BUDGET_EXCEEDED"):
        await adapter(handler=handler).complete(
            request(expected_json_schema={"description": "x" * 4096})
        )
    assert calls == ["/openai/v1/models"]


@pytest.mark.asyncio
async def test_expired_review_rechecked_before_each_transport():
    calls = []
    live = adapter(handler=lambda req: calls.append(req))
    live.clock = lambda: NOW.timestamp() + 31 * 86400
    with pytest.raises(AuthenticationFailed):
        await live.list_models()
    assert not calls


@pytest.mark.asyncio
async def test_smoke_command_uses_router_validation_and_private_config(tmp_path, monkeypatch):
    import yaml

    from fair.providers import smoke as smoke_module

    spec = spec_for("ollama_local")
    (tmp_path / "providers.yaml").write_text(
        yaml.safe_dump({"providers": [spec.model_dump(mode="json")]}), encoding="utf-8"
    )
    (tmp_path / "live_adapters.yaml").write_text(
        yaml.safe_dump(settings().model_dump()), encoding="utf-8"
    )
    calls = []

    def handler(req):
        calls.append(req.url.path)
        body = (
            local_tags()
            if req.url.path == "/api/tags"
            else local_info()
            if req.url.path == "/api/show"
            else {
                "model": "model",
                "message": {"role": "assistant", "content": "4"},
                "done": True,
                "done_reason": "stop",
            }
        )
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        smoke_module,
        "register_live",
        lambda registry, specs, config: register_live(
            registry, specs, config, httpx.MockTransport(handler)
        ),
    )
    result = await smoke_module.smoke(tmp_path, "ollama_local", "model")
    assert result["status"] == "ACCEPTED"
    assert result["verification_state"] == "DETERMINISTIC_ARITHMETIC"
    assert result["attempts"] == 1 and result["paid_inference_executed"] is False
    assert calls == ["/api/tags", "/api/show", "/api/chat"]
