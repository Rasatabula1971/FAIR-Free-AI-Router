"""Opt-in text adapters with fixed transports and conservative provider observations."""

import copy
import ipaddress
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from time import time
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from fair.constants import SECONDS_IN_DAY
from fair.governor.policy import admit_provider
from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.quality.json_data import strict_json
from fair.schemas.domain import DTO, NormalizedModelResponse, ProviderHealth, QuotaSnapshot
from fair.security.credentials import ProviderCredentials

TEXT_CAPABILITIES = {"reasoning", "coding", "structured_output"}


class LiveSettings(DTO):
    enabled: bool = False
    groq_free_plan_confirmed: bool = False
    gemini_free_tier_confirmed: bool = False
    gemini_max_output_tokens: int = Field(default=65536, ge=1, le=65536, strict=True)
    openrouter_free_account_confirmed: bool = False
    ollama_local_only_confirmed: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    confirmed_providers: set[str] = Field(default_factory=set)
    review_max_age_days: int = Field(default=30, ge=1, le=30)

    @model_validator(mode="after")
    def local_endpoint(self):
        try:
            url = urlsplit(self.ollama_url)
            valid = (
                url.scheme == "http"
                and ipaddress.ip_address(url.hostname).is_loopback
                and not url.username
                and not url.password
                and url.path in {"", "/"}
                and not url.query
                and not url.fragment
                and (url.port is None or 1 <= url.port <= 65535)
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Ollama requires a literal loopback HTTP endpoint")
        return self

    def confirmed(self, provider_id):
        legacy = {
            "groq": self.groq_free_plan_confirmed,
            "google_gemini_api": self.gemini_free_tier_confirmed,
            "openrouter_free": self.openrouter_free_account_confirmed,
            "ollama_local": self.ollama_local_only_confirmed,
        }
        return provider_id in self.confirmed_providers or bool(legacy.get(provider_id))


def retry_seconds(value, now):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if 0 < seconds <= SECONDS_IN_DAY else None


def duration(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", value):
        return None
    seconds = sum(
        float(number) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
        for number, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", value)
    )
    return seconds if 0 < seconds <= SECONDS_IN_DAY else None


def zero(value):
    try:
        return (
            not isinstance(value, bool)
            and Decimal(str(value)).is_finite()
            and Decimal(str(value)) == 0
        )
    except InvalidOperation:
        return False


def zero_priced(entry):
    prices = entry.get("pricing")
    return (
        isinstance(prices, dict)
        and {"prompt", "completion"} <= prices.keys()
        and all(zero(value) for value in prices.values())
    )


class TextAdapter:
    """OpenAI-style chat adapter; subclasses describe one provider's catalog and payload shape."""

    live_inference = True
    base_url = ""
    expected_provider = ""
    expected_access = ""
    remote = True
    catalog_path: str | None = "/models"
    catalog_key = "data"
    catalog_id_key = "id"
    context_field: str | None = "context_length"
    zero_price_models = False
    account_check_path: str | None = None
    provider_preferences: dict[str, object] | None = None
    output_tokens_key = "max_tokens"
    extra_payload: dict[str, object] = {}
    credential_header = "Authorization"
    credential_prefix = "Bearer "

    _catalog_ttl = 60

    def __init__(self, spec, settings, credential=None, transport=None, clock=time):
        self.provider_id, self.spec, self.settings = spec.provider_id, spec, settings
        self._credential, self.clock = credential, clock
        self._quota = QuotaSnapshot(provider_id=self.provider_id)
        self._model_cache = None
        self._model_cache_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=25, write=5, pool=5),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._admit()

    def _admit(self):
        admit_provider(self.spec, now=datetime.fromtimestamp(self.clock(), UTC))
        if (
            self.provider_id != self.expected_provider
            or self.spec.access_class != self.expected_access
        ):
            raise AuthenticationFailed("ADAPTER_IDENTITY_OR_ACCESS_CLASS_MISMATCH")
        if not self.settings.enabled:
            raise AuthenticationFailed("LIVE_ADAPTERS_DISABLED")
        if not self.settings.confirmed(self.provider_id) or not self.spec.models:
            raise AuthenticationFailed("LIVE_ACCOUNT_REVIEW_REQUIRED")
        if self.remote:
            if self._credential is None or not self._credential.get_secret_value().strip():
                raise AuthenticationFailed("PROVIDER_CREDENTIAL_REQUIRED")
            reviewed = self.spec.terms_last_verified
            if reviewed is None or reviewed.tzinfo is None:
                raise AuthenticationFailed("CURRENT_TERMS_REVIEW_REQUIRED")
            age = self.clock() - reviewed.timestamp()
            if not 0 <= age <= self.settings.review_max_age_days * SECONDS_IN_DAY:
                raise AuthenticationFailed("CURRENT_TERMS_REVIEW_REQUIRED")
            if self.spec.max_data_class != "PUBLIC":
                raise AuthenticationFailed("REMOTE_ADAPTER_PUBLIC_ONLY")
        if any(model.capabilities - TEXT_CAPABILITIES for model in self.spec.models):
            raise AuthenticationFailed("TEXT_ADAPTER_CAPABILITY_UNSUPPORTED")
        if self.zero_price_models and any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:free", model.model_id)
            or model.model_id.startswith("openrouter/")
            for model in self.spec.models
        ):
            raise AuthenticationFailed("EXPLICIT_FREE_MODEL_REQUIRED")
        if self.provider_id == "groq" and any(
            model.model_id.startswith("groq/compound") for model in self.spec.models
        ):
            raise AuthenticationFailed("SERVER_TOOLS_NOT_SUPPORTED")

    def check_admission(self):
        """Recheck local policy without consuming provider quota."""
        self._admit()

    async def close(self):
        await self._client.aclose()

    def _observe(self, headers):
        return QuotaSnapshot(provider_id=self.provider_id)

    def _error(self, status, headers):
        if status in {401, 403} or 300 <= status < 400:
            raise AuthenticationFailed("PROVIDER_AUTHENTICATION_OR_REDIRECT_BLOCKED")
        if status == 402:
            raise QuotaExceeded("FREE_ACCESS_UNAVAILABLE")
        if status == 429:
            observation = self._observe(headers)
            self._quota = observation
            if observation.quota_remaining_estimate == 0:
                raise QuotaExceeded("REQUEST_QUOTA_EXHAUSTED", reset_at=observation.reset_at)
            raise RateLimited(
                "RATE_LIMITED", retry_after=retry_seconds(headers.get("retry-after"), self.clock())
            )
        if status != 200:
            # The status code is FAIR's own observation, not upstream text, so
            # it is safe to surface; it is the difference between "overloaded
            # (503)", "bad request (400)" and "gone (404)" in the attempt log.
            raise ProviderUnavailable(f"HTTP_{status}")

    async def _json(self, method, path, payload=None):
        self._admit()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self._credential is not None:
            headers[self.credential_header] = (
                self.credential_prefix + self._credential.get_secret_value()
            )
        url = path if path.startswith("https://") else self.base_url + path
        try:
            async with self._client.stream(method, url, headers=headers, json=payload) as response:
                self._error(response.status_code, response.headers)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise MalformedResponse("COMPRESSED_PROVIDER_RESPONSE_UNSUPPORTED")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 4_000_000:
                        raise MalformedResponse("PROVIDER_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                value = strict_json(b"".join(chunks).decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError()
                if "error" in value or value.get("success") is False:
                    error = value.get("error")
                    self._error(
                        error.get("code", 500)
                        if isinstance(error, dict) and type(error.get("code")) is int
                        else 500,
                        response.headers,
                    )
                    raise MalformedResponse("PROVIDER_ERROR_ENVELOPE")
                self._quota = self._observe(response.headers)
                return value
        except httpx.HTTPError:
            raise ProviderUnavailable("PROVIDER_TRANSPORT_FAILED") from None
        except (ValueError, UnicodeError, RecursionError):
            raise MalformedResponse("MALFORMED_PROVIDER_RESPONSE") from None

    async def health(self):
        models = await self.list_models()
        return ProviderHealth(
            provider_id=self.provider_id,
            state="ACTIVE" if models else "DISABLED",
            source="OBSERVED",
        )

    async def quota(self):
        return self._quota.model_copy(deep=True)

    async def list_models(self):
        if self.catalog_path is None:
            return [m.model_copy(deep=True) for m in self.spec.models if m.active]
        now = self.clock()
        if self._model_cache is not None and now - self._model_cache_at < self._catalog_ttl:
            return [m.model_copy(deep=True) for m in self._model_cache]
        result = await self._fetch_models()
        self._model_cache = result
        self._model_cache_at = now
        return result

    def _catalog_entries(self, data):
        entries = data.get(self.catalog_key)
        if not isinstance(entries, list) or len(entries) > 4096:
            raise MalformedResponse("INVALID_MODEL_CATALOG")
        return entries

    def _catalog_context(self, entry):
        return None if self.context_field is None else entry.get(self.context_field)

    async def _fetch_models(self):
        entries = self._catalog_entries(await self._json("GET", self.catalog_path))
        result = []
        for configured in self.spec.models:
            matches = [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get(self.catalog_id_key) == configured.model_id
            ]
            if len(matches) != 1 or not configured.active:
                continue
            entry = matches[0]
            if entry.get("active", True) is not True:
                continue
            if self.zero_price_models and not zero_priced(entry):
                continue
            if self.context_field is not None:
                context = self._catalog_context(entry)
                if type(context) is not int or context < configured.context_window:
                    continue
            result.append(configured.model_copy(deep=True))
        return result

    async def _before_completion(self, payload):
        if self.account_check_path is not None:
            account = await self._json("GET", self.account_check_path)
            if (
                not isinstance(account.get("data"), dict)
                or account["data"].get("is_free_tier") is not True
            ):
                raise AuthenticationFailed("FREE_ACCOUNT_NOT_CONFIRMED")
        if self.provider_preferences is not None:
            payload["provider"] = copy.deepcopy(self.provider_preferences)

    def _after_completion(self, data):
        if self.zero_price_models:
            usage = data.get("usage")
            if not isinstance(usage, dict) or not zero(usage.get("cost")):
                raise BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED")

    async def complete(self, request):
        models = await self.list_models()
        model = next((item for item in models if item.model_id == request.model_id), None)
        if model is None:
            raise AuthenticationFailed("REVIEWED_MODEL_UNAVAILABLE_OR_PRICING_CHANGED")
        if not 1 <= request.max_output_tokens <= 4096:
            raise MalformedResponse("OUTPUT_BUDGET_INVALID")
        if len(request.task.encode()) + request.max_output_tokens > model.context_window:
            raise MalformedResponse("CONTEXT_BUDGET_EXCEEDED")
        payload = {
            "model": model.model_id,
            "messages": [{"role": "user", "content": request.task}],
            "stream": False,
            self.output_tokens_key: request.max_output_tokens,
        }
        payload.update(copy.deepcopy(self.extra_payload))
        if request.expected_json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "fair_result",
                    "strict": True,
                    "schema": request.expected_json_schema,
                },
            }
        await self._before_completion(payload)
        if len(json.dumps(payload).encode()) + request.max_output_tokens > model.context_window:
            raise MalformedResponse("CONTEXT_BUDGET_EXCEEDED")
        data = await self._json("POST", "/chat/completions", payload)
        self._after_completion(data)
        try:
            choices = data["choices"]
            if (
                data["model"] != model.model_id
                or not isinstance(choices, list)
                or len(choices) != 1
            ):
                raise ValueError()
            choice = choices[0]
            message = choice["message"]
            if (
                message.get("role") != "assistant"
                or message.get("tool_calls")
                or message.get("function_call")
            ):
                raise ValueError()
            content = message["content"]
            if (
                not isinstance(content, str)
                or not content.strip()
                or choice["finish_reason"] not in {"stop", "length"}
            ):
                raise ValueError()
            return NormalizedModelResponse(
                provider_id=self.provider_id,
                model_id=model.model_id,
                text=content,
                finish_reason=choice["finish_reason"],
                quota=self._quota,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MalformedResponse("INVALID_CHAT_COMPLETION") from None


class GroqAdapter(TextAdapter):
    base_url = "https://api.groq.com/openai/v1"
    expected_provider = "groq"
    expected_access = "FREE_RECURRING"
    context_field = "context_window"
    output_tokens_key = "max_completion_tokens"

    def _observe(self, headers):
        try:
            limit = int(headers["x-ratelimit-limit-requests"])
            remaining = int(headers["x-ratelimit-remaining-requests"])
            if not 0 <= remaining <= limit or limit <= 0:
                raise ValueError()
            reset = duration(headers.get("x-ratelimit-reset-requests"))
            return QuotaSnapshot(
                provider_id=self.provider_id,
                quota_limit=limit,
                quota_remaining_estimate=remaining,
                reset_at=self.clock() + reset if reset else None,
            )
        except (ValueError, KeyError):
            return QuotaSnapshot(provider_id=self.provider_id)


class OpenRouterFreeAdapter(TextAdapter):
    base_url = "https://openrouter.ai/api/v1"
    expected_provider = "openrouter_free"
    expected_access = "FREE_DYNAMIC"
    zero_price_models = True
    account_check_path = "/key"
    provider_preferences = {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
    }


class KiloFreeAdapter(TextAdapter):
    """Kilo Gateway: only current ':free' models with zero catalog pricing pass."""

    base_url = "https://api.kilo.ai/api/gateway"
    expected_provider = "kilo_free"
    expected_access = "FREE_DYNAMIC"
    zero_price_models = True

    def _after_completion(self, data):
        usage = data.get("usage")
        if not isinstance(usage, dict) or not zero(usage.get("cost_microdollars")):
            raise BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED")


class MistralAdapter(TextAdapter):
    base_url = "https://api.mistral.ai/v1"
    expected_provider = "mistral"
    expected_access = "FREE_RECURRING"
    context_field = "max_context_length"

    def _observe(self, headers):
        try:
            limit = int(headers["x-ratelimit-limit-req-minute"])
            remaining = int(headers["x-ratelimit-remaining-req-minute"])
            if not 0 <= remaining <= limit or limit <= 0:
                raise ValueError()
            return QuotaSnapshot(
                provider_id=self.provider_id,
                quota_limit=limit,
                quota_remaining_estimate=remaining,
                reset_at=self.clock() + 60,
            )
        except (ValueError, KeyError):
            return QuotaSnapshot(provider_id=self.provider_id)


class ZaiFreeAdapter(TextAdapter):
    """Z.ai flash models are zero-priced but absent from /models; the completion echo verifies them."""

    base_url = "https://api.z.ai/api/paas/v4"
    expected_provider = "zai_free"
    expected_access = "FREE_DYNAMIC"
    catalog_path = None
    extra_payload = {"thinking": {"type": "disabled"}}

    def _admit(self):
        super()._admit()
        if any(not model.model_id.endswith("-flash") for model in self.spec.models):
            raise AuthenticationFailed("EXPLICIT_FREE_MODEL_REQUIRED")


class NvidiaNimAdapter(TextAdapter):
    base_url = "https://integrate.api.nvidia.com/v1"
    expected_provider = "nvidia_nim"
    expected_access = "FREE_RECURRING"
    context_field = None


class OllamaCloudAdapter(TextAdapter):
    base_url = "https://ollama.com/v1"
    expected_provider = "ollama_cloud"
    expected_access = "FREE_RECURRING"
    context_field = None


class CloudflareWorkersAiAdapter(TextAdapter):
    """Workers AI is free only inside the daily neuron allocation, which the adapter meters itself."""

    expected_provider = "cloudflare_workers_ai"
    expected_access = "FREE_RECURRING"
    catalog_key = "result"
    catalog_id_key = "name"
    context_field = "context_window"
    daily_neurons = 10_000

    def __init__(self, spec, settings, account_id, **kwargs):
        if not isinstance(account_id, str) or not re.fullmatch(r"[0-9a-f]{32}", account_id):
            raise AuthenticationFailed("CLOUDFLARE_ACCOUNT_ID_REQUIRED")
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
        self.catalog_path = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/models/search?task=Text%20Generation&per_page=100"
        )
        self._neurons_used = 0.0
        self._neuron_day = None
        super().__init__(spec, settings, **kwargs)

    def _catalog_context(self, entry):
        properties = entry.get("properties")
        if not isinstance(properties, list):
            return None
        for item in properties:
            if isinstance(item, dict) and item.get("property_id") == "context_window":
                try:
                    return int(item.get("value"))
                except (TypeError, ValueError):
                    return None
        return None

    def _day(self):
        return int(self.clock() // SECONDS_IN_DAY)

    def _reset_at(self):
        return (self._day() + 1) * SECONDS_IN_DAY

    def _observe(self, headers):
        if self._neuron_day != self._day():
            self._neuron_day, self._neurons_used = self._day(), 0.0
        remaining = max(self.daily_neurons - self._neurons_used, 0)
        return QuotaSnapshot(
            provider_id=self.provider_id,
            quota_limit=self.daily_neurons,
            quota_remaining_estimate=int(remaining),
            reset_at=self._reset_at(),
        )

    async def _before_completion(self, payload):
        if self._observe({}).quota_remaining_estimate <= 0:
            raise QuotaExceeded("DAILY_NEURON_ALLOCATION_SPENT", reset_at=self._reset_at())

    def _after_completion(self, data):
        usage = data.get("usage")
        neurons = usage.get("neurons") if isinstance(usage, dict) else None
        if isinstance(neurons, bool) or not isinstance(neurons, (int, float)) or neurons < 0:
            raise BillingViolation("NEURON_USAGE_NOT_REPORTED")
        self._neurons_used += neurons
        self._quota = self._observe({})


class GeminiAdapter(TextAdapter):
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    expected_provider = "google_gemini_api"
    expected_access = "FREE_RECURRING"
    credential_header = "x-goog-api-key"
    credential_prefix = ""

    def _admit(self):
        super()._admit()
        if any(
            not re.fullmatch(r"gemini-[A-Za-z0-9][A-Za-z0-9._-]{0,120}", model.model_id)
            for model in self.spec.models
        ):
            raise AuthenticationFailed("EXPLICIT_GEMINI_MODEL_REQUIRED")

    async def _metadata(self, model):
        # Fetch only exact reviewed names; listing pagination cannot approve new models.
        data = await self._json("GET", "/models/" + model.model_id)
        methods = data.get("supportedGenerationMethods")
        if (
            data.get("name") != "models/" + model.model_id
            or not isinstance(methods, list)
            or "generateContent" not in methods
            or type(data.get("inputTokenLimit")) is not int
            or data["inputTokenLimit"] < model.context_window
            or type(data.get("outputTokenLimit")) is not int
            or data["outputTokenLimit"] <= 0
            or (model.model_revision is not None and data.get("version") != model.model_revision)
        ):
            return None
        return data

    async def list_models(self):
        result = []
        for model in self.spec.models:
            if model.active and await self._metadata(model) is not None:
                result.append(model.model_copy(deep=True))
        return result

    async def complete(self, request):
        model = next(
            (m for m in self.spec.models if m.active and m.model_id == request.model_id), None
        )
        if model is None:
            raise AuthenticationFailed("REVIEWED_GEMINI_MODEL_REQUIRED")
        if not 1 <= request.max_output_tokens <= self.settings.gemini_max_output_tokens:
            raise MalformedResponse("OUTPUT_BUDGET_INVALID")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request.task}]}],
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": request.max_output_tokens,
            },
        }
        if request.expected_json_schema is not None:
            payload["generationConfig"].update(
                responseMimeType="application/json", responseJsonSchema=request.expected_json_schema
            )
        # Gemini publishes separate input and output limits; the output allowance does not
        # consume the configured input capacity. Byte counting remains conservative.
        if len(json.dumps(payload).encode()) > model.context_window:
            raise MalformedResponse("CONTEXT_BUDGET_EXCEEDED")
        metadata = await self._metadata(model)
        if metadata is None:
            raise AuthenticationFailed("REVIEWED_GEMINI_MODEL_UNAVAILABLE_OR_CHANGED")
        if request.max_output_tokens > metadata["outputTokenLimit"]:
            raise MalformedResponse("OUTPUT_BUDGET_EXCEEDS_MODEL_LIMIT")
        data = await self._json("POST", "/models/" + model.model_id + ":generateContent", payload)
        try:
            candidates = data["candidates"]
            if (
                data["modelVersion"] != model.model_id
                or not isinstance(candidates, list)
                or len(candidates) != 1
                or (data.get("promptFeedback") or {}).get("blockReason")
            ):
                raise ValueError()
            candidate = candidates[0]
            content = candidate["content"]
            if content.get("role") != "model" or candidate["finishReason"] not in {
                "STOP",
                "MAX_TOKENS",
            }:
                raise ValueError()
            parts = content["parts"]
            if not isinstance(parts, list) or not 1 <= len(parts) <= 4096:
                raise ValueError()
            texts = []
            for part in parts:
                if (
                    not isinstance(part, dict)
                    or part.keys() - {"text", "thought", "thoughtSignature"}
                    or not isinstance(part.get("text"), str)
                    or type(part.get("thought", False)) is not bool
                ):
                    raise ValueError()
                if not part.get("thought", False):
                    texts.append(part["text"])
            text = "".join(texts)
            if not text.strip():
                raise ValueError()
            return NormalizedModelResponse(
                provider_id=self.provider_id,
                model_id=data["modelVersion"],
                text=text,
                finish_reason="stop" if candidate["finishReason"] == "STOP" else "length",
                quota=self._quota,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MalformedResponse("INVALID_GEMINI_COMPLETION") from None


class OllamaLocalAdapter(TextAdapter):
    expected_provider = "ollama_local"
    expected_access = "FREE_LOCAL"
    remote = False

    def __init__(self, spec, settings, **kwargs):
        self.base_url = settings.ollama_url.rstrip("/")
        super().__init__(spec, settings, **kwargs)

    async def _fetch_models(self):
        data = await self._json("GET", "/api/tags")
        entries = data.get("models")
        if not isinstance(entries, list) or len(entries) > 4096:
            raise MalformedResponse("INVALID_LOCAL_CATALOG")
        result = []
        for configured in self.spec.models:
            if not configured.active or "cloud" in configured.model_id.casefold():
                continue
            matches = [
                item
                for item in entries
                if isinstance(item, dict) and item.get("name") == configured.model_id
            ]
            if len(matches) != 1:
                continue
            entry = matches[0]
            if (
                entry.get("remote_model")
                or entry.get("remote_host")
                or not isinstance(entry.get("details"), dict)
                or entry.get("details", {}).get("format") != "gguf"
            ):
                continue
            if type(entry.get("size")) is not int or entry["size"] <= 0:
                continue
            if (
                configured.model_revision is not None
                and entry.get("digest") != configured.model_revision
            ):
                continue
            result.append(configured.model_copy(deep=True))
        return result

    async def complete(self, request):
        model = next(
            (item for item in await self.list_models() if item.model_id == request.model_id), None
        )
        if model is None:
            raise AuthenticationFailed("LOCAL_REVIEWED_MODEL_REQUIRED")
        info = await self._json("POST", "/api/show", {"model": model.model_id})
        if (
            info.get("remote_model")
            or info.get("remote_host")
            or not isinstance(info.get("details"), dict)
            or info.get("details", {}).get("format") != "gguf"
            or not isinstance(info.get("capabilities"), list)
            or "completion" not in info.get("capabilities", [])
            or not isinstance(info.get("model_info"), dict)
        ):
            raise AuthenticationFailed("LOCAL_COMPLETION_WEIGHTS_REQUIRED")
        metadata = info["model_info"]
        context = metadata.get(str(metadata.get("general.architecture")) + ".context_length")
        if type(context) is not int or context < model.context_window:
            raise AuthenticationFailed("LOCAL_CONTEXT_CAPACITY_NOT_CONFIRMED")
        if (
            not 1 <= request.max_output_tokens <= 4096
            or len(request.task.encode()) + request.max_output_tokens > model.context_window
        ):
            raise MalformedResponse("CONTEXT_OR_OUTPUT_BUDGET_EXCEEDED")
        payload = {
            "model": model.model_id,
            "stream": False,
            "messages": [{"role": "user", "content": request.task}],
            "options": {"num_predict": request.max_output_tokens, "num_ctx": model.context_window},
            "keep_alive": 0,
        }
        if request.expected_json_schema is not None:
            payload["format"] = request.expected_json_schema
        if len(json.dumps(payload).encode()) + request.max_output_tokens > model.context_window:
            raise MalformedResponse("CONTEXT_BUDGET_EXCEEDED")
        data = await self._json("POST", "/api/chat", payload)
        try:
            message = data["message"]
            if (
                data["model"] != model.model_id
                or data.get("done") is not True
                or message.get("role") != "assistant"
                or message.get("tool_calls")
            ):
                raise ValueError()
            if (
                not isinstance(message["content"], str)
                or not message["content"].strip()
                or data.get("done_reason") not in {"stop", "length"}
            ):
                raise ValueError()
            return NormalizedModelResponse(
                provider_id=self.provider_id,
                model_id=model.model_id,
                text=message["content"],
                finish_reason=data["done_reason"],
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MalformedResponse("INVALID_LOCAL_COMPLETION") from None


LIVE_CREDENTIAL_BINDINGS = {
    "groq": "GROQ_API_KEY",
    "openrouter_free": "OPENROUTER_API_KEY",
    "google_gemini_api": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "kilo_free": "KILO_API_KEY",
    "zai_free": "ZAI_API_KEY",
    "nvidia_nim": "NVIDIA_API_KEY",
    "ollama_cloud": "OLLAMA_CLOUD_API_KEY",
}


def register_live(registry, specs, settings, transport=None, *, credentials=None):
    if credentials is None:
        credentials = ProviderCredentials(LIVE_CREDENTIAL_BINDINGS)
    cloud_adapters = {
        "groq": GroqAdapter,
        "openrouter_free": OpenRouterFreeAdapter,
        "google_gemini_api": GeminiAdapter,
        "mistral": MistralAdapter,
        "kilo_free": KiloFreeAdapter,
        "zai_free": ZaiFreeAdapter,
        "nvidia_nim": NvidiaNimAdapter,
        "ollama_cloud": OllamaCloudAdapter,
    }
    for spec in specs:
        if not settings.enabled or spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            registry.register(spec)
        elif spec.provider_id == "ollama_local":
            registry.register(spec, OllamaLocalAdapter(spec, settings, transport=transport))
        elif spec.provider_id in cloud_adapters:
            adapter = cloud_adapters[spec.provider_id]
            registry.register_credentialed(
                spec,
                lambda secret: adapter(spec, settings, credential=secret, transport=transport),
                credentials,
            )
        else:
            raise ValueError("Active provider has no reviewed live adapter")
