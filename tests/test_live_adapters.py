"""Transport-level tests for the live adapters — no network, no credentials leave the process."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from fair.embedded.module import _CLOUD_PROVIDERS, FAIR, _loopback
from fair.providers.base import (
    AccessDenied,
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
    RequestNotSupported,
)
from fair.providers.live import (
    CloudflareWorkersAiAdapter,
    GeminiAdapter,
    GroqAdapter,
    KiloFreeAdapter,
    LiveSettings,
    MistralAdapter,
    OpenRouterFreeAdapter,
    ZaiFreeAdapter,
)
from fair.schemas.domain import NormalizedModelRequest, ProviderSpec
from fair.schemas.qualification import ModelQualification, ProviderQualification

ACCOUNT = "0" * 32


def _spec(provider_id, access_class, model_id, context=131072):
    reviewed = datetime.now(UTC) - timedelta(seconds=10)
    free_status = (
        "verified_free_plan" if access_class == "FREE_RECURRING" else "verified_zero_price_model"
    )
    return ProviderSpec(
        provider_id=provider_id,
        access_class=access_class,
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        terms_last_verified=reviewed,
        models=[{"model_id": model_id, "context_window": context, "capabilities": {"reasoning"}}],
        qualification=ProviderQualification(
            provider_id=provider_id,
            free_status=free_status,
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
            ],
        ),
    )


def _settings():
    return LiveSettings(enabled=True, confirmed_providers=set(_CLOUD_PROVIDERS))


def _request(model_id):
    return NormalizedModelRequest(
        task="ping",
        model_id=model_id,
        request_id="r",
        client_id="c",
        task_class="general",
        max_output_tokens=64,
    )


def _completion(model_id, content="pong", usage=None):
    body = {
        "model": model_id,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _asked_for_a_stream(request):
    try:
        return json.loads(request.content).get("stream") is True
    except (ValueError, UnicodeDecodeError, AttributeError):
        return False


def _as_sse(body):
    """Re-serve a buffered completion as the event stream a provider would send."""
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    model = body.get("model")
    events = [
        {"model": model, "choices": [{"delta": {"role": message.get("role", "assistant")}}]},
        {"model": model, "choices": [{"delta": {"content": message.get("content", "")}}]},
        {"model": model, "choices": [{"delta": {}, "finish_reason": choice.get("finish_reason")}]},
    ]
    if body.get("usage") is not None:
        events.append({"model": model, "choices": [], "usage": body["usage"]})
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


def _transport(routes):
    """routes: {(method, path_suffix): (status, json_body) | callable(request)}.

    A route asked for with "stream": true is served as SSE, the way a provider
    answers a streaming request, so one route serves both transports.
    """
    seen = []

    def handler(request):
        seen.append(request)
        for (method, suffix), response in routes.items():
            if (
                request.method == method
                and request.url.path.endswith(suffix)
                or (request.method == method and suffix in str(request.url))
            ):
                if callable(response):
                    return response(request)
                status, body = response
                if status == 200 and _asked_for_a_stream(request):
                    return _as_sse(body)
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": {"code": 404}})

    return httpx.MockTransport(handler), seen


class TestKilo:
    MODEL = "qwen/qwen3.8-27b:free"

    def _adapter(self, routes):
        transport, seen = _transport(routes)
        adapter = KiloFreeAdapter(
            _spec("kilo_free", "FREE_DYNAMIC", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        return adapter, seen

    def test_http_client_uses_bounded_phase_timeouts(self):
        adapter, _ = self._adapter({})
        timeout = adapter._client.timeout
        assert timeout.connect == 5
        assert timeout.read == 25
        assert timeout.write == 5
        assert timeout.pool == 5

    async def test_zero_priced_free_model_completes(self):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0", "completion": "0", "discount": 0},
                }
            ]
        }
        adapter, seen = self._adapter(
            {
                ("GET", "/models"): (200, catalog),
                ("POST", "/chat/completions"): (
                    200,
                    _completion(self.MODEL, usage={"cost_microdollars": 0}),
                ),
            }
        )
        response = await adapter.complete(_request(self.MODEL))
        assert response.text == "pong"
        assert adapter.safe_diagnostics() == {"cost_microdollars": "ZERO"}
        assert str(seen[-1].url) == "https://api.kilo.ai/api/gateway/chat/completions"
        assert "provider" not in json.loads(seen[-1].content)

    @pytest.mark.parametrize(
        "model_id",
        [
            "nvidia/nemotron-3-super-120b-a12b:free",
            "poolside/laguna-s-2.1:free",
            "stepfun/step-3.7-flash:free",
            "dots-studio/dots-3-note-preview:free",
            "inclusionai/ling-3.0-flash-sante:free",
        ],
    )
    def test_trial_preview_or_promotional_models_fail_admission(self, model_id):
        with pytest.raises(
            AuthenticationFailed,
            match="KILO_TRIAL_OR_PROMOTIONAL_MODEL_NOT_ALLOWED",
        ):
            KiloFreeAdapter(
                _spec("kilo_free", "FREE_DYNAMIC", model_id),
                _settings(),
                credential=SecretStr("k"),
                transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            )

    async def test_http_403_is_access_denied_not_bad_credentials(self):
        adapter, _ = self._adapter({("GET", "/models"): (403, {"error": {"code": 403}})})
        with pytest.raises(AccessDenied):
            await adapter.list_models()

    async def test_priced_model_is_dropped_from_catalog(self):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0.1", "completion": "0"},
                }
            ]
        }
        adapter, _ = self._adapter({("GET", "/models"): (200, catalog)})
        assert await adapter.list_models() == []

    async def test_nonzero_cost_is_a_billing_violation(self):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        }
        adapter, _ = self._adapter(
            {
                ("GET", "/models"): (200, catalog),
                ("POST", "/chat/completions"): (
                    200,
                    _completion(self.MODEL, usage={"cost_microdollars": 1}),
                ),
            }
        )
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))
        assert adapter.safe_diagnostics() == {"cost_microdollars": "NONZERO_OR_INVALID"}

    async def test_missing_cost_field_uses_fresh_zero_price_catalog_fallback(self):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        }
        adapter, _ = self._adapter(
            {
                ("GET", "/models"): (200, catalog),
                ("POST", "/chat/completions"): (
                    200,
                    _completion(self.MODEL, usage={"prompt_tokens": 1, "completion_tokens": 1}),
                ),
            }
        )
        response = await adapter.complete(_request(self.MODEL))
        assert response.text == "pong"
        assert adapter.safe_diagnostics() == {"cost_microdollars": "CATALOG_ZERO_PRICE_FALLBACK"}

    async def test_missing_cost_field_without_exact_catalog_echo_fails_closed(self):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        }
        adapter, _ = self._adapter(
            {
                ("GET", "/models"): (200, catalog),
                ("POST", "/chat/completions"): (
                    200,
                    _completion(
                        "other/model:free", usage={"prompt_tokens": 1, "completion_tokens": 1}
                    ),
                ),
            }
        )
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))
        assert adapter.safe_diagnostics() == {"cost_microdollars": "COST_FIELD_MISSING"}

    async def test_missing_usage_fails_closed_with_safe_diagnostic(self):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        }
        adapter, _ = self._adapter(
            {
                ("GET", "/models"): (200, catalog),
                ("POST", "/chat/completions"): (200, _completion(self.MODEL)),
            }
        )
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))
        assert adapter.safe_diagnostics() == {"cost_microdollars": "USAGE_MISSING"}

    def test_non_free_model_id_is_refused(self):
        with pytest.raises(AuthenticationFailed):
            KiloFreeAdapter(
                _spec("kilo_free", "FREE_DYNAMIC", "nex-agi/nex-n2.5-pro"),
                _settings(),
                credential=SecretStr("k"),
                transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            )


class TestOpenRouter:
    MODEL = "google/gemma-4-26b-a4b-it:free"

    async def test_privacy_preferences_and_free_tier_check(self):
        transport, seen = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {
                        "data": [
                            {
                                "id": self.MODEL,
                                "context_length": 262144,
                                "pricing": {"prompt": "0", "completion": "0"},
                            }
                        ]
                    },
                ),
                ("GET", "/key"): (200, {"data": {"is_free_tier": True}}),
                ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"cost": 0})),
            }
        )
        adapter = OpenRouterFreeAdapter(
            _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        await adapter.complete(_request(self.MODEL))
        payload = json.loads(seen[-1].content)
        assert payload["provider"]["data_collection"] == "deny"
        assert payload["provider"]["max_price"] == {
            "prompt": 0,
            "completion": 0,
            "request": 0,
            "image": 0,
        }

    async def test_completion_requests_usage_accounting(self):
        """The zero-cost check needs usage in the response, which must be asked for."""
        transport, seen = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {
                        "data": [
                            {
                                "id": self.MODEL,
                                "context_length": 262144,
                                "pricing": {"prompt": "0", "completion": "0"},
                            }
                        ]
                    },
                ),
                ("GET", "/key"): (200, {"data": {"is_free_tier": True}}),
                ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"cost": 0})),
            }
        )
        adapter = OpenRouterFreeAdapter(
            _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        await adapter.complete(_request(self.MODEL))
        assert json.loads(seen[-1].content)["usage"] == {"include": True}

    async def test_absent_usage_block_fails_closed(self):
        """No cost evidence is not zero cost."""
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {
                        "data": [
                            {
                                "id": self.MODEL,
                                "context_length": 262144,
                                "pricing": {"prompt": "0", "completion": "0"},
                            }
                        ]
                    },
                ),
                ("GET", "/key"): (200, {"data": {"is_free_tier": True}}),
                ("POST", "/chat/completions"): (200, _completion(self.MODEL)),
            }
        )
        adapter = OpenRouterFreeAdapter(
            _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))

    def test_models_without_response_format_do_not_claim_structured_output(self):
        """require_parameters means an unsupported response_format fails the call."""
        capabilities = {
            model.model_id: model.capabilities
            for model in _CLOUD_PROVIDERS["openrouter_free"]["models"]
        }
        assert "structured_output" not in capabilities["nvidia/nemotron-3-ultra-550b-a55b:free"]
        assert "structured_output" not in capabilities["cohere/north-mini-code:free"]
        # Every configured model still has to be routable for ordinary text work.
        assert all("reasoning" in caps for caps in capabilities.values())


class TestMistral:
    MODEL = "ministral-8b-latest"

    async def test_catalog_uses_max_context_length(self):
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "max_context_length": 4096}]},
                ),
            }
        )
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        assert await adapter.list_models() == []

    async def test_monthly_limit_reached_uses_admin_billing_period_end(self):
        now = datetime.now(UTC)
        period_end = now + timedelta(days=7)
        adapter_routes = {
            ("GET", "/models"): (
                200,
                {"data": [{"id": self.MODEL, "max_context_length": 262144}]},
            ),
            ("POST", "/chat/completions"): (
                429,
                {"object": "error", "type": "rate_limit_error", "message": "limit"},
            ),
            ("GET", "/admin/spend-limit"): (
                200,
                {
                    "limits": {
                        "completion": {"monthly_limit_reached": True},
                        "currency": "USD",
                    }
                },
            ),
            ("GET", "/admin/usage"): (
                200,
                {"end_date": period_end.isoformat().replace("+00:00", "Z")},
            ),
        }
        transport, seen = _transport(adapter_routes)
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            admin_credential=SecretStr("admin-k"),
            transport=transport,
            clock=lambda: now.timestamp(),
        )
        with pytest.raises(QuotaExceeded, match="MONTHLY_USAGE_LIMIT_REACHED") as error:
            await adapter.complete(_request(self.MODEL))
        assert error.value.reset_at == pytest.approx(period_end.timestamp())
        admin_request = next(
            request for request in seen if "/admin/spend-limit" in str(request.url)
        )
        assert admin_request.headers["x-api-key"] == "admin-k"
        usage_request = next(request for request in seen if "/admin/usage" in str(request.url))
        assert usage_request.headers["x-api-key"] == "admin-k"
        quota = await adapter.quota()
        assert quota.quota_remaining_estimate == 0
        assert quota.reset_at == error.value.reset_at

    async def test_monthly_limit_without_period_end_uses_six_hour_recheck(self):
        now = datetime.now(UTC).timestamp()
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "max_context_length": 262144}]},
                ),
                ("POST", "/chat/completions"): (
                    429,
                    {"object": "error", "type": "rate_limit_error", "message": "limit"},
                ),
                ("GET", "/admin/spend-limit"): (
                    200,
                    {"limits": {"completion": {"monthly_limit_reached": True}}},
                ),
                ("GET", "/admin/usage"): (500, {"object": "error"}),
            }
        )
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            admin_credential=SecretStr("admin-k"),
            transport=transport,
            clock=lambda: now,
        )
        with pytest.raises(QuotaExceeded) as error:
            await adapter.complete(_request(self.MODEL))
        assert error.value.reset_at == pytest.approx(now + 6 * 60 * 60)

    async def test_monthly_admin_false_preserves_transient_rate_limit(self):
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "max_context_length": 262144}]},
                ),
                ("POST", "/chat/completions"): (
                    429,
                    {"object": "error", "type": "rate_limit_error", "message": "limit"},
                ),
                ("GET", "/admin/spend-limit"): (
                    200,
                    {"limits": {"completion": {"monthly_limit_reached": False}}},
                ),
            }
        )
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            admin_credential=SecretStr("admin-k"),
            transport=transport,
        )
        with pytest.raises(RateLimited):
            await adapter.complete(_request(self.MODEL))

    async def test_missing_admin_key_never_guesses_monthly_exhaustion(self):
        transport, seen = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "max_context_length": 262144}]},
                ),
                ("POST", "/chat/completions"): (
                    429,
                    {"object": "error", "type": "rate_limit_error", "message": "limit"},
                ),
            }
        )
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        with pytest.raises(RateLimited):
            await adapter.complete(_request(self.MODEL))
        assert not any("/admin/spend-limit" in str(request.url) for request in seen)

    async def test_per_minute_quota_headers_exhaust(self):
        def chat(request):
            return httpx.Response(
                429,
                json={"message": "Rate limit exceeded"},
                headers={
                    "x-ratelimit-limit-req-minute": "10",
                    "x-ratelimit-remaining-req-minute": "0",
                },
            )

        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "max_context_length": 262144}]},
                ),
                ("POST", "/chat/completions"): chat,
            }
        )
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        with pytest.raises(QuotaExceeded) as error:
            await adapter.complete(_request(self.MODEL))
        assert error.value.reset_at is not None


class TestZai:
    def test_zai_is_never_admitted_as_free_provider(self):
        with pytest.raises(AuthenticationFailed, match="NO_RECURRING_FREE_TIER"):
            ZaiFreeAdapter(
                _spec("zai_free", "FREE_DYNAMIC", "glm-4.5-flash"),
                _settings(),
                credential=SecretStr("k"),
                transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            )


class TestGroq:
    MODEL = "openai/gpt-oss-20b"

    async def test_uses_max_completion_tokens_and_context_window(self):
        transport, seen = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                ),
                ("POST", "/chat/completions"): (200, _completion(self.MODEL)),
            }
        )
        adapter = GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        await adapter.complete(_request(self.MODEL))
        payload = json.loads(seen[-1].content)
        assert payload["max_completion_tokens"] == 64
        assert "max_tokens" not in payload


class TestCloudflare:
    MODEL = "@cf/openai/gpt-oss-20b"

    def _catalog(self, context="128000"):
        return {
            "success": True,
            "result": [
                {
                    "name": self.MODEL,
                    "properties": [{"property_id": "context_window", "value": context}],
                }
            ],
        }

    def _adapter(self, routes, clock=None):
        transport, seen = _transport(routes)
        kwargs = {"credential": SecretStr("k"), "transport": transport}
        if clock is not None:
            kwargs["clock"] = clock
        adapter = CloudflareWorkersAiAdapter(
            _spec("cloudflare_workers_ai", "FREE_RECURRING", self.MODEL, context=128000),
            _settings(),
            ACCOUNT,
            **kwargs,
        )
        return adapter, seen

    async def test_completion_does_not_require_undocumented_neuron_usage(self):
        adapter, seen = self._adapter(
            {
                ("GET", "/ai/models/search"): (200, self._catalog()),
                ("POST", "/chat/completions"): (
                    200,
                    _completion(
                        self.MODEL,
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    ),
                ),
            }
        )
        response = await adapter.complete(_request(self.MODEL))
        assert response.quota.quota_limit is None
        assert response.quota.quota_remaining_estimate is None
        assert str(seen[0].url).startswith(
            f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/models/search"
        )
        assert (
            str(seen[-1].url)
            == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1/chat/completions"
        )

    async def test_daily_free_allocation_error_exhausts_until_utc_reset(self):
        now = datetime.now(UTC).timestamp()
        expected_reset = (int(now // 86400) + 1) * 86400
        adapter, _ = self._adapter(
            {
                ("GET", "/ai/models/search"): (200, self._catalog()),
                ("POST", "/chat/completions"): (
                    429,
                    {
                        "success": False,
                        "errors": [{"code": 3036, "message": "daily allocation spent"}],
                    },
                ),
            },
            clock=lambda: now,
        )
        with pytest.raises(QuotaExceeded) as raised:
            await adapter.complete(_request(self.MODEL))
        assert raised.value.reset_at == expected_reset
        quota = await adapter.quota()
        assert quota.quota_limit == 10_000
        assert quota.quota_remaining_estimate == 0
        assert quota.reset_at == expected_reset

    async def test_out_of_capacity_429_is_transient_not_daily_exhaustion(self):
        adapter, _ = self._adapter(
            {
                ("GET", "/ai/models/search"): (200, self._catalog()),
                ("POST", "/chat/completions"): (
                    429,
                    {
                        "success": False,
                        "errors": [{"code": 3040, "message": "capacity exceeded"}],
                    },
                ),
            }
        )
        with pytest.raises(RateLimited):
            await adapter.complete(_request(self.MODEL))
        quota = await adapter.quota()
        assert quota.quota_remaining_estimate is None

    async def test_small_context_model_is_dropped(self):
        adapter, _ = self._adapter(
            {("GET", "/ai/models/search"): (200, self._catalog(context="4096"))}
        )
        assert await adapter.list_models() == []

    def test_account_id_must_be_hex(self):
        with pytest.raises(AuthenticationFailed):
            CloudflareWorkersAiAdapter(
                _spec("cloudflare_workers_ai", "FREE_RECURRING", self.MODEL),
                _settings(),
                "../evil",
                credential=SecretStr("k"),
            )


class TestModuleWiring:
    def test_env_file_wires_every_cloud_provider(self, tmp_path, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        for name in ("OLLAMA_URL", "OLLAMA_HOST", "OLLAMA_ENABLED", "CLOUDFLARE_ACCOUNT_ID"):
            monkeypatch.delenv(name, raising=False)
        lines = ["# comment", f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}"]
        lines += [f"{entry['env']}=secret-{n}" for n, entry in enumerate(_CLOUD_PROVIDERS.values())]
        env_file = tmp_path / ".env"
        env_file.write_text("\n".join(lines), encoding="utf-8")
        fair = FAIR(
            env_file=str(env_file),
            confirmed_free_providers=set(_CLOUD_PROVIDERS),
        )
        assert {p["provider_id"] for p in fair.providers()} == set(_CLOUD_PROVIDERS)
        assert fair.skipped == {}

    def test_kilo_reviewed_models_exclude_nvidia_trial_routes(self):
        models = _CLOUD_PROVIDERS["kilo_free"]["models"]
        assert models
        assert all(model.model_id.endswith(":free") for model in models)
        assert all(not model.model_id.startswith("nvidia/") for model in models)

    def test_recurring_provider_requires_explicit_free_account_confirmation(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)

        fair = FAIR(kilo_api_key="k", groq_api_key="g")

        assert {p["provider_id"] for p in fair.providers()} == {"kilo_free"}
        assert "groq" in fair.skipped
        assert "explicit free-tier account confirmation required" in fair.skipped["groq"]

    def test_explicit_free_account_confirmation_allows_recurring_provider(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)

        fair = FAIR(
            groq_api_key="g",
            confirmed_free_providers={"groq"},
        )

        assert [p["provider_id"] for p in fair.providers()] == ["groq"]
        assert fair.skipped == {}

    def test_unknown_free_provider_confirmation_is_rejected(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)

        with pytest.raises(ValueError, match="Unknown confirmed free provider"):
            FAIR(
                kilo_api_key="k",
                confirmed_free_providers={"not-a-provider"},
            )

    def test_nvidia_hosted_credit_api_is_never_eligible(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

        fair = FAIR(
            nvidia_api_key="n",
            kilo_api_key="k",
        )

        assert {p["provider_id"] for p in fair.providers()} == {"kilo_free"}
        assert fair.skipped["nvidia_nim"].startswith("hosted preview API uses starter credits")

    def test_ollama_cloud_credit_pricing_is_never_eligible(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)

        fair = FAIR(
            ollama_cloud_api_key="o",
            kilo_api_key="k",
        )

        assert {p["provider_id"] for p in fair.providers()} == {"kilo_free"}
        assert fair.skipped["ollama_cloud"].startswith("credit-priced cloud service")

    def test_zai_trial_or_paid_api_is_never_eligible(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)

        fair = FAIR(
            zai_api_key="z",
            kilo_api_key="k",
            confirmed_free_providers={"zai_free"},
        )

        assert {p["provider_id"] for p in fair.providers()} == {"kilo_free"}
        assert fair.skipped["zai_free"].startswith("Z.ai")

    def test_cloudflare_without_account_is_skipped(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        fair = FAIR(
            cloudflare_api_token="t",
            groq_api_key="g",
            confirmed_free_providers={"cloudflare_workers_ai", "groq"},
        )
        assert [p["provider_id"] for p in fair.providers()] == ["groq"]
        assert "cloudflare_workers_ai" in fair.skipped

    def test_cooldown_matches_longest_provider_window(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        assert (
            FAIR(
                groq_api_key="g",
                confirmed_free_providers={"groq"},
            )._router.settings.cooldown_seconds
            == 360
        )
        assert (
            FAIR(
                groq_api_key="g",
                confirmed_free_providers={"groq"},
                cooldown_seconds=30,
            )._router.settings.cooldown_seconds
            == 30
        )

    def test_localhost_normalizes_to_loopback(self):
        assert _loopback("http://localhost:11434/") == "http://127.0.0.1:11434"
        assert _loopback("http://127.0.0.1:11434") == "http://127.0.0.1:11434"

    def test_explicit_ollama_models_skip_discovery(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        fair = FAIR(ollama_url="http://localhost:1", ollama_models=["llama3.2:3b"])
        assert fair.providers()[0]["models"] == ["llama3.2:3b"]


# ── Structured-output transport ──────────────────────────────────────────

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "items"],
    "properties": {
        "kind": {"type": "string", "const": "payoff_reveal"},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1},
        },
    },
}


def _diagnostic_settings():
    return LiveSettings(
        enabled=True,
        confirmed_providers=set(_CLOUD_PROVIDERS),
        provider_error_diagnostics=True,
    )


class TestSchemaTransport:
    """The caller's schema is reduced to what the provider parses; the rest goes in the prompt."""

    MODEL = "openai/gpt-oss-20b"

    def _adapter(self, settings=None, status=200, body=None):
        transport, seen = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                ),
                ("POST", "/chat/completions"): (
                    status,
                    _completion(self.MODEL) if body is None else body,
                ),
            }
        )
        adapter = GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            settings or _settings(),
            credential=SecretStr("sk-secret-value"),
            transport=transport,
        )
        return adapter, seen

    def _schema_request(self):
        return _request(self.MODEL).model_copy(update={"expected_json_schema": _SCHEMA})

    async def _sent(self):
        adapter, seen = self._adapter()
        await adapter.complete(self._schema_request())
        return json.loads(seen[-1].content)

    async def test_unsupported_keywords_never_reach_the_provider(self):
        body = json.dumps(await self._sent())
        for keyword in ("minLength", "minItems", "maxItems"):
            assert keyword not in body, keyword

    async def test_const_is_sent_as_a_single_value_enum(self):
        schema = (await self._sent())["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["kind"] == {"type": "string", "enum": ["payoff_reveal"]}

    async def test_strict_mode_and_structure_are_preserved(self):
        block = (await self._sent())["response_format"]["json_schema"]
        assert block["strict"] is True
        assert block["schema"]["additionalProperties"] is False
        assert block["schema"]["required"] == ["kind", "items"]

    async def test_dropped_constraints_are_restated_in_the_prompt(self):
        content = (await self._sent())["messages"][0]["content"]
        assert content.startswith("ping")
        assert "1 to 5 items" in content
        assert "must not be empty" in content

    async def test_a_request_without_a_schema_sends_the_task_unchanged(self):
        adapter, seen = self._adapter()
        await adapter.complete(_request(self.MODEL))
        payload = json.loads(seen[-1].content)
        assert payload["messages"][0]["content"] == "ping"
        assert "response_format" not in payload

    async def test_gemini_receives_its_own_dialect(self):
        model = "gemini-3.6-flash"
        transport, seen = _transport(
            {
                ("GET", "/models/" + model): (
                    200,
                    {
                        "name": "models/" + model,
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ),
                # Gemini streams; one event carrying the envelope it would have buffered.
                ("POST", "GenerateContent"): lambda request: httpx.Response(
                    200,
                    content=(
                        "data: "
                        + json.dumps(
                            {
                                "modelVersion": model,
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [{"text": "{}"}],
                                        },
                                        "finishReason": "STOP",
                                    }
                                ],
                            }
                        )
                        + "\n\ndata: [DONE]\n\n"
                    ).encode(),
                    headers={"content-type": "text/event-stream"},
                ),
            }
        )
        adapter = GeminiAdapter(
            _spec("google_gemini_api", "FREE_RECURRING", model, context=1048576),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        await adapter.complete(_request(model).model_copy(update={"expected_json_schema": _SCHEMA}))
        payload = json.loads(seen[-1].content)
        schema = payload["generationConfig"]["responseJsonSchema"]
        # Gemini rejects additionalProperties; strict mode requires it. One schema
        # cannot satisfy both, which is the whole reason a dialect exists.
        assert "additionalProperties" not in json.dumps(schema)
        assert schema["properties"]["kind"]["enum"] == ["payoff_reveal"]
        assert "1 to 5 items" in payload["contents"][0]["parts"][0]["text"]


class TestProviderErrorDiagnostics:
    """HTTP_400 alone leaves an operator nothing to act on; the body is opt-in and scrubbed."""

    MODEL = "openai/gpt-oss-20b"
    BODY = {"error": {"message": "response_format.json_schema.schema: 'minLength' is unsupported"}}

    def _adapter(self, settings, status=400, body=None):
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                ),
                ("POST", "/chat/completions"): (status, self.BODY if body is None else body),
            }
        )
        return GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            settings,
            credential=SecretStr("sk-secret-value"),
            transport=transport,
        )

    async def _fail(self, settings, **kwargs):
        adapter = self._adapter(settings, **kwargs)
        with pytest.raises(ProviderUnavailable, match="HTTP_400"):
            await adapter.complete(_request(self.MODEL))
        return adapter

    async def test_nothing_is_retained_by_default(self):
        adapter = await self._fail(_settings())
        assert adapter.safe_diagnostics() == {}

    async def test_the_provider_message_is_retained_when_enabled(self):
        adapter = await self._fail(_diagnostic_settings())
        record = adapter.safe_diagnostics()["last_provider_error"]
        assert record["status"] == "HTTP_400"
        assert "'minLength' is unsupported" in record["provider_message"]

    async def test_the_raised_code_is_the_same_either_way(self):
        # Both calls above assert ProviderUnavailable("HTTP_400"); diagnostics add a
        # record, they never change what the router or the attempt log sees.
        await self._fail(_settings())
        await self._fail(_diagnostic_settings())

    async def test_a_body_that_is_not_json_still_reports_the_status(self):
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                ),
                ("POST", "/chat/completions"): lambda request: httpx.Response(
                    400, content=b"<html>gateway error</html>"
                ),
            }
        )
        adapter = GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            _diagnostic_settings(),
            credential=SecretStr("sk-secret-value"),
            transport=transport,
        )
        with pytest.raises(ProviderUnavailable, match="HTTP_400"):
            await adapter.complete(_request(self.MODEL))
        assert (
            "gateway error" in adapter.safe_diagnostics()["last_provider_error"]["provider_message"]
        )

    async def test_a_credential_echoed_by_the_provider_is_redacted(self):
        adapter = await self._fail(
            _diagnostic_settings(),
            body={"error": {"message": "invalid key sk-secret-value supplied"}},
        )
        message = adapter.safe_diagnostics()["last_provider_error"]["provider_message"]
        assert "sk-secret-value" not in message
        assert "[redacted]" in message

    async def test_the_record_is_bounded(self):
        adapter = await self._fail(_diagnostic_settings(), body={"error": {"message": "x" * 5000}})
        message = adapter.safe_diagnostics()["last_provider_error"]["provider_message"]
        assert len(message) <= 512

    async def test_cloudflare_error_codes_still_win_over_the_record(self):
        """A structured quota error must keep its own exception, not become HTTP_429."""
        transport, _ = _transport(
            {
                ("GET", "/ai/models/search"): (
                    200,
                    {
                        "result": [
                            {
                                "name": "@cf/openai/gpt-oss-20b",
                                "properties": [
                                    {"property_id": "context_window", "value": "131072"}
                                ],
                            }
                        ]
                    },
                ),
                ("POST", "/chat/completions"): (429, {"error": {"code": 3036}}),
            }
        )
        adapter = CloudflareWorkersAiAdapter(
            _spec("cloudflare_workers_ai", "FREE_RECURRING", "@cf/openai/gpt-oss-20b"),
            _diagnostic_settings(),
            account_id=ACCOUNT,
            credential=SecretStr("k"),
            transport=transport,
        )
        with pytest.raises(QuotaExceeded, match="DAILY_NEURON_ALLOCATION_SPENT"):
            await adapter.complete(_request("@cf/openai/gpt-oss-20b"))

    def test_kilo_keeps_its_billing_state_alongside_the_record(self):
        transport, _ = _transport({})
        adapter = KiloFreeAdapter(
            _spec("kilo_free", "FREE_DYNAMIC", "qwen/qwen3.8-27b:free"),
            _diagnostic_settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        assert adapter.safe_diagnostics() == {"cost_microdollars": "NOT_OBSERVED"}
        adapter._record_error_diagnostic(400, "/chat/completions", b'{"error":{"message":"bad"}}')
        assert adapter.safe_diagnostics() == {
            "cost_microdollars": "NOT_OBSERVED",
            "last_provider_error": {
                "status": "HTTP_400",
                "path": "/chat/completions",
                "provider_message": "bad",
            },
        }


# ── Catalog: negative caching, drop reasons, published limits ────────────


class _Clock:
    """A clock the test moves by hand; adapters admit themselves against real time."""

    def __init__(self):
        self.now = datetime.now(UTC).timestamp()

    def __call__(self):
        return self.now


class TestCatalogFailureIsCached:
    """One unreachable catalog cost three consecutive 25-second read timeouts."""

    MODEL = "ministral-8b-latest"

    def _adapter(self, clock, response=None):
        def handler(request):
            calls.append(request)
            if response is not None:
                return response
            raise httpx.ConnectTimeout("timeout")

        calls: list = []
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=httpx.MockTransport(handler),
            clock=clock,
        )
        return adapter, calls

    async def test_a_failed_catalog_is_fetched_once_not_once_per_model(self):
        clock = _Clock()
        adapter, calls = self._adapter(clock)
        for _ in range(3):
            with pytest.raises(ProviderUnavailable, match="PROVIDER_TRANSPORT_FAILED"):
                await adapter.list_models()
        assert len(calls) == 1

    async def test_the_cached_failure_reports_what_the_first_one_did(self):
        clock = _Clock()
        adapter, _ = self._adapter(clock)
        with pytest.raises(ProviderUnavailable) as first:
            await adapter.list_models()
        with pytest.raises(ProviderUnavailable) as cached:
            await adapter.list_models()
        assert str(cached.value) == str(first.value)

    async def test_the_catalog_is_retried_once_the_failure_goes_stale(self):
        clock = _Clock()
        adapter, calls = self._adapter(clock)
        with pytest.raises(ProviderUnavailable):
            await adapter.list_models()
        clock.now += adapter._catalog_error_ttl + 1
        with pytest.raises(ProviderUnavailable):
            await adapter.list_models()
        assert len(calls) == 2

    async def test_a_recovered_catalog_clears_the_failure(self):
        clock = _Clock()
        state = {"fail": True}

        def handler(request):
            if state["fail"]:
                raise httpx.ConnectTimeout("timeout")
            return httpx.Response(
                200, json={"data": [{"id": self.MODEL, "max_context_length": 262144}]}
            )

        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL),
            _settings(),
            credential=SecretStr("k"),
            transport=httpx.MockTransport(handler),
            clock=clock,
        )
        with pytest.raises(ProviderUnavailable):
            await adapter.list_models()
        state["fail"] = False
        clock.now += adapter._catalog_error_ttl + 1
        assert [m.model_id for m in await adapter.list_models()] == [self.MODEL]
        assert adapter._catalog_error is None


