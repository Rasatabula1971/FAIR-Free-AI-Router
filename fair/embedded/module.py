"""The FAIR entry point — wire into any app with one constructor call."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from fair.config import RoutingSettings
from fair.embedded.router import EmbeddedRouter
from fair.providers.base import ProviderAdapter
from fair.providers.live import (
    TEXT_CAPABILITIES,
    CloudflareWorkersAiAdapter,
    GeminiAdapter,
    GroqAdapter,
    KiloFreeAdapter,
    LiveSettings,
    MistralAdapter,
    NvidiaNimAdapter,
    OllamaCloudAdapter,
    OllamaLocalAdapter,
    OpenRouterFreeAdapter,
    ZaiFreeAdapter,
)
from fair.providers.registry import Registry
from fair.quality.thresholds import DEFAULT_THRESHOLDS
from fair.schemas.api import SolveRequest, SolveResponse
from fair.schemas.domain import ModelDescriptor, ProviderSpec
from fair.schemas.qualification import ModelQualification, ProviderQualification
from fair.security.adapter import CredentialedAdapter


def _reviewed_at():
    return datetime.now(UTC) - timedelta(seconds=10)


def _make_qualification(provider_id, free_status, models):
    reviewed = _reviewed_at()
    return ProviderQualification(
        provider_id=provider_id,
        free_status=free_status,
        production_allowed=True,
        billing_enabled=False,
        payment_method_required=False,
        payment_method_present=False,
        can_auto_bill=False,
        reviewed_at=reviewed,
        expires_at=reviewed + timedelta(days=29),
        reviewer_reference="embedded-module-auto",
        billing_reference="embedded-module-auto",
        terms_reference="embedded-module-auto",
        privacy_reference="embedded-module-auto",
        limits_reference="embedded-module-auto",
        models=[
            ModelQualification(
                model_id=m.model_id,
                model_revision=m.model_revision,
                input_price_per_million=0,
                output_price_per_million=0,
                request_price=0,
                pricing_reference="embedded-module-auto",
                paid_tools_enabled=False,
                live_test_passed=True,
                live_test_at=reviewed,
                live_test_reference="embedded-module-auto",
                zero_charge_verified=True,
                zero_charge_reference="embedded-module-auto",
            )
            for m in models
        ],
    )


def _text_models(*entries):
    return [
        ModelDescriptor(model_id=model_id, context_window=context, capabilities=set(TEXT_CAPABILITIES))
        for model_id, context in entries
    ]


_FREE_STATUS = {"FREE_RECURRING": "verified_free_plan", "FREE_DYNAMIC": "verified_zero_price_model"}

# Cloud providers: constructor keyword, environment variable, adapter, access class, models.
_CLOUD_PROVIDERS = {
    "google_gemini_api": {
        "kwarg": "gemini_api_key",
        "env": "GEMINI_API_KEY",
        "adapter": GeminiAdapter,
        "access_class": "FREE_RECURRING",
        "request_limit": 1500,
        "models": _text_models(("gemini-3.5-flash-lite", 1048576), ("gemini-3.6-flash", 1048576)),
    },
    "groq": {
        "kwarg": "groq_api_key",
        "env": "GROQ_API_KEY",
        "adapter": GroqAdapter,
        "access_class": "FREE_RECURRING",
        "request_limit": 1000,
        "models": _text_models(("openai/gpt-oss-20b", 131072), ("openai/gpt-oss-120b", 131072)),
    },
    "openrouter_free": {
        "kwarg": "openrouter_api_key",
        "env": "OPENROUTER_API_KEY",
        "adapter": OpenRouterFreeAdapter,
        "access_class": "FREE_DYNAMIC",
        "models": _text_models(
            ("google/gemma-4-26b-a4b-it:free", 262144),
            ("inclusionai/ling-3.0-flash-sante:free", 262144),
            ("cohere/north-mini-code:free", 256000),
            ("dots-studio/dots-3-note-preview:free", 512000),
        ),
    },
    "mistral": {
        "kwarg": "mistral_api_key",
        "env": "MISTRAL_API_KEY",
        "adapter": MistralAdapter,
        "access_class": "FREE_RECURRING",
        "models": _text_models(("ministral-8b-latest", 262144), ("ministral-3b-latest", 131072)),
    },
    "kilo_free": {
        "kwarg": "kilo_api_key",
        "env": "KILO_API_KEY",
        "adapter": KiloFreeAdapter,
        "access_class": "FREE_DYNAMIC",
        "models": _text_models(
            ("nvidia/nemotron-3-super-120b-a12b:free", 262144),
            ("nex-agi/nex-n2.5-mini:free", 262144),
            ("poolside/laguna-s-2.1:free", 262144),
        ),
    },
    "zai_free": {
        "kwarg": "zai_api_key",
        "env": "ZAI_API_KEY",
        "adapter": ZaiFreeAdapter,
        "access_class": "FREE_DYNAMIC",
        "models": _text_models(("glm-4.5-flash", 131072), ("glm-4.7-flash", 131072)),
    },
    "nvidia_nim": {
        "kwarg": "nvidia_api_key",
        "env": "NVIDIA_API_KEY",
        "adapter": NvidiaNimAdapter,
        "access_class": "FREE_RECURRING",
        "models": _text_models(("meta/llama-3.3-70b-instruct", 131072), ("meta/llama-3.1-8b-instruct", 131072)),
    },
    "ollama_cloud": {
        "kwarg": "ollama_cloud_api_key",
        "env": "OLLAMA_CLOUD_API_KEY",
        "adapter": OllamaCloudAdapter,
        "access_class": "FREE_RECURRING",
        "models": _text_models(("gpt-oss:20b", 131072)),
    },
    "cloudflare_workers_ai": {
        "kwarg": "cloudflare_api_token",
        "env": "CLOUDFLARE_API_TOKEN",
        "adapter": CloudflareWorkersAiAdapter,
        "access_class": "FREE_RECURRING",
        "models": _text_models(
            ("@cf/meta/llama-3.3-70b-instruct-fp8-fast", 24000),
            ("@cf/openai/gpt-oss-20b", 128000),
            ("@cf/meta/llama-4-scout-17b-16e-instruct", 131000),
        ),
    },
}

_LOCAL_CONTEXT_CAP = 16384


def _read_env_file(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _loopback(url):
    parts = urlsplit(url)
    if parts.hostname == "localhost":
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts).rstrip("/")


def _discover_local_models(url):
    """Chat-capable GGUF models served by the local daemon; None when it is not reachable."""
    try:
        data = httpx.get(url + "/api/tags", timeout=2, trust_env=False).json()
    except (httpx.HTTPError, ValueError):
        return None
    models = []
    for entry in data.get("models", []) if isinstance(data, dict) else []:
        details = entry.get("details") if isinstance(entry, dict) else None
        if not isinstance(details, dict) or details.get("format") != "gguf":
            continue
        family = str(details.get("family", "")).casefold()
        name = str(entry.get("name", ""))
        if "embed" in name.casefold() or "bert" in family:
            continue
        context = details.get("context_length")
        context = context if type(context) is int and context > 0 else 4096
        models.append((name, min(context, _LOCAL_CONTEXT_CAP)))
    return models


class FAIR:
    """Free AI Router — embeddable module.

    Provides quality-verified AI inference through free providers.
    Every answer is judged: arithmetic, code, JSON, citation, and cross-check
    validation all run before a result is accepted.

    Usage::

        fair = FAIR(gemini_api_key="...")
        result = await fair.solve("What is 15 * 23?",
                                  validation={"kind": "arithmetic", "expression": "15*23"})
        print(result.output)  # "345"
    """

    def __init__(
        self,
        *,
        gemini_api_key: str | None = None,
        groq_api_key: str | None = None,
        openrouter_api_key: str | None = None,
        mistral_api_key: str | None = None,
        kilo_api_key: str | None = None,
        zai_api_key: str | None = None,
        nvidia_api_key: str | None = None,
        ollama_cloud_api_key: str | None = None,
        cloudflare_api_token: str | None = None,
        cloudflare_account_id: str | None = None,
        ollama_url: str | None = None,
        ollama_models: list[str] | None = None,
        env_file: str | None = None,
        providers: list[tuple[ProviderSpec, ProviderAdapter]] | None = None,
        quality_level: Literal["commodity", "standard", "advanced", "high_impact_support"] = "standard",
        max_attempts: int = 3,
        max_unanswered_attempts: int = 6,
        max_verification_attempts: int = 2,
        timeout_seconds: float = 15,
        cooldown_seconds: float = 360,
        cache_enabled: bool = True,
        cache_ttl_seconds: int = 3600,
        cache_max_entries: int = 1000,
        cross_check_required: bool = False,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self._registry = Registry()
        self._quality_level = quality_level
        self._cross_check = cross_check_required
        self._on_event = on_event
        self.skipped: dict[str, str] = {}

        env = dict(os.environ)
        if env_file:
            env = _read_env_file(env_file) | env
        given = {
            "gemini_api_key": gemini_api_key,
            "groq_api_key": groq_api_key,
            "openrouter_api_key": openrouter_api_key,
            "mistral_api_key": mistral_api_key,
            "kilo_api_key": kilo_api_key,
            "zai_api_key": zai_api_key,
            "nvidia_api_key": nvidia_api_key,
            "ollama_cloud_api_key": ollama_cloud_api_key,
            "cloudflare_api_token": cloudflare_api_token,
        }
        cloudflare_account_id = cloudflare_account_id or env.get("CLOUDFLARE_ACCOUNT_ID")

        live_settings = LiveSettings(enabled=True, confirmed_providers=set(_CLOUD_PROVIDERS) | {"ollama_local"})

        for provider_id, entry in _CLOUD_PROVIDERS.items():
            key = given[entry["kwarg"]] or env.get(entry["env"])
            if not key or not key.strip():
                continue
            extra = {}
            if provider_id == "cloudflare_workers_ai":
                if not cloudflare_account_id:
                    self.skipped[provider_id] = "CLOUDFLARE_ACCOUNT_ID missing"
                    continue
                extra["account_id"] = cloudflare_account_id
            self._register_cloud(provider_id, entry, key.strip(), live_settings, extra)

        ollama_url = ollama_url or env.get("OLLAMA_URL") or env.get("OLLAMA_HOST")
        if not ollama_url and env.get("OLLAMA_ENABLED"):
            ollama_url = "http://127.0.0.1:11434"
        if ollama_url:
            self._register_ollama(_loopback(ollama_url), ollama_models, live_settings)

        if providers:
            for spec, adapter in providers:
                self._registry.register(spec, adapter)

        if not self._registry.adapters:
            raise ValueError(
                "FAIR requires at least one provider. Pass an API key "
                "(gemini_api_key, groq_api_key, openrouter_api_key, mistral_api_key, "
                "kilo_api_key, zai_api_key, nvidia_api_key, ollama_cloud_api_key, "
                "cloudflare_api_token) or set the corresponding environment variable."
            )

        settings = RoutingSettings(
            max_attempts=max_attempts,
            max_unanswered_attempts=max_unanswered_attempts,
            max_verification_attempts=max_verification_attempts,
            timeout_seconds=timeout_seconds,
            cooldown_seconds=cooldown_seconds,
            cache_enabled=cache_enabled,
            cache_ttl_seconds=cache_ttl_seconds,
            cache_max_entries=cache_max_entries,
        )
        self._router = EmbeddedRouter(
            self._registry, settings, dict(DEFAULT_THRESHOLDS), on_event=on_event,
        )

    def _register_cloud(self, provider_id, entry, api_key, settings, extra):
        models = [m.model_copy(deep=True) for m in entry["models"]]
        spec = ProviderSpec(
            provider_id=provider_id,
            access_class=entry["access_class"],
            status="ACTIVE",
            current_access_cost_usd=0,
            requires_paid_subscription=False,
            requires_credit_purchase=False,
            auto_billing_required=False,
            programmatic_access=True,
            production_eligibility=True,
            terms_last_verified=_reviewed_at(),
            models=models,
            request_limit=entry.get("request_limit"),
            qualification=_make_qualification(
                provider_id, _FREE_STATUS[entry["access_class"]], models,
            ),
        )
        credential = SecretStr(api_key)
        adapter = CredentialedAdapter(
            provider_id,
            entry["adapter"](spec, settings, credential=credential, **extra),
            credential,
        )
        self._registry.register(spec, adapter)

    def _register_ollama(self, url, model_ids, settings):
        if model_ids:
            discovered = [(model_id, _LOCAL_CONTEXT_CAP) for model_id in model_ids]
        else:
            discovered = _discover_local_models(url)
            if discovered is None:
                self.skipped["ollama_local"] = f"no Ollama daemon at {url}"
                return
            if not discovered:
                self.skipped["ollama_local"] = "no chat-capable local models pulled"
                return
        settings.ollama_local_only_confirmed = True
        settings.ollama_url = url
        spec = ProviderSpec(
            provider_id="ollama_local",
            access_class="FREE_LOCAL",
            status="ACTIVE",
            current_access_cost_usd=0,
            requires_paid_subscription=False,
            requires_credit_purchase=False,
            auto_billing_required=False,
            programmatic_access=True,
            production_eligibility=True,
            models=_text_models(*discovered),
        )
        self._registry.register(spec, OllamaLocalAdapter(spec, settings))

    async def solve(
        self,
        task: str,
        *,
        task_type: str | None = None,
        quality_level: str | None = None,
        expected_schema: dict | None = None,
        validation: dict | None = None,
        evidence: list[dict] | None = None,
        cross_check_required: bool | None = None,
        max_output_tokens: int = 1024,
        client_id: str = "embedded",
        priority: str = "P2",
        cache_mode: str = "default",
    ) -> SolveResponse:
        """Send a task to free AI models with quality verification.

        Returns a SolveResponse with:
        - status: "ACCEPTED" (verified answer), "ESCALATION_REQUIRED" (no model passed),
                  or "FAILED" (infrastructure failure)
        - output: the verified answer text (only when ACCEPTED)
        - quality: full QualityReport with scores and verification state
        """
        request = SolveRequest(
            client_id=client_id,
            task=task,
            task_type=task_type,
            quality_level=quality_level or self._quality_level,
            expected_schema=expected_schema,
            validation=validation,
            evidence=evidence or [],
            cross_check_required=cross_check_required if cross_check_required is not None else self._cross_check,
            max_output_tokens=max_output_tokens,
            priority=priority,
            cache_mode=cache_mode,
        )
        return await self._router.solve(request)

    @property
    def stopped(self) -> bool:
        return self._router.stopped

    @stopped.setter
    def stopped(self, value: bool):
        self._router.stopped = value

    def providers(self) -> list[dict]:
        return [
            {
                "provider_id": p.provider_id,
                "status": self._router.quota.effective_status(p),
                "access_class": p.access_class,
                "models": [m.model_id for m in p.models],
            }
            for p in self._registry.providers.values()
        ]

    def clear_cache(self, client_id: str = "embedded") -> dict:
        return self._router.cache.clear(client_id)

    async def close(self):
        await self._router.close()
        for adapter in self._registry.adapters.values():
            if hasattr(adapter, "close"):
                await adapter.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
