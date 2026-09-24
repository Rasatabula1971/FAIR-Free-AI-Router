"""Transport-level tests for the live adapters — no network, no credentials leave the process."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from fair.embedded.module import _CLOUD_PROVIDERS, FAIR, _loopback
from fair.providers.base import AuthenticationFailed, BillingViolation, QuotaExceeded
from fair.providers.live import (
    CloudflareWorkersAiAdapter,
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
    free_status = "verified_free_plan" if access_class == "FREE_RECURRING" else "verified_zero_price_model"
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
        task="ping", model_id=model_id, request_id="r", client_id="c", task_class="general",
        max_output_tokens=64,
    )


def _completion(model_id, content="pong", usage=None):
    body = {
        "model": model_id,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _transport(routes):
    """routes: {(method, path_suffix): (status, json_body) | callable(request)}."""
    seen = []

    def handler(request):
        seen.append(request)
        for (method, suffix), response in routes.items():
            if request.method == method and request.url.path.endswith(suffix) or (
                request.method == method and suffix in str(request.url)
            ):
                if callable(response):
                    return response(request)
                status, body = response
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": {"code": 404}})

    return httpx.MockTransport(handler), seen


class TestKilo:
    MODEL = "nex-agi/nex-n2.5-pro:free"

    def _adapter(self, routes):
        transport, seen = _transport(routes)
        adapter = KiloFreeAdapter(
            _spec("kilo_free", "FREE_DYNAMIC", self.MODEL), _settings(),
            credential=SecretStr("k"), transport=transport,
        )
        return adapter, seen

    async def test_zero_priced_free_model_completes(self):
        catalog = {"data": [{"id": self.MODEL, "context_length": 262144, "pricing": {"prompt": "0", "completion": "0", "discount": 0}}]}
        adapter, seen = self._adapter({
            ("GET", "/models"): (200, catalog),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"cost_microdollars": 0})),
        })
        response = await adapter.complete(_request(self.MODEL))
        assert response.text == "pong"
        assert str(seen[-1].url) == "https://api.kilo.ai/api/gateway/chat/completions"
        assert "provider" not in json.loads(seen[-1].content)

    async def test_priced_model_is_dropped_from_catalog(self):
        catalog = {"data": [{"id": self.MODEL, "context_length": 262144, "pricing": {"prompt": "0.1", "completion": "0"}}]}
        adapter, _ = self._adapter({("GET", "/models"): (200, catalog)})
        assert await adapter.list_models() == []

    async def test_nonzero_cost_is_a_billing_violation(self):
        catalog = {"data": [{"id": self.MODEL, "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"}}]}
        adapter, _ = self._adapter({
            ("GET", "/models"): (200, catalog),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"cost_microdollars": 1})),
        })
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))

    def test_non_free_model_id_is_refused(self):
        with pytest.raises(AuthenticationFailed):
            KiloFreeAdapter(
                _spec("kilo_free", "FREE_DYNAMIC", "nex-agi/nex-n2.5-pro"), _settings(),
                credential=SecretStr("k"), transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            )


class TestOpenRouter:
    MODEL = "google/gemma-4-26b-a4b-it:free"

    async def test_privacy_preferences_and_free_tier_check(self):
        transport, seen = _transport({
            ("GET", "/models"): (200, {"data": [{"id": self.MODEL, "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"}}]}),
            ("GET", "/key"): (200, {"data": {"is_free_tier": True}}),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"cost": 0})),
        })
        adapter = OpenRouterFreeAdapter(
            _spec("openrouter_free", "FREE_DYNAMIC", self.MODEL), _settings(),
            credential=SecretStr("k"), transport=transport,
        )
        await adapter.complete(_request(self.MODEL))
        payload = json.loads(seen[-1].content)
        assert payload["provider"]["data_collection"] == "deny"
        assert payload["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0, "image": 0}


class TestMistral:
    MODEL = "ministral-8b-latest"

    async def test_catalog_uses_max_context_length(self):
        transport, _ = _transport({
            ("GET", "/models"): (200, {"data": [{"id": self.MODEL, "max_context_length": 4096}]}),
        })
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL), _settings(),
            credential=SecretStr("k"), transport=transport,
        )
        assert await adapter.list_models() == []

    async def test_per_minute_quota_headers_exhaust(self):
        def chat(request):
            return httpx.Response(
                429, json={"message": "Rate limit exceeded"},
                headers={"x-ratelimit-limit-req-minute": "10", "x-ratelimit-remaining-req-minute": "0"},
            )

        transport, _ = _transport({
            ("GET", "/models"): (200, {"data": [{"id": self.MODEL, "max_context_length": 262144}]}),
            ("POST", "/chat/completions"): chat,
        })
        adapter = MistralAdapter(
            _spec("mistral", "FREE_RECURRING", self.MODEL), _settings(),
            credential=SecretStr("k"), transport=transport,
        )
        with pytest.raises(QuotaExceeded) as error:
            await adapter.complete(_request(self.MODEL))
        assert error.value.reset_at is not None


class TestZai:
    MODEL = "glm-4.5-flash"

    async def test_thinking_disabled_and_catalog_skipped(self):
        transport, seen = _transport({
            ("POST", "/chat/completions"): (200, _completion(self.MODEL)),
        })
        adapter = ZaiFreeAdapter(
            _spec("zai_free", "FREE_DYNAMIC", self.MODEL), _settings(),
            credential=SecretStr("k"), transport=transport,
        )
        response = await adapter.complete(_request(self.MODEL))
        assert response.text == "pong"
        assert [r.method for r in seen] == ["POST"]
        assert json.loads(seen[0].content)["thinking"] == {"type": "disabled"}

    def test_non_flash_model_is_refused(self):
        with pytest.raises(AuthenticationFailed):
            ZaiFreeAdapter(
                _spec("zai_free", "FREE_DYNAMIC", "glm-5"), _settings(),
                credential=SecretStr("k"), transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            )


class TestGroq:
    MODEL = "openai/gpt-oss-20b"

    async def test_uses_max_completion_tokens_and_context_window(self):
        transport, seen = _transport({
            ("GET", "/models"): (200, {"data": [{"id": self.MODEL, "context_window": 131072, "active": True}]}),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL)),
        })
        adapter = GroqAdapter(
            _spec("groq", "FREE_RECURRING", self.MODEL), _settings(),
            credential=SecretStr("k"), transport=transport,
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
            "result": [{"name": self.MODEL, "properties": [{"property_id": "context_window", "value": context}]}],
        }

    def _adapter(self, routes, clock=None):
        transport, seen = _transport(routes)
        kwargs = {"credential": SecretStr("k"), "transport": transport}
        if clock is not None:
            kwargs["clock"] = clock
        adapter = CloudflareWorkersAiAdapter(
            _spec("cloudflare_workers_ai", "FREE_RECURRING", self.MODEL, context=128000),
            _settings(), ACCOUNT, **kwargs,
        )
        return adapter, seen

    async def test_catalog_and_neuron_metering(self):
        adapter, seen = self._adapter({
            ("GET", "/ai/models/search"): (200, self._catalog()),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"neurons": 2.5})),
        })
        response = await adapter.complete(_request(self.MODEL))
        assert response.quota.quota_limit == 10_000
        assert response.quota.quota_remaining_estimate == 9_997
        assert str(seen[0].url).startswith(f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/models/search")
        assert str(seen[-1].url) == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1/chat/completions"

    async def test_missing_neurons_is_a_billing_violation(self):
        adapter, _ = self._adapter({
            ("GET", "/ai/models/search"): (200, self._catalog()),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"prompt_tokens": 1})),
        })
        with pytest.raises(BillingViolation):
            await adapter.complete(_request(self.MODEL))

    async def test_daily_allocation_stops_dispatch(self):
        adapter, seen = self._adapter({
            ("GET", "/ai/models/search"): (200, self._catalog()),
            ("POST", "/chat/completions"): (200, _completion(self.MODEL, usage={"neurons": 10_000})),
        })
        await adapter.complete(_request(self.MODEL))
        with pytest.raises(QuotaExceeded):
            await adapter.complete(_request(self.MODEL))
        assert sum(r.method == "POST" for r in seen) == 1

    async def test_small_context_model_is_dropped(self):
        adapter, _ = self._adapter({("GET", "/ai/models/search"): (200, self._catalog(context="4096"))})
        assert await adapter.list_models() == []

    def test_account_id_must_be_hex(self):
        with pytest.raises(AuthenticationFailed):
            CloudflareWorkersAiAdapter(
                _spec("cloudflare_workers_ai", "FREE_RECURRING", self.MODEL), _settings(),
                "../evil", credential=SecretStr("k"),
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
        assert FAIR(
            groq_api_key="g",
            confirmed_free_providers={"groq"},
        )._router.settings.cooldown_seconds == 360
        assert FAIR(
            groq_api_key="g",
            confirmed_free_providers={"groq"},
            cooldown_seconds=30,
        )._router.settings.cooldown_seconds == 30

    def test_localhost_normalizes_to_loopback(self):
        assert _loopback("http://localhost:11434/") == "http://127.0.0.1:11434"
        assert _loopback("http://127.0.0.1:11434") == "http://127.0.0.1:11434"

    def test_explicit_ollama_models_skip_discovery(self, monkeypatch):
        for entry in _CLOUD_PROVIDERS.values():
            monkeypatch.delenv(entry["env"], raising=False)
        fair = FAIR(ollama_url="http://localhost:1", ollama_models=["llama3.2:3b"])
        assert fair.providers()[0]["models"] == ["llama3.2:3b"]
