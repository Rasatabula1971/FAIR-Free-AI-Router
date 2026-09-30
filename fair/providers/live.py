"""Opt-in text adapters with fixed transports and conservative provider observations."""

import copy
import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from time import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field, model_validator

from fair.constants import SECONDS_IN_DAY, completion_deadline
from fair.governor.policy import admit_provider
from fair.providers.base import (
    AccessDenied,
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ModelUnavailable,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
    RequestNotSupported,
)
from fair.providers.schema_dialects import (
    GEMINI,
    JSON_SCHEMA,
    OPENAI_STRICT,
    constraint_notes,
    transport_schema,
)
from fair.quality.json_data import strict_json
from fair.schemas.domain import DTO, NormalizedModelResponse, ProviderHealth, QuotaSnapshot
from fair.security.credentials import ProviderCredentials

TEXT_CAPABILITIES = {"reasoning", "coding", "structured_output"}
# The largest completion budget a TextAdapter will send, whatever a descriptor says.
# A model may declare less -- from its review, or from the limit its provider
# publishes in the live catalog -- and the smaller of the two governs.
MAX_OUTPUT_TOKENS = 4096


class LiveSettings(DTO):
    enabled: bool = False
    groq_free_plan_confirmed: bool = False
    gemini_free_tier_confirmed: bool = False
    gemini_max_output_tokens: int = Field(default=65536, ge=1, le=65536, strict=True)
    openrouter_free_account_confirmed: bool = False
    ollama_local_only_confirmed: bool = False
    # Operator-only. Keeps the last non-200 provider message, bounded and scrubbed,
    # readable through safe_diagnostics(). Off by default: an upstream body is
    # provider text, and HTTP_400 alone leaves an operator nothing to act on.
    provider_error_diagnostics: bool = False
    # Idle budget between two reads. On a streaming completion this is the stall
    # detector -- tokens arriving keep resetting it -- so the overall budget below
    # can be generous without letting a dead generation hang.
    read_timeout_seconds: float = Field(default=25, gt=0, le=300)
    # Assumed free-tier throughput and hard ceiling, sizing the budget for a
    # buffered completion exactly as RoutingSettings sizes the attempt around it.
    output_tokens_per_second: float = Field(default=30, gt=0, le=10000)
    max_completion_seconds: float = Field(default=600, gt=0, le=3600)
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
    # Where this provider publishes its own completion-token ceiling, when it does.
    # None keeps the reviewed descriptor value untouched, which is what every
    # provider did before the field existed.
    output_field: str | None = None
    zero_price_models = False
    account_check_path: str | None = None
    provider_preferences: dict[str, object] | None = None
    output_tokens_key = "max_tokens"
    extra_payload: dict[str, object] = {}
    credential_header = "Authorization"
    credential_prefix = "Bearer "
    inspect_error_body = False
    # Which structured-output dialect this provider's API speaks. The caller's schema
    # is reduced to that dialect before dispatch; see fair.providers.schema_dialects.
    schema_dialect = OPENAI_STRICT
    # A buffered completion sends nothing until the last token is written, so the
    # whole generation has to fit one read. Streaming turns that into one read per
    # chunk, which is what lets a large budget be waited for without also waiting
    # that long on a provider that has stopped responding.
    supports_streaming = True

    _catalog_ttl = 60
    # A failed catalog is held no longer than a successful one is held fresh.
    _catalog_error_ttl = 60
    # An upstream error body is provider text, so only a bounded, credential-scrubbed
    # message is kept, only when an operator turns diagnostics on, and only through
    # safe_diagnostics(). No raised code and no attempt log changes either way.
    _diagnostic_max_chars = 512
    _diagnostic_parse_limit = 20_000

    def __init__(self, spec, settings, credential=None, transport=None, clock=time):
        self.provider_id, self.spec, self.settings = spec.provider_id, spec, settings
        self._credential, self.clock = credential, clock
        self._quota = QuotaSnapshot(provider_id=self.provider_id)
        self._model_cache = None
        self._model_cache_at = 0.0
        self._last_error: dict[str, str] | None = None
        self._catalog_error: Exception | None = None
        self._catalog_error_at = 0.0
        self._catalog_drops: dict[str, str] = {}
        self._client = httpx.AsyncClient(
            timeout=self._timeout(settings.read_timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._admit()

    @staticmethod
    def _timeout(read):
        return httpx.Timeout(connect=5, read=read, write=5, pool=5)

    def _completion_timeout(self, request, streaming):
        """How long one read may block: per chunk when streaming, else the whole answer."""
        if streaming:
            return self._timeout(self.settings.read_timeout_seconds)
        return self._timeout(
            completion_deadline(
                request.max_output_tokens,
                base_seconds=self.settings.read_timeout_seconds,
                tokens_per_second=self.settings.output_tokens_per_second,
                ceiling_seconds=self.settings.max_completion_seconds,
            )
        )

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
        if status == 403:
            raise AccessDenied("PROVIDER_ACCESS_DENIED")
        if status == 401 or 300 <= status < 400:
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

    def _error_from_body(self, status, headers, data):
        """Provider-specific structured error hook; must raise for non-200 responses."""
        self._error(status, headers)

    @staticmethod
    def _error_message(data):
        """The human-readable message out of the error envelopes these APIs use."""
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return ""
        error = data.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            for key in ("message", "detail", "description"):
                if isinstance(error.get(key), str):
                    return error[key]
        entries = data.get("errors")
        if isinstance(entries, list):
            messages = [
                item["message"]
                for item in entries
                if isinstance(item, dict) and isinstance(item.get("message"), str)
            ]
            if messages:
                return "; ".join(messages)
        for key in ("message", "detail", "error_description"):
            if isinstance(data.get(key), str):
                return data[key]
        return ""

    def _record_error_diagnostic(self, status, raw):
        text = raw.decode("utf-8", errors="replace")[: self._diagnostic_parse_limit]
        try:
            message = self._error_message(json.loads(text)) or text
        except ValueError:
            message = text
        if self._credential is not None:
            secret = self._credential.get_secret_value()
            if secret:
                message = message.replace(secret, "[redacted]")
        message = " ".join(message.split())
        self._last_error = {
            "status": f"HTTP_{status}",
            "provider_message": message[: self._diagnostic_max_chars] or "EMPTY_ERROR_BODY",
        }

    def safe_diagnostics(self):
        """Fixed, secret-scanned operator diagnostics; empty unless something was recorded."""
        record = {}
        if self._last_error:
            record["last_provider_error"] = dict(self._last_error)
        if self._catalog_drops:
            record["catalog_drops"] = dict(self._catalog_drops)
        return record

    async def _stream_json(self, path, payload, timeout):
        """Read an SSE completion and assemble the envelope a buffered one would have.

        Every check downstream -- the zero-cost observation above all -- then runs on
        the same shape it has always run on. A stream that carries no usage assembles
        without usage and is refused by _after_completion exactly as a buffered
        response without usage is refused today. Streaming changes how the bytes
        arrive, never what has to be proved about them.
        """
        chunks = await self._stream_chunks(path, payload, timeout)
        model = content = finish = usage = None
        role = "assistant"
        for chunk in chunks:
            if not isinstance(chunk, dict):
                raise MalformedResponse("INVALID_COMPLETION_STREAM")
            if isinstance(chunk.get("model"), str):
                # Every chunk has to name the same model. Letting the last one win
                # would accept a stream whose body came from somewhere else as long
                # as its final chunk carried the right name.
                if model is not None and chunk["model"] != model:
                    raise MalformedResponse("INCONSISTENT_COMPLETION_STREAM")
                model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not choices:
                continue
            if not isinstance(choices, list) or len(choices) != 1:
                raise MalformedResponse("INVALID_COMPLETION_STREAM")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise MalformedResponse("INVALID_COMPLETION_STREAM")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("function_call"):
                raise MalformedResponse("INVALID_COMPLETION_STREAM")
            if isinstance(delta.get("role"), str):
                role = delta["role"]
            piece = delta.get("content")
            if isinstance(piece, str):
                content = (content or "") + piece
            if isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
        assembled = {
            "model": model,
            "choices": [{"message": {"role": role, "content": content}, "finish_reason": finish}],
        }
        if usage is not None:
            assembled["usage"] = usage
        return assembled

    async def _stream_chunks(self, path, payload, timeout):
        url = path if path.startswith("https://") else self.base_url + path
        headers = {"Accept": "text/event-stream", "Accept-Encoding": "identity"}
        if self._credential is not None:
            headers[self.credential_header] = (
                self.credential_prefix + self._credential.get_secret_value()
            )
        chunks, size = [], 0
        try:
            async with self._client.stream(
                "POST", url, headers=headers, json=payload, timeout=timeout
            ) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    if self.settings.provider_error_diagnostics:
                        self._record_error_diagnostic(response.status_code, raw)
                    self._error_from_body(
                        response.status_code, response.headers, self._maybe_json(raw)
                    )
                    raise MalformedResponse("PROVIDER_ERROR_ENVELOPE")
                self._quota = self._observe(response.headers)
                async for line in response.aiter_lines():
                    size += len(line)
                    if size > 4_000_000 or len(chunks) > 100_000:
                        raise MalformedResponse("PROVIDER_RESPONSE_TOO_LARGE")
                    if not line.startswith("data:"):
                        continue
                    body = line[len("data:") :].strip()
                    if not body or body == "[DONE]":
                        continue
                    chunks.append(strict_json(body))
        except httpx.HTTPError:
            raise ProviderUnavailable("PROVIDER_TRANSPORT_FAILED") from None
        except (ValueError, UnicodeError, RecursionError):
            raise MalformedResponse("MALFORMED_PROVIDER_RESPONSE") from None
        if not chunks:
            raise MalformedResponse("EMPTY_COMPLETION_STREAM")
        return chunks

    @staticmethod
    def _maybe_json(raw):
        try:
            value = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    async def _json(self, method, path, payload=None, timeout=None):
        self._admit()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self._credential is not None:
            headers[self.credential_header] = (
                self.credential_prefix + self._credential.get_secret_value()
            )
        url = path if path.startswith("https://") else self.base_url + path
        try:
            async with self._client.stream(
                method, url, headers=headers, json=payload, timeout=timeout or self._client.timeout
            ) as response:
                # An error body is read only when a subclass parses it for quota or
                # retry detail, or when an operator has turned diagnostics on. With
                # neither, the status code alone is the observation and the body is
                # never fetched -- the behaviour this branch has always had.
                capture = response.status_code != 200 and self.settings.provider_error_diagnostics
                if response.status_code != 200 and not self.inspect_error_body and not capture:
                    self._error(response.status_code, response.headers)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise MalformedResponse("COMPRESSED_PROVIDER_RESPONSE_UNSUPPORTED")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 4_000_000:
                        raise MalformedResponse("PROVIDER_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if capture:
                    self._record_error_diagnostic(response.status_code, raw)
                if response.status_code != 200 and not self.inspect_error_body:
                    # The same code in the same order as with diagnostics off; a
                    # body that is not JSON must not change what the caller sees.
                    self._error(response.status_code, response.headers)
                value = strict_json(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError()
                if response.status_code != 200:
                    self._error_from_body(response.status_code, response.headers, value)
                    raise MalformedResponse("PROVIDER_ERROR_ENVELOPE")
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
        # A catalog that just failed will not succeed on the next model in the same
        # solve, and every eligible model asks again: one unreachable endpoint cost
        # three 25-second read timeouts in a row because only success was cached.
        # The failure is held as briefly as a successful catalog is held fresh.
        if (
            self._catalog_error is not None
            and now - self._catalog_error_at < self._catalog_error_ttl
        ):
            raise self._catalog_error.with_traceback(None)
        try:
            result = await self._fetch_models()
        except Exception as error:
            self._catalog_error, self._catalog_error_at = error, now
            raise
        self._catalog_error = None
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

    def _catalog_output(self, entry):
        """The provider's own maximum completion tokens for this model, when it says."""
        return None if self.output_field is None else entry.get(self.output_field)

    async def _fetch_models(self):
        entries = self._catalog_entries(await self._json("GET", self.catalog_path))
        result, dropped = [], {}
        for configured in self.spec.models:
            matches = [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get(self.catalog_id_key) == configured.model_id
            ]
            if not configured.active:
                dropped[configured.model_id] = "DESCRIPTOR_INACTIVE"
                continue
            if len(matches) != 1:
                # Four different causes used to leave the same trace: the model gone
                # from the catalog, listed twice, switched off, repriced, or shrunk
                # below its reviewed context. The router can only report that the
                # model was unavailable, so the reason is recorded for the operator.
                dropped[configured.model_id] = (
                    "ABSENT_FROM_CATALOG" if not matches else "AMBIGUOUS_IN_CATALOG"
                )
                continue
            entry = matches[0]
            if entry.get("active", True) is not True:
                dropped[configured.model_id] = "INACTIVE_IN_CATALOG"
                continue
            if self.zero_price_models and not zero_priced(entry):
                dropped[configured.model_id] = "NOT_ZERO_PRICED"
                continue
            if self.context_field is not None:
                context = self._catalog_context(entry)
                if type(context) is not int:
                    dropped[configured.model_id] = "CONTEXT_NOT_REPORTED"
                    continue
                if context < configured.context_window:
                    dropped[configured.model_id] = (
                        f"CATALOG_CONTEXT_BELOW_REVIEWED_{configured.context_window}"
                    )
                    continue
            result.append(self._with_catalog_output(configured, entry))
        self._catalog_drops = dropped
        return result

    def _with_catalog_output(self, configured, entry):
        """Lower the reviewed output limit to the provider's own, never raise it.

        A live catalog is current where a reviewed descriptor is a claim from the day
        it was written, so a smaller published limit wins. The reverse is not true:
        raising a limit on unreviewed data is the guess this package refuses to make.
        A provider that publishes no usable limit changes nothing.
        """
        published = self._catalog_output(entry)
        if type(published) is not int or published < 1:
            return configured.model_copy(deep=True)
        limit = min(published, configured.max_output_tokens or published)
        return configured.model_copy(deep=True, update={"max_output_tokens": limit})

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

    def _schema_for_transport(self, request):
        """The schema this provider can parse, and prompt text for what it cannot carry."""
        if request.expected_json_schema is None:
            return None, None
        return (
            transport_schema(request.expected_json_schema, self.schema_dialect),
            constraint_notes(request.expected_json_schema, self.schema_dialect),
        )

    @staticmethod
    def _task_text(request, note):
        return request.task if note is None else request.task + "\n\n" + note

    async def complete(self, request):
        models = await self.list_models()
        model = next((item for item in models if item.model_id == request.model_id), None)
        if model is None:
            raise ModelUnavailable("REVIEWED_MODEL_UNAVAILABLE_OR_PRICING_CHANGED")
        limit = min(model.max_output_tokens or MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)
        if not 1 <= request.max_output_tokens <= limit:
            raise RequestNotSupported("OUTPUT_BUDGET_INVALID")
        if len(request.task.encode()) + request.max_output_tokens > model.context_window:
            raise RequestNotSupported("CONTEXT_BUDGET_EXCEEDED")
        schema, note = self._schema_for_transport(request)
        streaming = self.supports_streaming
        payload = {
            "model": model.model_id,
            "messages": [{"role": "user", "content": self._task_text(request, note)}],
            "stream": streaming,
            self.output_tokens_key: request.max_output_tokens,
        }
        payload.update(copy.deepcopy(self.extra_payload))
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "fair_result", "strict": True, "schema": schema},
            }
        await self._before_completion(payload)
        if len(json.dumps(payload).encode()) + request.max_output_tokens > model.context_window:
            raise RequestNotSupported("CONTEXT_BUDGET_EXCEEDED")
        timeout = self._completion_timeout(request, streaming)
        data = (
            await self._stream_json("/chat/completions", payload, timeout)
            if streaming
            else await self._json("POST", "/chat/completions", payload, timeout=timeout)
        )
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
    # The zero-cost observation this adapter fails closed without has to arrive in
    # the response. Whether OpenRouter emits it on a stream is unverified against the
    # live API, and a wrong answer here would refuse every completion rather than
    # merely time one out. Buffered until a live check says otherwise.
    supports_streaming = False
    # OpenRouter reports the completion ceiling under top_provider rather than at the
    # top level, so the read is a hook rather than a field name. Unconfirmed against
    # the live API: if the shape is wrong the value is simply absent and the reviewed
    # descriptor stands, which is the behaviour before this existed.
    output_field = "top_provider"
    account_check_path = "/key"
    # _after_completion fails closed unless the response reports a zero cost,
    # and OpenRouter only returns usage accounting when it is asked for. Without
    # this every completion would be rejected as an unconfirmed cost.
    extra_payload = {"usage": {"include": True}}
    provider_preferences = {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
    }

    def _catalog_output(self, entry):
        top = entry.get(self.output_field)
        return top.get("max_completion_tokens") if isinstance(top, dict) else None


class KiloFreeAdapter(TextAdapter):
    """Kilo Gateway: only current ':free' models with zero catalog pricing pass."""

    base_url = "https://api.kilo.ai/api/gateway"
    expected_provider = "kilo_free"
    expected_access = "FREE_DYNAMIC"
    zero_price_models = True
    # As OpenRouter: cost_microdollars has to come back with the answer, and its
    # own comment notes some non-streaming responses already omit it.
    supports_streaming = False

    _explicitly_temporary_ids = frozenset(
        {
            "poolside/laguna-s-2.1:free",
            "stepfun/step-3.7-flash:free",
        }
    )

    def _admit(self):
        super()._admit()
        for model in self.spec.models:
            model_id = model.model_id.casefold()
            if (
                model_id.startswith("nvidia/")
                or model_id.startswith("inclusionai/ling-3.0-flash")
                or "preview" in model_id
                or model_id in self._explicitly_temporary_ids
            ):
                raise AuthenticationFailed("KILO_TRIAL_OR_PROMOTIONAL_MODEL_NOT_ALLOWED")

    def __init__(self, *args, **kwargs):
        self._cost_observation = "NOT_OBSERVED"
        super().__init__(*args, **kwargs)

    def safe_diagnostics(self):
        """Fixed billing-verification state, plus whatever the base adapter recorded."""
        return super().safe_diagnostics() | {"cost_microdollars": self._cost_observation}

    def _after_completion(self, data):
        usage = data.get("usage")
        if not isinstance(usage, dict):
            self._cost_observation = "USAGE_MISSING"
            raise BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED")
        if "cost_microdollars" in usage:
            if zero(usage["cost_microdollars"]):
                self._cost_observation = "ZERO"
                return
            self._cost_observation = "NONZERO_OR_INVALID"
            raise BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED")

        # Kilo's current Gateway contract explicitly exposes ':free' models as
        # free. Some non-streaming responses omit cost_microdollars even though
        # usage is present, so accept only an exact fresh catalog proof for the
        # exact response model: ':free' suffix plus zero catalog pricing.
        response_model = data.get("model")
        fresh_catalog = (
            self._model_cache is not None
            and 0 <= self.clock() - self._model_cache_at < self._catalog_ttl
        )
        catalog_ids = {model.model_id for model in (self._model_cache or [])}
        if (
            fresh_catalog
            and isinstance(response_model, str)
            and response_model.endswith(":free")
            and response_model in catalog_ids
        ):
            self._cost_observation = "CATALOG_ZERO_PRICE_FALLBACK"
            return

        self._cost_observation = "COST_FIELD_MISSING"
        raise BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED")


class MistralAdapter(TextAdapter):
    base_url = "https://api.mistral.ai/v1"
    expected_provider = "mistral"
    expected_access = "FREE_RECURRING"
    context_field = "max_context_length"
    admin_spend_limit_url = "https://api.mistral.ai/v1/admin/spend-limit"
    admin_usage_url = "https://api.mistral.ai/v1/admin/usage"
    _monthly_recheck_seconds = 6 * 60 * 60

    def __init__(self, *args, admin_credential=None, **kwargs):
        self._admin_credential = admin_credential
        super().__init__(*args, **kwargs)

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

    async def _admin_json(self, url):
        if self._admin_credential is None:
            return None
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "x-api-key": self._admin_credential.get_secret_value(),
        }
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    return None
                if response.headers.get("content-encoding", "identity") != "identity":
                    return None
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        return None
                    chunks.append(chunk)
                data = strict_json(b"".join(chunks).decode("utf-8"))
                return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
            return None

    async def _monthly_limit_reached(self):
        data = await self._admin_json(self.admin_spend_limit_url)
        if data is None:
            return False
        limits = data.get("limits")
        completion = limits.get("completion") if isinstance(limits, dict) else None
        return isinstance(completion, dict) and completion.get("monthly_limit_reached") is True

    async def _monthly_reset_at(self):
        data = await self._admin_json(self.admin_usage_url)
        if data is not None and isinstance(data.get("end_date"), str):
            try:
                end = datetime.fromisoformat(data["end_date"].replace("Z", "+00:00"))
                reset_at = end.timestamp()
                if self.clock() < reset_at <= self.clock() + 40 * SECONDS_IN_DAY:
                    return reset_at
            except (ValueError, OverflowError):
                pass
        # A confirmed monthly limit with no trustworthy period end stays out of
        # rotation temporarily, then rechecks. This avoids both request hammering
        # and accidentally parking Mistral for an extra full month.
        return self.clock() + self._monthly_recheck_seconds

    async def complete(self, request):
        try:
            return await super().complete(request)
        except (QuotaExceeded, RateLimited):
            if await self._monthly_limit_reached():
                reset_at = await self._monthly_reset_at()
                self._quota = QuotaSnapshot(
                    provider_id=self.provider_id,
                    quota_remaining_estimate=0,
                    reset_at=reset_at,
                )
                raise QuotaExceeded("MONTHLY_USAGE_LIMIT_REACHED", reset_at=reset_at) from None
            raise


class ZaiFreeAdapter(TextAdapter):
    """Legacy compatibility shim that fails closed.

    Z.ai offers trial, prepaid, and paid-plan API access, not a recurring free
    API tier. FAIR therefore must never admit this provider.
    """

    expected_provider = "zai_free"
    expected_access = "FREE_DYNAMIC"

    def _admit(self):
        raise AuthenticationFailed("NO_RECURRING_FREE_TIER")


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
    """Workers AI on an operator-confirmed Workers Free account.

    Cloudflare does not document per-response neuron usage on the OpenAI-compatible
    endpoint. Free-plan safety therefore relies on Cloudflare's documented hard
    10,000-neuron/day allocation: code 3036 means the allocation is exhausted and
    further free-plan requests fail until 00:00 UTC.
    """

    expected_provider = "cloudflare_workers_ai"
    expected_access = "FREE_RECURRING"
    catalog_key = "result"
    catalog_id_key = "name"
    context_field = "context_window"
    inspect_error_body = True

    def __init__(self, spec, settings, account_id, **kwargs):
        if not isinstance(account_id, str) or not re.fullmatch(r"[0-9a-f]{32}", account_id):
            raise AuthenticationFailed("CLOUDFLARE_ACCOUNT_ID_REQUIRED")
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
        self.catalog_path = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/models/search?task=Text%20Generation&per_page=100"
        )
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

    def _reset_at(self):
        return (int(self.clock() // SECONDS_IN_DAY) + 1) * SECONDS_IN_DAY

    @staticmethod
    def _cloudflare_error_code(data):
        error = data.get("error")
        if isinstance(error, dict) and type(error.get("code")) is int:
            return error["code"]
        errors = data.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict) and type(item.get("code")) is int:
                    return item["code"]
        return None

    def _error_from_body(self, status, headers, data):
        code = self._cloudflare_error_code(data)
        if status == 429 and code == 3036:
            self._quota = QuotaSnapshot(
                provider_id=self.provider_id,
                quota_limit=10_000,
                quota_remaining_estimate=0,
                reset_at=self._reset_at(),
            )
            raise QuotaExceeded("DAILY_NEURON_ALLOCATION_SPENT", reset_at=self._reset_at())
        if status == 429 and code == 3040:
            raise RateLimited(
                "RATE_LIMITED",
                retry_after=retry_seconds(headers.get("retry-after"), self.clock()),
            )
        self._error(status, headers)


class GeminiAdapter(TextAdapter):
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    expected_provider = "google_gemini_api"
    expected_access = "FREE_RECURRING"
    credential_header = "x-goog-api-key"
    credential_prefix = ""
    inspect_error_body = True
    schema_dialect = GEMINI
    _pacific = ZoneInfo("America/Los_Angeles")

    def _daily_reset_at(self):
        local = datetime.fromtimestamp(self.clock(), self._pacific)
        tomorrow = local.date() + timedelta(days=1)
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=self._pacific).timestamp()

    @staticmethod
    def _retry_delay_from_details(error):
        details = error.get("details")
        if not isinstance(details, list):
            return None
        for detail in details:
            if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("RetryInfo"):
                delay = duration(detail.get("retryDelay"))
                if delay is not None:
                    return delay
        return None

    @staticmethod
    def _quota_ids(error):
        ids = []
        details = error.get("details")
        if not isinstance(details, list):
            return ids
        for detail in details:
            if not isinstance(detail, dict) or not str(detail.get("@type", "")).endswith(
                "QuotaFailure"
            ):
                continue
            violations = detail.get("violations")
            if not isinstance(violations, list):
                continue
            for violation in violations:
                if isinstance(violation, dict) and isinstance(violation.get("quotaId"), str):
                    ids.append(violation["quotaId"])
        return ids

    def _error_from_body(self, status, headers, data):
        error = data.get("error")
        if status != 429 or not isinstance(error, dict):
            self._error(status, headers)
            return

        code = error.get("code")
        quota_ids = self._quota_ids(error)
        daily = code == "quota_exceeded" or any(
            "perday" in quota_id.casefold() for quota_id in quota_ids
        )
        if daily:
            reset_at = self._daily_reset_at()
            self._quota = QuotaSnapshot(
                provider_id=self.provider_id,
                quota_remaining_estimate=0,
                reset_at=reset_at,
            )
            raise QuotaExceeded("DAILY_QUOTA_EXHAUSTED", reset_at=reset_at)

        retry_after = self._retry_delay_from_details(error)
        if (
            code in {"rate_limit_exceeded", "too_many_requests"}
            or quota_ids
            or retry_after is not None
        ):
            if retry_after is None:
                retry_after = retry_seconds(headers.get("retry-after"), self.clock())
            raise RateLimited("RATE_LIMITED", retry_after=retry_after)

        self._error(status, headers)

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

    async def _stream_generate(self, model, payload, timeout):
        """Assemble streamed candidates into the envelope generateContent returns.

        The checks in complete() -- model identity, prompt feedback, role, finish
        reason and the per-part key rules -- then run exactly as they do on a
        buffered answer. Anything this method cannot aggregate is handed on
        unchanged rather than rejected here, so a malformed stream is refused by
        those checks, under the code they have always used, instead of gaining a
        second name for the same failure.
        """
        chunks = await self._stream_chunks(
            "/models/" + model.model_id + ":streamGenerateContent?alt=sse", payload, timeout
        )
        version = finish = feedback = None
        role = "model"
        parts: list = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                return {}
            if isinstance(chunk.get("modelVersion"), str):
                version = chunk["modelVersion"]
            if chunk.get("promptFeedback"):
                feedback = chunk["promptFeedback"]
            candidates = chunk.get("candidates")
            if not candidates:
                continue
            if not isinstance(candidates, list) or len(candidates) != 1:
                return chunk
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                return chunk
            if isinstance(candidate.get("finishReason"), str):
                finish = candidate["finishReason"]
            content = candidate.get("content")
            if content is None:
                continue
            if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
                return chunk
            if isinstance(content.get("role"), str):
                role = content["role"]
            parts.extend(content.get("parts") or [])
        assembled = {
            "modelVersion": version,
            "candidates": [
                {
                    "content": {"role": role, "parts": self._merge_parts(parts)},
                    "finishReason": finish,
                }
            ],
        }
        if feedback is not None:
            assembled["promptFeedback"] = feedback
        return assembled

    @staticmethod
    def _merge_parts(parts):
        """Collapse a stream's text fragments so the envelope sees an ordinary part list.

        One part per chunk would put thousands in a large answer and trip the 4096-part
        bound on length alone. Only neighbours carrying the same keys and the same
        thought flag merge, so nothing a part declares is lost, and a part this cannot
        read is passed through for the envelope check to refuse.
        """
        merged: list[tuple[object, dict]] = []
        for part in parts:
            if not isinstance(part, dict):
                merged.append((None, part))
                continue
            signature = (tuple(sorted(part.keys() - {"text"})), part.get("thought", False))
            if merged and merged[-1][0] == signature and isinstance(part.get("text"), str):
                merged[-1][1]["text"] = merged[-1][1].get("text", "") + part["text"]
                continue
            merged.append((signature, dict(part)))
        return [item for _, item in merged]

    async def complete(self, request):
        model = next(
            (m for m in self.spec.models if m.active and m.model_id == request.model_id), None
        )
        if model is None:
            raise ModelUnavailable("REVIEWED_GEMINI_MODEL_REQUIRED")
        if not 1 <= request.max_output_tokens <= self.settings.gemini_max_output_tokens:
            raise RequestNotSupported("OUTPUT_BUDGET_INVALID")
        schema, note = self._schema_for_transport(request)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": self._task_text(request, note)}]}],
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": request.max_output_tokens,
            },
        }
        if schema is not None:
            payload["generationConfig"].update(
                responseMimeType="application/json", responseJsonSchema=schema
            )
        # Gemini publishes separate input and output limits; the output allowance does not
        # consume the configured input capacity. Byte counting remains conservative.
        if len(json.dumps(payload).encode()) > model.context_window:
            raise RequestNotSupported("CONTEXT_BUDGET_EXCEEDED")
        metadata = await self._metadata(model)
        if metadata is None:
            raise ModelUnavailable("REVIEWED_GEMINI_MODEL_UNAVAILABLE_OR_CHANGED")
        if request.max_output_tokens > metadata["outputTokenLimit"]:
            raise RequestNotSupported("OUTPUT_BUDGET_EXCEEDS_MODEL_LIMIT")
        streaming = self.supports_streaming
        timeout = self._completion_timeout(request, streaming)
        if streaming:
            data = await self._stream_generate(model, payload, timeout)
        else:
            data = await self._json(
                "POST",
                "/models/" + model.model_id + ":generateContent",
                payload,
                timeout=timeout,
            )
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
    schema_dialect = JSON_SCHEMA

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
            raise ModelUnavailable("LOCAL_REVIEWED_MODEL_REQUIRED")
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
            not 1
            <= request.max_output_tokens
            <= min(model.max_output_tokens or MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)
            or len(request.task.encode()) + request.max_output_tokens > model.context_window
        ):
            raise RequestNotSupported("CONTEXT_OR_OUTPUT_BUDGET_EXCEEDED")
        schema, note = self._schema_for_transport(request)
        payload = {
            "model": model.model_id,
            "stream": False,
            "messages": [{"role": "user", "content": self._task_text(request, note)}],
            "options": {"num_predict": request.max_output_tokens, "num_ctx": model.context_window},
            "keep_alive": 0,
        }
        if schema is not None:
            payload["format"] = schema
        if len(json.dumps(payload).encode()) + request.max_output_tokens > model.context_window:
            raise RequestNotSupported("CONTEXT_BUDGET_EXCEEDED")
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