class TestCatalogDropReasons:
    """REVIEWED_MODEL_UNAVAILABLE_OR_PRICING_CHANGED covered five different causes."""

    MODEL = "google/gemma-4-26b-a4b-it:free"

    async def _drops(self, entries):
        transport, _ = _transport({("GET", "/models"): (200, {"data": entries})})
        adapter = OpenRouterFreeAdapter(
            _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL, context=262144),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        assert await adapter.list_models() == []
        return adapter

    async def test_a_model_missing_from_the_catalog_says_so(self):
        adapter = await self._drops([{"id": "someone/else:free"}])
        assert adapter.safe_diagnostics()["catalog_drops"] == {self.MODEL: "ABSENT_FROM_CATALOG"}

    async def test_a_model_that_lost_its_zero_price_says_so(self):
        adapter = await self._drops(
            [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0.1", "completion": "0.2"},
                }
            ]
        )
        assert adapter.safe_diagnostics()["catalog_drops"] == {self.MODEL: "NOT_ZERO_PRICED"}

    async def test_a_model_whose_context_shrank_names_the_reviewed_value(self):
        adapter = await self._drops(
            [
                {
                    "id": self.MODEL,
                    "context_length": 131072,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        )
        assert adapter.safe_diagnostics()["catalog_drops"] == {
            self.MODEL: "CATALOG_CONTEXT_BELOW_REVIEWED_262144"
        }

    async def test_a_model_switched_off_upstream_says_so(self):
        adapter = await self._drops(
            [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "active": False,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        )
        assert adapter.safe_diagnostics()["catalog_drops"] == {self.MODEL: "INACTIVE_IN_CATALOG"}

    async def test_a_catalog_reporting_no_context_says_so(self):
        adapter = await self._drops(
            [{"id": self.MODEL, "pricing": {"prompt": "0", "completion": "0"}}]
        )
        assert adapter.safe_diagnostics()["catalog_drops"] == {self.MODEL: "CONTEXT_NOT_REPORTED"}

    async def test_a_routable_model_records_no_drop(self):
        transport, _ = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {
                        "data": [
                            {
                                "id": self.MODEL,
                                "context_length": 262144,
                                "pricing": {"prompt": "0", "completion": "0"},
                            }
                        ]
                    },
                )
            }
        )
        adapter = OpenRouterFreeAdapter(
            _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL, context=262144),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        assert len(await adapter.list_models()) == 1
        assert "catalog_drops" not in adapter.safe_diagnostics()


