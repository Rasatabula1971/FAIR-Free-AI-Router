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
from fair.embedded.quota import SharedQuotaLedger
from fair.embedded.router import EmbeddedRouter
from fair.governor.policy import AdmissionDenied
from fair.providers.base import AuthenticationFailed, ProviderAdapter
from fair.providers.live import (
    TEXT_CAPABILITIES,
    CloudflareWorkersAiAdapter,
    GeminiAdapter,
    GroqAdapter,
    KiloFreeAdapter,
    LiveSettings,
    MistralAdapter,
    OllamaLocalAdapter,
    OpenRouterFreeAdapter,
    ZaiFreeAdapter,
)
from fair.providers.registry import Registry
from fair.quality.source_reviews import SourceReviewRegistry
from fair.quality.thresholds import DEFAULT_THRESHOLDS
from fair.schemas.api import SolveRequest, SolveResponse
from fair.schemas.domain import ModelDescriptor, PrivacyClass, ProviderSpec
from fair.schemas.qualification import ModelQualification, ProviderQualification
from fair.security.adapter import CredentialedAdapter
from fair.security.credentials import CredentialConfigurationError

# Built-in provider policy was manually re-verified against current provider
# documentation on this date. This MUST NOT be derived from process startup:
# qualified() intentionally expires provider evidence after 30 days so stale
# pricing/terms cannot be renewed merely by restarting FAIR.
_BUILTIN_PROVIDER_REVIEWED_AT = datetime(2026, 9, 27, tzinfo=UTC)


def _reviewed_at():
    return _BUILTIN_PROVIDER_REVIEWED_AT


def _review_reference():
    """Derived from the review date, never written out a second time.

    The qualification references used to carry the date as their own literal,
    so bumping the constant alone left them citing a different review than the
    one the evidence is dated to.
    """
    return f"builtin-provider-review-{_BUILTIN_PROVIDER_REVIEWED_AT.date().isoformat()}"


def _make_qualification(provider_id, free_status, models, reference):
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
        reviewer_reference=reference,
        billing_reference=reference,
        terms_reference=reference,
        privacy_reference=reference,
        limits_reference=reference,
        models=[
            ModelQualification(
                model_id=m.model_id,
                model_revision=m.model_revision,
                input_price_per_million=0,
                output_price_per_million=0,
                request_price=0,
                pricing_reference=reference,
                paid_tools_enabled=False,
                live_test_passed=True,
                live_test_at=reviewed,
                live_test_reference=reference,
                zero_charge_verified=True,
                zero_charge_reference=reference,
            )
            for m in models
        ],
    )


def _skip_reason(error):
    """Why a configured provider was not registered.

    AdmissionDenied on a built-in provider means its manual review has aged
    out of the qualification window, which is the expected way this fires.
    """
    if isinstance(error, AdmissionDenied):
        return (
            "provider review is expired or no longer satisfies free-only admission; "
            "re-verify provider terms and pricing, then update the built-in review date"
        )
    if isinstance(error, AuthenticationFailed):
        return "provider adapter refused admission with its configured credential"
    return "provider adapter initialization failed"


# Not every free model accepts response_format, and advertising a capability
# the model does not have sends a request the gateway rejects -- OpenRouter is
# configured with require_parameters, so an unsupported parameter fails the
# call rather than being dropped. Models list their own capabilities when they
# differ from the full text set.
_NO_STRUCTURED_OUTPUT = frozenset(TEXT_CAPABILITIES) - {"structured_output"}


def _text_models(*entries):
    """Each entry is (model_id, context_window) or (model_id, context_window, capabilities)."""
    return [
        ModelDescriptor(
            model_id=entry[0],
            context_window=entry[1],
            capabilities=set(entry[2]) if len(entry) > 2 else set(TEXT_CAPABILITIES),
        )
        for entry in entries
    ]


_FREE_STATUS = {"FREE_RECURRING": "verified_free_plan", "FREE_DYNAMIC": "verified_zero_price_model"}

# These gateways prove zero price at request time: catalog entries must be
# explicitly free/zero-priced and the completion response must report zero
# cost. Free-plan providers whose same API key can belong to a billable
# account are NOT auto-confirmed.
_RUNTIME_ZERO_COST_PROVIDERS = frozenset({"openrouter_free", "kilo_free"})

