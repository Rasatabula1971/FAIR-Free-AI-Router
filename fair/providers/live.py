"""Opt-in text adapters with fixed transports and conservative provider observations."""

import ipaddress
import json
import re
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from time import time
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

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


class LiveSettings(DTO):
    enabled: bool = False
    groq_free_plan_confirmed: bool = False
    openrouter_free_account_confirmed: bool = False
    ollama_local_only_confirmed: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
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


def retry_seconds(value, now):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if 0 < seconds <= 86400 else None


def duration(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", value):
        return None
    seconds = sum(
        float(number) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
        for number, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", value)
    )
    return seconds if 0 < seconds <= 86400 else None


def zero(value):
    try:
        return (
            not isinstance(value, bool)
            and Decimal(str(value)).is_finite()
            and Decimal(str(value)) == 0
        )
    except InvalidOperation:
        return False


class TextAdapter:
    live_inference = True
    base_url = ""
    expected_provider = ""
    expected_access = ""
    catalog_path = "/models"

    def __init__(self, spec, settings, credential=None, transport=None, clock=time):
        self.provider_id, self.spec, self.settings = spec.provider_id, spec, settings
        self._credential, self.clock = credential, clock
        self._quota = QuotaSnapshot(provider_id=self.provider_id)
        self._client = httpx.AsyncClient(
            timeout=30, trust_env=False, follow_redirects=False, transport=transport
        )
        self._admit()

    def _admit(self):
        admit_provider(self.spec)
        if (
            self.provider_id != self.expected_provider
            or self.spec.access_class != self.expected_access
        ):
            raise AuthenticationFailed("ADAPTER_IDENTITY_OR_ACCESS_CLASS_MISMATCH")
        if not self.settings.enabled:
            raise AuthenticationFailed("LIVE_ADAPTERS_DISABLED")
        confirmations = {
            "groq": self.settings.groq_free_plan_confirmed,
            "openrouter_free": self.settings.openrouter_free_account_confirmed,
            "ollama_local": self.settings.ollama_local_only_confirmed,
        }
        if not confirmations.get(self.provider_id) or not self.spec.models:
            raise AuthenticationFailed("LIVE_ACCOUNT_REVIEW_REQUIRED")
        if self.provider_id != "ollama_local":
            if self._credential is None or not self._credential.get_secret_value().strip():
                raise AuthenticationFailed("PROVIDER_CREDENTIAL_REQUIRED")
            reviewed = self.spec.terms_last_verified
            if reviewed is None or reviewed.tzinfo is None:
                raise AuthenticationFailed("CURRENT_TERMS_REVIEW_REQUIRED")
            age = self.clock() - reviewed.timestamp()
            if not 0 <= age <= self.settings.review_max_age_days * 86400:
                raise AuthenticationFailed("CURRENT_TERMS_REVIEW_REQUIRED")
            if self.spec.max_data_class != "PUBLIC":
                raise AuthenticationFailed("REMOTE_ADAPTER_PUBLIC_ONLY")
        if any(
            model.capabilities - {"reasoning", "coding", "structured_output"}
            for model in self.spec.models
        ):
            raise AuthenticationFailed("TEXT_ADAPTER_CAPABILITY_UNSUPPORTED")
        if self.provider_id == "openrouter_free" and any(
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
            raise ProviderUnavailable("PROVIDER_UNAVAILABLE")

    async def _json(self, method, path, payload=None):
        self._admit()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self._credential is not None:
            headers["Authorization"] = "Bearer " + self._credential.get_secret_value()
        try:
            async with self._client.stream(
                method, self.base_url + path, headers=headers, json=payload
            ) as response:
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
                if "error" in value:
                    error = value["error"]
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
        data = await self._json("GET", self.catalog_path)
        entries = data.get("data")
        if not isinstance(entries, list) or len(entries) > 4096:
            raise MalformedResponse("INVALID_MODEL_CATALOG")
        result = []
        for configured in self.spec.models:
            matches = [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("id") == configured.model_id
            ]
            if len(matches) != 1 or not configured.active:
                continue
            entry = matches[0]
            if entry.get("active", True) is not True:
                continue
            if self.provider_id == "openrouter_free":
                prices = entry.get("pricing", {})
                if (
                    not isinstance(prices, dict)
                    or not {"prompt", "completion", "request"} <= prices.keys()
                    or not all(zero(value) for value in prices.values())
                ):
                    continue
            context = entry.get(
                "context_window" if self.provider_id == "groq" else "context_length"
            )
            if type(context) is not int or context < configured.context_window:
                continue
            result.append(configured.model_copy(deep=True))
        return result

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
            "max_completion_tokens"
            if self.provider_id == "groq"
            else "max_tokens": request.max_output_tokens,
        }
        if request.expected_json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "fair_result",
                    "strict": True,
                    "schema": request.expected_json_schema,
                },
            }
        if self.provider_id == "openrouter_free":
            account = await self._json("GET", "/key")
            if (
                not isinstance(account.get("data"), dict)
                or account["data"].get("is_free_tier") is not True
            ):
                raise AuthenticationFailed("FREE_ACCOUNT_NOT_CONFIRMED")
            payload["provider"] = {
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
            }
        if len(json.dumps(payload).encode()) + request.max_output_tokens > model.context_window:
            raise MalformedResponse("CONTEXT_BUDGET_EXCEEDED")
        data = await self._json("POST", "/chat/completions", payload)
        if self.provider_id == "openrouter_free":
            usage = data.get("usage")
            if not isinstance(usage, dict) or not zero(usage.get("cost")):
                raise BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED")
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


class OllamaLocalAdapter(TextAdapter):
    expected_provider = "ollama_local"
    expected_access = "FREE_LOCAL"

    def __init__(self, spec, settings, **kwargs):
        self.base_url = settings.ollama_url.rstrip("/")
        super().__init__(spec, settings, **kwargs)

    async def list_models(self):
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


def register_live(registry, specs, settings, transport=None):
    credentials = ProviderCredentials(
        {"groq": "GROQ_API_KEY", "openrouter_free": "OPENROUTER_API_KEY"}
    )
    for spec in specs:
        if not settings.enabled or spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            registry.register(spec)
        elif spec.provider_id == "ollama_local":
            registry.register(spec, OllamaLocalAdapter(spec, settings, transport=transport))
        elif spec.provider_id in {"groq", "openrouter_free"}:
            adapter = GroqAdapter if spec.provider_id == "groq" else OpenRouterFreeAdapter
            registry.register_credentialed(
                spec,
                lambda secret: adapter(spec, settings, credential=secret, transport=transport),
                credentials,
            )
        else:
            raise ValueError("Active provider has no reviewed live adapter")