class TestPublishedOutputLimit:
    """A live catalog is current; a reviewed descriptor is a claim from the day it was written."""

    MODEL = "google/gemma-4-26b-a4b-it:free"

    async def _limit(self, top_provider, reviewed=4096):
        entry = {
            "id": self.MODEL,
            "context_length": 262144,
            "pricing": {"prompt": "0", "completion": "0"},
        }
        if top_provider is not None:
            entry["top_provider"] = top_provider
        transport, _ = _transport({("GET", "/models"): (200, {"data": [entry]})})
        spec = _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL, context=262144)
        spec.models[0].max_output_tokens = reviewed
        adapter = OpenRouterFreeAdapter(
            spec, _settings(), credential=SecretStr("k"), transport=transport
        )
        models = await adapter.list_models()
        return models[0].max_output_tokens

    async def test_a_smaller_published_limit_lowers_the_reviewed_one(self):
        assert await self._limit({"max_completion_tokens": 2048}, reviewed=4096) == 2048

    async def test_a_larger_published_limit_never_raises_the_reviewed_one(self):
        """Raising a limit on unreviewed data is the guess this package refuses to make."""
        assert await self._limit({"max_completion_tokens": 65536}, reviewed=4096) == 4096

    async def test_a_reviewed_descriptor_with_no_limit_adopts_the_published_one(self):
        assert await self._limit({"max_completion_tokens": 8192}, reviewed=None) == 8192

    async def test_an_absent_field_changes_nothing(self):
        assert await self._limit(None, reviewed=4096) == 4096

    async def test_an_unreadable_field_changes_nothing(self):
        assert await self._limit({"max_completion_tokens": "lots"}, reviewed=4096) == 4096
        assert await self._limit("not-an-object", reviewed=4096) == 4096

    async def test_a_provider_that_publishes_no_limit_is_unaffected(self):
        """Every adapter but OpenRouter keeps output_field None until it is reviewed."""
        assert GroqAdapter.output_field is None
        assert CloudflareWorkersAiAdapter.output_field is None
        assert MistralAdapter.output_field is None
        assert KiloFreeAdapter.output_field is None

    async def test_the_published_limit_governs_dispatch(self):
        """A budget above what the provider publishes is refused here, not by an HTTP 400."""
        entry = {
            "id": self.MODEL,
            "context_length": 262144,
            "pricing": {"prompt": "0", "completion": "0"},
            "top_provider": {"max_completion_tokens": 100},
        }
        transport, seen = _transport(
            {
                ("GET", "/models"): (200, {"data": [entry]}),
                ("GET", "/key"): (200, {"data": {"is_free_tier": True}}),
                ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"cost": 0})),
            }
        )
        spec = _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL, context=262144)
        spec.models[0].max_output_tokens = 4096
        adapter = OpenRouterFreeAdapter(
            spec, _settings(), credential=SecretStr("k"), transport=transport
        )
        request = _request(self.MODEL)
        with pytest.raises(RequestNotSupported, match="OUTPUT_BUDGET_INVALID"):
            await adapter.complete(request.model_copy(update={"max_output_tokens": 101}))
        assert not any(r.method == "POST" for r in seen)
        await adapter.complete(request.model_copy(update={"max_output_tokens": 100}))
        assert json.loads(seen[-1].content)["max_tokens"] == 100