# Cloud providers: constructor keyword, environment variable, adapter, access class, models.
_CLOUD_PROVIDERS = {
    "google_gemini_api": {
        "kwarg": "gemini_api_key",
        "env": "GEMINI_API_KEY",
        "adapter": GeminiAdapter,
        "access_class": "FREE_RECURRING",
        # Gemini returns no rate-limit headers, so this local ceiling is the
        # only request guard FAIR has. It is a conservative floor, not a claim
        # about the account's real allowance: Google's per-day limit varies by
        # model and tier, and a 429 still exhausts the provider on its own.
        "request_limit": 1500,
        "request_limit_window": "DAILY_PACIFIC",
        "models": _text_models(("gemini-3.5-flash-lite", 1048576), ("gemini-3.6-flash", 1048576)),
    },
    "groq": {
        "kwarg": "groq_api_key",
        "env": "GROQ_API_KEY",
        "adapter": GroqAdapter,
        "access_class": "FREE_RECURRING",
        # Groq reports x-ratelimit-reset-requests, and an observed reset always
        # overrides this window. It only covers the case where no response has
        # carried usable headers yet.
        "request_limit": 1000,
        "request_limit_window": "DAILY_UTC",
        "models": _text_models(("openai/gpt-oss-20b", 131072), ("openai/gpt-oss-120b", 131072)),
    },
    "openrouter_free": {
        "kwarg": "openrouter_api_key",
        "env": "OPENROUTER_API_KEY",
        "adapter": OpenRouterFreeAdapter,
        "access_class": "FREE_DYNAMIC",
        # OpenRouter's free account currently allows 50 requests/day. FAIR
        # counts attempts locally too so several apps can share that allowance
        # when they point at one SharedQuotaLedger.
        "request_limit": 50,
        "request_limit_window": "DAILY_UTC",
        "models": _text_models(
            # Reviewed against OpenRouter on 2026-09-27. Nemotron 3 Ultra is
            # the primary long-context reasoning/agent model but its free
            # endpoint does not accept response_format.
            (
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                1000000,
                _NO_STRUCTURED_OUTPUT,
            ),
            # FAIR sends strict JSON Schema through response_format. Nex-N2.5
            # Mini's free endpoint explicitly supports that contract.
            ("nex-agi/nex-n2.5-mini:free", 262144),
            # Keep a fast coding-specialist fallback. It does not accept
            # response_format, so it must not be selected for schema requests.
            ("cohere/north-mini-code:free", 256000, _NO_STRUCTURED_OUTPUT),
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
            ("nex-agi/nex-n2.5-pro:free", 262144),
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

        fair = FAIR(
            gemini_api_key="...",
            confirmed_free_providers={"google_gemini_api"},
        )
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
        confirmed_free_providers: set[str] | None = None,
        ollama_url: str | None = None,
        ollama_models: list[str] | None = None,
        env_file: str | None = None,
        providers: list[tuple[ProviderSpec, ProviderAdapter]] | None = None,
        quality_level: Literal[
            "commodity", "standard", "advanced", "high_impact_support"
        ] = "standard",
        max_attempts: int = 3,
        max_unanswered_attempts: int = 6,
        max_verification_attempts: int = 2,
        timeout_seconds: float = 15,
        cooldown_seconds: float = 360,
        cache_enabled: bool = True,
        cache_ttl_seconds: int = 3600,
        cache_max_entries: int = 1000,
        cross_check_required: bool = False,
        source_reviews: list[dict] | str | None = None,
        application_id: str | None = None,
        shared_quota_path: str | None = None,
        quota_pool_ids: dict[str, str] | None = None,
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
        application_id = application_id or env.get("FAIR_APPLICATION_ID") or "embedded"
        self._application_id = application_id
        shared_quota_path = shared_quota_path or env.get("FAIR_SHARED_QUOTA_PATH")
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
        ollama_cloud_key = ollama_cloud_api_key or env.get("OLLAMA_CLOUD_API_KEY")
        if ollama_cloud_key and ollama_cloud_key.strip():
            self.skipped["ollama_cloud"] = (
                "credit-priced cloud service is not eligible for FAIR free-only routing"
            )
        nvidia_key = nvidia_api_key or env.get("NVIDIA_API_KEY")
        if nvidia_key and nvidia_key.strip():
            self.skipped["nvidia_nim"] = (
                "hosted preview API uses starter credits and is not eligible for FAIR "
                "recurring-free routing"
            )

        confirmed = set(confirmed_free_providers or ())
        unknown_confirmations = confirmed - set(_CLOUD_PROVIDERS)
        if unknown_confirmations:
            names = ", ".join(sorted(unknown_confirmations))
            raise ValueError(f"Unknown confirmed free provider(s): {names}")
        confirmed |= set(_RUNTIME_ZERO_COST_PROVIDERS)
        live_settings = LiveSettings(
            enabled=True,
            confirmed_providers=confirmed | {"ollama_local"},
        )

        for provider_id, entry in _CLOUD_PROVIDERS.items():
            key = given[entry["kwarg"]] or env.get(entry["env"])
            if not key or not key.strip():
                continue
            if provider_id not in confirmed:
                self.skipped[provider_id] = (
                    "explicit free-tier account confirmation required; "
                    "pass confirmed_free_providers with this provider_id"
                )
                continue
            extra = {}
            if provider_id == "cloudflare_workers_ai":
                if not cloudflare_account_id:
                    self.skipped[provider_id] = "CLOUDFLARE_ACCOUNT_ID missing"
                    continue
                extra["account_id"] = cloudflare_account_id
            try:
                self._register_cloud(provider_id, entry, key.strip(), live_settings, extra)
            except (AdmissionDenied, AuthenticationFailed, CredentialConfigurationError) as error:
                # One provider whose review has expired, or whose adapter will
                # not admit itself, must not deny the application every other
                # provider. Only these three are expected here; a genuine
                # programming error still surfaces.
                self.skipped[provider_id] = _skip_reason(error)

        ollama_url = ollama_url or env.get("OLLAMA_URL") or env.get("OLLAMA_HOST")
        if not ollama_url and env.get("OLLAMA_ENABLED"):
            ollama_url = "http://127.0.0.1:11434"
        if ollama_url:
            self._register_ollama(_loopback(ollama_url), ollama_models, live_settings)

        if providers:
            for spec, adapter in providers:
                self._registry.register(spec, adapter)

        quota_pool_ids = dict(quota_pool_ids or {})
        unknown_quota_pools = set(quota_pool_ids) - set(self._registry.providers)
        if unknown_quota_pools:
            names = ", ".join(sorted(unknown_quota_pools))
            raise ValueError(f"Unknown quota_pool_ids provider(s): {names}")
        quota_ledger = SharedQuotaLedger(shared_quota_path) if shared_quota_path else None

        if not self._registry.adapters:
            raise ValueError(
                "FAIR requires at least one safely eligible provider. "
                "Use a runtime-zero-cost provider (OpenRouter Free or Kilo Free), "
                "explicitly confirm a recurring free-tier account with "
                "confirmed_free_providers, configure local Ollama, or pass a "
                "reviewed custom provider."
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
        reviews = None
        if source_reviews is not None:
            reviews = (
                SourceReviewRegistry.from_file(source_reviews)
                if isinstance(source_reviews, str)
                else SourceReviewRegistry(source_reviews)
            )
        self._router = EmbeddedRouter(
            self._registry,
            settings,
            dict(DEFAULT_THRESHOLDS),
            on_event=on_event,
            source_reviews=reviews,
            quota_ledger=quota_ledger,
            application_id=application_id,
            quota_pool_ids=quota_pool_ids,
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
            request_limit_window=entry.get("request_limit_window"),
            qualification=_make_qualification(
                provider_id,
                _FREE_STATUS[entry["access_class"]],
                models,
                (
                    f"{_review_reference()}:{provider_id}:runtime-zero-cost"
                    if provider_id in _RUNTIME_ZERO_COST_PROVIDERS
                    else f"{_review_reference()}:{provider_id}:operator-confirmed"
                ),
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
            # Inference never leaves the host, so local Ollama is the only
            # route eligible for data above PUBLIC. Remote adapters reject a
            # non-PUBLIC max_data_class outright (providers/live.py:_admit).
            max_data_class="RESTRICTED",
            models=_text_models(*discovered),
        )
        self._registry.register(spec, OllamaLocalAdapter(spec, settings))

    async def solve(
        self,
        task: str,
        *,
        task_type: str | None = None,
        quality_level: str | None = None,
        privacy_class: PrivacyClass = "PUBLIC",
        required_capabilities: set[str] | None = None,
        freshness_required: bool = False,
        expected_schema: dict | None = None,
        validation: dict | None = None,
        evidence: list[dict] | None = None,
        source_policy: dict | None = None,
        cross_check_required: bool | None = None,
        max_output_tokens: int = 1024,
        client_id: str | None = None,
        priority: str = "P2",
        cache_mode: str = "default",
    ) -> SolveResponse:
        """Send a task to free AI models with quality verification.

        ``privacy_class`` bounds which providers may see the task: a provider is
        only eligible when its ``max_data_class`` is at least as permissive.
        Remote adapters are PUBLIC-only by construction, so anything above
        PUBLIC routes to local Ollama or not at all -- an unroutable class
        escalates rather than downgrading to a cloud provider.

        Returns a SolveResponse with:
        - status: "ACCEPTED" (verified answer), "ESCALATION_REQUIRED" (no model passed),
                  or "FAILED" (infrastructure failure)
        - output: the verified answer text (only when ACCEPTED)
        - quality: full QualityReport with scores and verification state
        """
        request = SolveRequest.model_validate(
            {
                "client_id": client_id or self._application_id,
                "task": task,
                "task_type": task_type,
                "quality_level": quality_level or self._quality_level,
                "privacy_class": privacy_class,
                "required_capabilities": required_capabilities or set(),
                "freshness_required": freshness_required,
                "expected_schema": expected_schema,
                "validation": validation,
                "evidence": evidence or [],
                "source_policy": source_policy,
                "cross_check_required": (
                    cross_check_required if cross_check_required is not None else self._cross_check
                ),
                "max_output_tokens": max_output_tokens,
                "priority": priority,
                "cache_mode": cache_mode,
            }
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
                "quota_pool_id": self._router.quota.pool_id(p.provider_id),
                "quota_remaining": self._router.quota.remaining(p),
            }
            for p in self._registry.providers.values()
        ]

    def quota_usage(self) -> dict:
        """Return secret-free shared quota usage grouped by application."""
        return self._router.quota.usage_report(self._registry.providers.values())

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
