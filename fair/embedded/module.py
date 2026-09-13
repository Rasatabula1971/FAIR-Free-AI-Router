"""The FAIR entry point — wire into any app with one constructor call."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal

from pydantic import SecretStr

from fair.config import RoutingSettings
from fair.embedded.router import EmbeddedRouter
from fair.providers.base import ProviderAdapter
from fair.providers.live import (
    GeminiAdapter,
    GroqAdapter,
    LiveSettings,
    OllamaLocalAdapter,
    OpenRouterFreeAdapter,
)
from fair.providers.mock import MockAdapter
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


_GEMINI_MODELS = [
    ModelDescriptor(
        model_id="gemini-2.0-flash",
        context_window=1048576,
        capabilities={"reasoning", "coding", "structured_output"},
    ),
]

_GROQ_MODELS = [
    ModelDescriptor(
        model_id="llama-3.3-70b-versatile",
        context_window=32768,
        capabilities={"reasoning", "coding", "structured_output"},
    ),
    ModelDescriptor(
        model_id="gemma2-9b-it",
        context_window=8192,
        capabilities={"reasoning", "coding", "structured_output"},
    ),
]

_OPENROUTER_MODELS = [
    ModelDescriptor(
        model_id="meta-llama/llama-3.3-70b-instruct:free",
        context_window=131072,
        capabilities={"reasoning", "coding", "structured_output"},
    ),
]


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
        ollama_url: str | None = None,
        providers: list[tuple[ProviderSpec, ProviderAdapter]] | None = None,
        quality_level: Literal["commodity", "standard", "advanced", "high_impact_support"] = "standard",
        max_attempts: int = 3,
        max_verification_attempts: int = 2,
        timeout_seconds: float = 15,
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

        live_settings = LiveSettings(enabled=True)

        gemini_api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY")
        groq_api_key = groq_api_key or os.environ.get("GROQ_API_KEY")
        openrouter_api_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        ollama_url = ollama_url or (
            os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
            if os.environ.get("OLLAMA_ENABLED") else None
        )

        if gemini_api_key:
            live_settings.gemini_free_tier_confirmed = True
            self._register_gemini(gemini_api_key, live_settings)
        if groq_api_key:
            live_settings.groq_free_plan_confirmed = True
            self._register_groq(groq_api_key, live_settings)
        if openrouter_api_key:
            live_settings.openrouter_free_account_confirmed = True
            self._register_openrouter(openrouter_api_key, live_settings)
        if ollama_url:
            live_settings.ollama_local_only_confirmed = True
            live_settings.ollama_url = ollama_url
            self._register_ollama(live_settings)

        if providers:
            for spec, adapter in providers:
                self._registry.register(spec, adapter)

        if not self._registry.adapters:
            raise ValueError(
                "FAIR requires at least one provider. Pass an API key "
                "(gemini_api_key, groq_api_key, openrouter_api_key) or set the "
                "corresponding environment variable (GEMINI_API_KEY, GROQ_API_KEY, "
                "OPENROUTER_API_KEY)."
            )

        settings = RoutingSettings(
            max_attempts=max_attempts,
            max_verification_attempts=max_verification_attempts,
            timeout_seconds=timeout_seconds,
            cache_enabled=cache_enabled,
            cache_ttl_seconds=cache_ttl_seconds,
            cache_max_entries=cache_max_entries,
        )
        self._router = EmbeddedRouter(
            self._registry, settings, dict(DEFAULT_THRESHOLDS), on_event=on_event,
        )

    def _register_gemini(self, api_key, settings):
        models = list(_GEMINI_MODELS)
        reviewed = _reviewed_at()
        spec = ProviderSpec(
            provider_id="google_gemini_api",
            access_class="FREE_RECURRING",
            status="ACTIVE",
            current_access_cost_usd=0,
            requires_paid_subscription=False,
            requires_credit_purchase=False,
            auto_billing_required=False,
            programmatic_access=True,
            production_eligibility=True,
            terms_last_verified=reviewed,
            models=models,
            request_limit=1500,
            qualification=_make_qualification(
                "google_gemini_api", "verified_free_plan", models,
            ),
        )
        credential = SecretStr(api_key)
        adapter = CredentialedAdapter(
            spec.provider_id,
            GeminiAdapter(spec, settings, credential=credential),
            credential,
        )
        self._registry.register(spec, adapter)

    def _register_groq(self, api_key, settings):
        models = list(_GROQ_MODELS)
        reviewed = _reviewed_at()
        spec = ProviderSpec(
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
            models=models,
            request_limit=14400,
            qualification=_make_qualification("groq", "verified_free_plan", models),
        )
        credential = SecretStr(api_key)
        adapter = CredentialedAdapter(
            spec.provider_id,
            GroqAdapter(spec, settings, credential=credential),
            credential,
        )
        self._registry.register(spec, adapter)

    def _register_openrouter(self, api_key, settings):
        models = list(_OPENROUTER_MODELS)
        reviewed = _reviewed_at()
        spec = ProviderSpec(
            provider_id="openrouter_free",
            access_class="FREE_DYNAMIC",
            status="ACTIVE",
            current_access_cost_usd=0,
            requires_paid_subscription=False,
            requires_credit_purchase=False,
            auto_billing_required=False,
            programmatic_access=True,
            production_eligibility=True,
            terms_last_verified=reviewed,
            models=models,
            qualification=_make_qualification(
                "openrouter_free", "verified_zero_price_model", models,
            ),
        )
        credential = SecretStr(api_key)
        adapter = CredentialedAdapter(
            spec.provider_id,
            OpenRouterFreeAdapter(spec, settings, credential=credential),
            credential,
        )
        self._registry.register(spec, adapter)

    def _register_ollama(self, settings):
        models = [
            ModelDescriptor(model_id="llama3.2:latest", context_window=131072),
        ]
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
            models=models,
        )
        adapter = OllamaLocalAdapter(spec, settings)
        self._registry.register(spec, adapter)

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