# ── Streaming transport and scaled budgets ───────────────────────────────


def _sse(events):
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


class TestStreamingTransport:
    MODEL = "openai/gpt-oss-20b"

    def _adapter(self, events, settings=None, cls=GroqAdapter, provider="groq"):
        transport, seen = _transport(
            {
                ("GET", "/models"): (
                    200,
                    {"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                ),
                ("POST", "/chat/completions"): lambda request: _sse(events),
            }
        )
        return (
            cls(
                _spec(provider, "FREE_RECURRING", self.MODEL),
                settings or _settings(),
                credential=SecretStr("k"),
                transport=transport,
            ),
            seen,
        )

    def _events(self, pieces, finish="stop", usage=None):
        events = [{"model": self.MODEL, "choices": [{"delta": {"role": "assistant"}}]}]
        events += [{"model": self.MODEL, "choices": [{"delta": {"content": p}}]} for p in pieces]
        events.append({"model": self.MODEL, "choices": [{"delta": {}, "finish_reason": finish}]})
        if usage is not None:
            events.append({"model": self.MODEL, "choices": [], "usage": usage})
        return events

    async def test_a_streamed_answer_is_assembled_in_order(self):
        adapter, _ = self._adapter(self._events(["Hel", "lo ", "world"]))
        response = await adapter.complete(_request(self.MODEL))
        assert response.text == "Hello world"
        assert response.finish_reason == "stop"

    async def test_the_request_asks_for_a_stream(self):
        adapter, seen = self._adapter(self._events(["x"]))
        await adapter.complete(_request(self.MODEL))
        assert json.loads(seen[-1].content)["stream"] is True

    async def test_a_truncated_answer_still_reports_length(self):
        adapter, _ = self._adapter(self._events(["x"], finish="length"))
        assert (await adapter.complete(_request(self.MODEL))).finish_reason == "length"

    async def test_an_empty_stream_is_refused(self):
        adapter, _ = self._adapter([])
        with pytest.raises(MalformedResponse, match="EMPTY_COMPLETION_STREAM"):
            await adapter.complete(_request(self.MODEL))

    async def test_a_stream_with_no_content_is_refused(self):
        adapter, _ = self._adapter(self._events([]))
        with pytest.raises(MalformedResponse, match="INVALID_CHAT_COMPLETION"):
            await adapter.complete(_request(self.MODEL))

    async def test_a_stream_whose_chunks_disagree_about_the_model_is_refused(self):
        """Letting the last chunk win would accept a body that came from elsewhere."""
        events = self._events(["x"])
        events[1]["model"] = "someone/else"
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INCONSISTENT_COMPLETION_STREAM"):
            await adapter.complete(_request(self.MODEL))

    async def test_a_stream_naming_only_another_model_is_refused(self):
        events = [
            {"model": "someone/else", "choices": [{"delta": {"content": "x"}}]},
            {"model": "someone/else", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INVALID_CHAT_COMPLETION"):
            await adapter.complete(_request(self.MODEL))

    async def test_a_delta_carrying_tool_calls_is_refused(self):
        events = self._events(["x"])
        events[1]["choices"][0]["delta"]["tool_calls"] = [{"id": "1"}]
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INVALID_COMPLETION_STREAM"):
            await adapter.complete(_request(self.MODEL))

    async def test_a_stream_with_no_finish_reason_is_refused(self):
        events = self._events(["x"], finish=None)
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INVALID_CHAT_COMPLETION"):
            await adapter.complete(_request(self.MODEL))


class TestStreamingKeepsTheBillingProof:
    """Streaming changes how the bytes arrive, never what has to be proved about them."""

    MODEL = "qwen/qwen3.8-27b:free"

    def _adapter(self, events, streaming):
        catalog = {
            "data": [
                {
                    "id": self.MODEL,
                    "context_length": 262144,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        }
        transport, _ = _transport(
            {
                ("GET", "/models"): (200, catalog),
                ("POST", "/chat/completions"): lambda request: _sse(events),
            }
        )
        adapter = KiloFreeAdapter(
            _spec("kilo_free", "FREE_DYNAMIC", self.MODEL, context=262144),
            _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        adapter.supports_streaming = streaming
        return adapter

    def _events(self, usage=None):
        events = [
            {"model": self.MODEL, "choices": [{"delta": {"role": "assistant"}}]},
            {"model": self.MODEL, "choices": [{"delta": {"content": "hi"}}]},
            {"model": self.MODEL, "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        if usage is not None:
            events.append({"model": self.MODEL, "choices": [], "usage": usage})
        return events

    async def test_a_stream_reporting_a_nonzero_cost_is_refused(self):
        adapter = self._adapter(self._events(usage={"cost_microdollars": 7}), streaming=True)
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))

    async def test_a_stream_carrying_a_zero_cost_is_accepted(self):
        adapter = self._adapter(self._events(usage={"cost_microdollars": 0}), streaming=True)
        assert (await adapter.complete(_request(self.MODEL))).text == "hi"

    async def test_every_adapter_streams_now_that_the_cost_proof_is_verified(self):
        """Probed 2026-09-30: a streamed completion still carried the zero-cost proof."""
        assert OpenRouterFreeAdapter.supports_streaming is True
        assert KiloFreeAdapter.supports_streaming is True
        assert GroqAdapter.supports_streaming is True
        assert MistralAdapter.supports_streaming is True
        assert CloudflareWorkersAiAdapter.supports_streaming is True
        assert GeminiAdapter.supports_streaming is True


class TestCompletionBudgets:
    MODEL = "openai/gpt-oss-20b"

    def _adapter(self, **settings_kw):
        transport, _ = _transport({})
        return GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            LiveSettings(enabled=True, confirmed_providers=set(_CLOUD_PROVIDERS), **settings_kw),
            credential=SecretStr("k"),
            transport=transport,
        )

    def _read(self, adapter, tokens, streaming):
        request = _request(self.MODEL).model_copy(update={"max_output_tokens": tokens})
        return adapter._completion_timeout(request, streaming).read

    def test_a_streaming_read_is_the_idle_budget_whatever_the_size(self):
        """Tokens arriving reset it, so it detects a stall rather than bounding the answer."""
        adapter = self._adapter()
        assert self._read(adapter, 256, True) == 25
        assert self._read(adapter, 32768, True) == 25

    def test_a_buffered_read_has_to_cover_the_whole_answer(self):
        adapter = self._adapter()
        assert self._read(adapter, 300, False) == pytest.approx(32.5)
        assert self._read(adapter, 3000, False) == pytest.approx(100)

    def test_no_single_read_may_exceed_the_ceiling(self):
        adapter = self._adapter(max_completion_seconds=90)
        assert self._read(adapter, 1_000_000, False) == 90

    def test_the_assumed_rate_is_configurable(self):
        adapter = self._adapter(output_tokens_per_second=10)
        assert self._read(adapter, 300, False) == pytest.approx(55)


class TestADiagnosticBelongsToOneRequest:
    """Kept past its request, a diagnostic is read against the next call and blames
    the wrong model for the previous one's failure."""

    MODEL = "openai/gpt-oss-20b"

    def _adapter(self, completions):
        """completions: a list of (status, body) served in order."""
        remaining = list(completions)

        def handler(request):
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200,
                    json={"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                )
            status, body = remaining.pop(0)
            if status == 200 and _asked_for_a_stream(request):
                return _as_sse(body)
            return httpx.Response(status, json=body)

        return GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            _diagnostic_settings(),
            credential=SecretStr("sk-secret-value"),
            transport=httpx.MockTransport(handler),
        )

    async def test_a_success_clears_the_previous_failure(self):
        adapter = self._adapter(
            [
                (400, {"error": {"message": "first model was throttled"}}),
                (200, _completion(MODEL_OK := self.MODEL)),
            ]
        )
        with pytest.raises(ProviderUnavailable):
            await adapter.complete(_request(self.MODEL))
        assert "throttled" in adapter.safe_diagnostics()["last_provider_error"]["provider_message"]
        await adapter.complete(_request(MODEL_OK))
        assert adapter.safe_diagnostics() == {}

    async def test_a_later_failure_does_not_report_an_earlier_one(self):
        adapter = self._adapter(
            [
                (400, {"error": {"message": "daily limit for some other model"}}),
                (200, _completion(self.MODEL)),
                (503, {"error": {"message": "upstream unavailable"}}),
            ]
        )
        with pytest.raises(ProviderUnavailable):
            await adapter.complete(_request(self.MODEL))
        await adapter.complete(_request(self.MODEL))
        with pytest.raises(ProviderUnavailable):
            await adapter.complete(_request(self.MODEL))
        message = adapter.safe_diagnostics()["last_provider_error"]["provider_message"]
        assert "upstream unavailable" in message
        assert "some other model" not in message

    async def test_the_record_names_the_request_it_came_from(self):
        adapter = self._adapter([(400, {"error": {"message": "bad"}})])
        with pytest.raises(ProviderUnavailable):
            await adapter.complete(_request(self.MODEL))
        assert adapter.safe_diagnostics()["last_provider_error"]["path"] == "/chat/completions"


class TestAnErrorInsideAStreamIsStillAnError:
    """A provider can answer 200 and put the failure in the body. Reading that as a
    malformed completion loses a throttle the governor would have acted on."""

    MODEL = "openai/gpt-oss-20b"

    def _adapter(self, events, headers=None):
        text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

        def handler(request):
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200,
                    json={"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]},
                )
            return httpx.Response(
                200,
                content=text.encode(),
                headers={"content-type": "text/event-stream", **(headers or {})},
            )

        return GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL),
            _diagnostic_settings(),
            credential=SecretStr("sk-secret-value"),
            transport=httpx.MockTransport(handler),
        )

    async def test_a_rate_limit_delivered_in_a_stream_is_a_rate_limit(self):
        adapter = self._adapter([{"error": {"code": 429, "message": "Rate limit exceeded"}}])
        with pytest.raises(RateLimited):
            await adapter.complete(_request(self.MODEL))

    async def test_an_exhausted_quota_delivered_in_a_stream_is_refused_as_one(self):
        adapter = self._adapter(
            [{"error": {"code": 429, "message": "daily cap"}}],
            headers={"x-ratelimit-limit-requests": "50", "x-ratelimit-remaining-requests": "0"},
        )
        with pytest.raises(QuotaExceeded):
            await adapter.complete(_request(self.MODEL))

    async def test_an_unauthenticated_stream_is_refused_as_one(self):
        adapter = self._adapter([{"error": {"code": 401, "message": "bad key"}}])
        with pytest.raises(AuthenticationFailed):
            await adapter.complete(_request(self.MODEL))

    async def test_the_message_is_kept_for_the_operator(self):
        adapter = self._adapter([{"error": {"code": 429, "message": "Rate limit exceeded: rpd"}}])
        with pytest.raises(RateLimited):
            await adapter.complete(_request(self.MODEL))
        record = adapter.safe_diagnostics()["last_provider_error"]
        assert "Rate limit exceeded: rpd" in record["provider_message"]

    async def test_a_success_envelope_with_success_false_is_refused(self):
        adapter = self._adapter([{"success": False, "result": None}])
        with pytest.raises((ProviderUnavailable, MalformedResponse)):
            await adapter.complete(_request(self.MODEL))

    async def test_an_ordinary_stream_is_unaffected(self):
        adapter = self._adapter(
            [
                {"model": self.MODEL, "choices": [{"delta": {"content": "fine"}}]},
                {"model": self.MODEL, "choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        )
        assert (await adapter.complete(_request(self.MODEL))).text == "fine"
