"""Transport-level tests for the Gemini and local Ollama adapters.

Both were entirely untested, though Gemini is a configured cloud provider and
local Ollama is now the only route for data above PUBLIC. The point of these
tests is that a response FAIR did not verify is never turned into an answer:
every malformed, blocked, remote-backed or over-budget case must raise rather
than return text.
"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from fair.providers.base import (
    AuthenticationFailed,
    MalformedResponse,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.live import GeminiAdapter, LiveSettings, OllamaLocalAdapter
from fair.schemas.domain import NormalizedModelRequest, ProviderSpec
from fair.schemas.qualification import ModelQualification, ProviderQualification

GEMINI_MODEL = "gemini-3.6-flash"
LOCAL_MODEL = "llama3:8b"
CONTEXT = 32768


def _qualification(provider_id, model_id, reviewed, revision=None):
    return ProviderQualification(
        provider_id=provider_id,
        free_status="verified_free_plan",
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
                model_revision=revision,
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
    )


def _gemini_spec(model_id=GEMINI_MODEL, revision=None, **overrides):
    """qualified() requires the review to name the same model revision as the spec."""
    reviewed = datetime.now(UTC) - timedelta(seconds=10)
    values = dict(
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
        request_limit_window="DAILY_PACIFIC",
        models=[
            {
                "model_id": model_id,
                "context_window": CONTEXT,
                "capabilities": {"reasoning"},
                "model_revision": revision,
            }
        ],
        qualification=_qualification("google_gemini_api", model_id, reviewed, revision),
    )
    return ProviderSpec(**(values | overrides))


def _local_spec(model_id=LOCAL_MODEL, **overrides):
    values = dict(
        provider_id="ollama_local",
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        max_data_class="RESTRICTED",
        models=[{"model_id": model_id, "context_window": CONTEXT, "capabilities": {"reasoning"}}],
    )
    return ProviderSpec(**(values | overrides))


def _settings(**overrides):
    values = dict(
        enabled=True,
        confirmed_providers={"google_gemini_api", "ollama_local"},
        ollama_local_only_confirmed=True,
    )
    return LiveSettings(**(values | overrides))


def _request(model_id, task="ping", max_output_tokens=64, schema=None):
    return NormalizedModelRequest(
        task=task,
        model_id=model_id,
        request_id="r",
        client_id="c",
        task_class="general",
        max_output_tokens=max_output_tokens,
        expected_json_schema=schema,
    )


def _as_sse(body):
    """Serve a generateContent body as one stream event, malformations and all.

    A converter that reshaped the body would normalise away the very anomalies the
    refusal tests supply, so the body is transmitted exactly as configured.
    """
    text = f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


def _transport(routes):
    """A ':generateContent' route also answers the streaming endpoint, as SSE."""
    seen = []

    def handler(request):
        seen.append(request)
        streaming = ":streamGenerateContent" in str(request.url)
        for (method, suffix), response in routes.items():
            if request.method != method:
                continue
            if not (suffix in str(request.url) or (streaming and suffix == ":generateContent")):
                continue
            if callable(response):
                return response(request)
            status, body = response
            if status == 200 and streaming:
                return _as_sse(body)
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": {"code": 404}})

    return httpx.MockTransport(handler), seen


# ── Gemini ──────────────────────────────────────────────────────────────


def _metadata(model_id=GEMINI_MODEL, **overrides):
    values = dict(
        name="models/" + model_id,
        supportedGenerationMethods=["generateContent"],
        inputTokenLimit=CONTEXT,
        outputTokenLimit=8192,
    )
    return values | overrides


def _generation(model_id=GEMINI_MODEL, text="pong", finish="STOP", **overrides):
    body = {
        "modelVersion": model_id,
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": finish,
            }
        ],
    }
    return body | overrides


class TestGemini:
    def _adapter(self, routes, spec=None, settings=None):
        transport, seen = _transport(routes)
        adapter = GeminiAdapter(
            spec or _gemini_spec(),
            settings or _settings(),
            credential=SecretStr("k"),
            transport=transport,
        )
        return adapter, seen

    async def test_a_reviewed_model_completes(self):
        adapter, seen = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, _generation()),
            }
        )
        response = await adapter.complete(_request(GEMINI_MODEL))
        assert response.text == "pong"
        assert response.finish_reason == "stop"
        assert response.model_id == GEMINI_MODEL

    async def test_the_credential_travels_in_the_gemini_header(self):
        adapter, seen = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, _generation()),
            }
        )
        await adapter.complete(_request(GEMINI_MODEL))
        assert seen[-1].headers["x-goog-api-key"] == "k"
        assert "authorization" not in seen[-1].headers

    async def test_daily_quota_failure_exhausts_until_midnight_pacific(self):
        now = datetime.now(UTC).timestamp()
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                    "violations": [
                                        {
                                            "quotaId": (
                                                "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                                            )
                                        }
                                    ],
                                },
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "45s",
                                },
                            ],
                        }
                    },
                ),
            }
        )
        adapter.clock = lambda: now
        with pytest.raises(QuotaExceeded) as raised:
            await adapter.complete(_request(GEMINI_MODEL))
        assert raised.value.reset_at is not None
        reset = datetime.fromtimestamp(raised.value.reset_at, ZoneInfo("America/Los_Angeles"))
        assert reset.hour == 0 and reset.minute == 0 and reset.second == 0
        assert reset.timestamp() > now

    async def test_minute_quota_failure_is_transient_and_uses_retry_info(self):
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                    "violations": [
                                        {
                                            "quotaId": (
                                                "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
                                            )
                                        }
                                    ],
                                },
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "12.5s",
                                },
                            ],
                        }
                    },
                ),
            }
        )
        with pytest.raises(RateLimited) as raised:
            await adapter.complete(_request(GEMINI_MODEL))
        assert raised.value.retry_after == 12.5

    async def test_modern_quota_exceeded_code_is_treated_as_daily(self):
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (
                    429,
                    {"error": {"code": "quota_exceeded", "message": "daily quota"}},
                ),
            }
        )
        with pytest.raises(QuotaExceeded):
            await adapter.complete(_request(GEMINI_MODEL))

    async def test_a_truncated_answer_reports_length(self):
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, _generation(finish="MAX_TOKENS")),
            }
        )
        assert (await adapter.complete(_request(GEMINI_MODEL))).finish_reason == "length"

    async def test_thought_parts_are_excluded_from_the_answer(self):
        body = _generation()
        body["candidates"][0]["content"]["parts"] = [
            {"text": "internal reasoning", "thought": True},
            {"text": "pong"},
        ]
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, body),
            }
        )
        assert (await adapter.complete(_request(GEMINI_MODEL))).text == "pong"

    async def test_a_json_schema_request_sets_the_response_mime_type(self):
        import json

        adapter, seen = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, _generation(text='{"a": 1}')),
            }
        )
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        await adapter.complete(_request(GEMINI_MODEL, schema=schema))
        config = json.loads(seen[-1].content)["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseJsonSchema"] == schema

    def test_a_non_gemini_model_name_is_refused(self):
        """The adapter admits itself on construction, before any request."""
        with pytest.raises(AuthenticationFailed, match="EXPLICIT_GEMINI_MODEL_REQUIRED"):
            self._adapter({}, spec=_gemini_spec("gpt-4o"))

    async def test_an_unreviewed_model_is_refused(self):
        adapter, _ = self._adapter({})
        with pytest.raises(AuthenticationFailed, match="REVIEWED_GEMINI_MODEL_REQUIRED"):
            await adapter.complete(_request("gemini-other"))

    @pytest.mark.parametrize(
        "metadata_overrides",
        [
            {"supportedGenerationMethods": ["embedContent"]},
            {"supportedGenerationMethods": "generateContent"},
            {"inputTokenLimit": CONTEXT - 1},
            {"inputTokenLimit": "many"},
            {"outputTokenLimit": 0},
            {"name": "models/other"},
        ],
    )
    async def test_a_model_whose_metadata_changed_is_dropped(self, metadata_overrides):
        adapter, _ = self._adapter(
            {("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata(**metadata_overrides))}
        )
        assert await adapter.list_models() == []

    async def test_a_revision_mismatch_drops_the_model(self):
        adapter, _ = self._adapter(
            {("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata(version="001"))},
            spec=_gemini_spec(revision="002"),
        )
        assert await adapter.list_models() == []

    async def test_a_matching_revision_keeps_the_model(self):
        adapter, _ = self._adapter(
            {("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata(version="002"))},
            spec=_gemini_spec(revision="002"),
        )
        assert [model.model_id for model in await adapter.list_models()] == [GEMINI_MODEL]

    async def test_a_model_that_disappeared_before_dispatch_is_refused(self):
        adapter, _ = self._adapter(
            {("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata(outputTokenLimit=0))}
        )
        with pytest.raises(AuthenticationFailed, match="UNAVAILABLE_OR_CHANGED"):
            await adapter.complete(_request(GEMINI_MODEL))

    async def test_an_output_budget_over_the_model_limit_is_refused(self):
        adapter, _ = self._adapter(
            {("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata(outputTokenLimit=32))}
        )
        with pytest.raises(MalformedResponse, match="OUTPUT_BUDGET_EXCEEDS_MODEL_LIMIT"):
            await adapter.complete(_request(GEMINI_MODEL, max_output_tokens=64))

    async def test_an_output_budget_over_the_configured_ceiling_is_refused(self):
        adapter, _ = self._adapter({}, settings=_settings(gemini_max_output_tokens=32))
        with pytest.raises(MalformedResponse, match="OUTPUT_BUDGET_INVALID"):
            await adapter.complete(_request(GEMINI_MODEL, max_output_tokens=64))

    async def test_a_task_larger_than_the_context_window_is_refused(self):
        adapter, _ = self._adapter({})
        with pytest.raises(MalformedResponse, match="CONTEXT_BUDGET_EXCEEDED"):
            await adapter.complete(_request(GEMINI_MODEL, task="x" * (CONTEXT + 1)))

    @pytest.mark.parametrize(
        "body",
        [
            {"candidates": []},
            {"modelVersion": "other"},
            {"candidates": "not a list"},
            {"promptFeedback": {"blockReason": "SAFETY"}},
        ],
    )
    async def test_a_malformed_envelope_is_refused(self, body):
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, _generation(**body)),
            }
        )
        with pytest.raises(MalformedResponse, match="INVALID_GEMINI_COMPLETION"):
            await adapter.complete(_request(GEMINI_MODEL))

    @pytest.mark.parametrize(
        "candidate",
        [
            {"content": {"role": "user", "parts": [{"text": "pong"}]}, "finishReason": "STOP"},
            {"content": {"role": "model", "parts": [{"text": "pong"}]}, "finishReason": "SAFETY"},
            {"content": {"role": "model", "parts": []}, "finishReason": "STOP"},
            {"content": {"role": "model", "parts": [{"text": "   "}]}, "finishReason": "STOP"},
            {"content": {"role": "model", "parts": [{"inlineData": {}}]}, "finishReason": "STOP"},
            {"content": {"role": "model", "parts": [{"text": 1}]}, "finishReason": "STOP"},
            {
                "content": {"role": "model", "parts": [{"text": "p", "thought": "yes"}]},
                "finishReason": "STOP",
            },
        ],
    )
    async def test_a_malformed_candidate_is_refused(self, candidate):
        body = _generation()
        body["candidates"] = [candidate]
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, body),
            }
        )
        with pytest.raises(MalformedResponse, match="INVALID_GEMINI_COMPLETION"):
            await adapter.complete(_request(GEMINI_MODEL))

    async def test_an_answer_of_only_thoughts_is_refused(self):
        body = _generation()
        body["candidates"][0]["content"]["parts"] = [{"text": "reasoning", "thought": True}]
        adapter, _ = self._adapter(
            {
                ("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata()),
                ("POST", ":generateContent"): (200, body),
            }
        )
        with pytest.raises(MalformedResponse, match="INVALID_GEMINI_COMPLETION"):
            await adapter.complete(_request(GEMINI_MODEL))

    async def test_an_inactive_model_is_not_listed(self):
        """A spec with no active model at all fails admission, so keep one active."""
        spec = _gemini_spec()
        retired = spec.models[0].model_copy(update={"model_id": "gemini-retired", "active": False})
        spec.models.append(retired)
        adapter, _ = self._adapter(
            {("GET", f"/models/{GEMINI_MODEL}"): (200, _metadata())}, spec=spec
        )
        assert [model.model_id for model in await adapter.list_models()] == [GEMINI_MODEL]


# ── Local Ollama ────────────────────────────────────────────────────────


def _tags(model_id=LOCAL_MODEL, **overrides):
    entry = {
        "name": model_id,
        "size": 4_000_000_000,
        "details": {"format": "gguf", "family": "llama"},
    }
    return {"models": [entry | overrides]}


def _show(**overrides):
    body = {
        "details": {"format": "gguf"},
        "capabilities": ["completion"],
        "model_info": {"general.architecture": "llama", "llama.context_length": CONTEXT},
    }
    return body | overrides


def _chat(model_id=LOCAL_MODEL, content="pong", **overrides):
    body = {
        "model": model_id,
        "done": True,
        "done_reason": "stop",
        "message": {"role": "assistant", "content": content},
    }
    return body | overrides


class TestOllamaLocal:
    def _adapter(self, routes, spec=None, settings=None):
        transport, seen = _transport(routes)
        adapter = OllamaLocalAdapter(
            spec or _local_spec(), settings or _settings(), transport=transport
        )
        return adapter, seen

    async def test_a_local_model_completes(self):
        adapter, _ = self._adapter(
            {
                ("GET", "/api/tags"): (200, _tags()),
                ("POST", "/api/show"): (200, _show()),
                ("POST", "/api/chat"): (200, _chat()),
            }
        )
        response = await adapter.complete(_request(LOCAL_MODEL))
        assert response.text == "pong"
        assert response.provider_id == "ollama_local"

    async def test_no_credential_is_sent_to_the_local_daemon(self):
        adapter, seen = self._adapter(
            {
                ("GET", "/api/tags"): (200, _tags()),
                ("POST", "/api/show"): (200, _show()),
                ("POST", "/api/chat"): (200, _chat()),
            }
        )
        await adapter.complete(_request(LOCAL_MODEL))
        assert "authorization" not in seen[-1].headers
        assert "x-goog-api-key" not in seen[-1].headers

    async def test_the_model_is_unloaded_after_the_call(self):
        import json

        adapter, seen = self._adapter(
            {
                ("GET", "/api/tags"): (200, _tags()),
                ("POST", "/api/show"): (200, _show()),
                ("POST", "/api/chat"): (200, _chat()),
            }
        )
        await adapter.complete(_request(LOCAL_MODEL))
        assert json.loads(seen[-1].content)["keep_alive"] == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"details": {"format": "safetensors"}},
            {"details": "not a dict"},
            {"size": 0},
            {"size": "big"},
            {"remote_model": "gpt-4o"},
            {"remote_host": "https://ollama.com"},
            {"name": "other-model"},
        ],
    )
    async def test_a_catalog_entry_that_is_not_a_local_gguf_is_dropped(self, overrides):
        adapter, _ = self._adapter({("GET", "/api/tags"): (200, _tags(**overrides))})
        assert await adapter.list_models() == []

    async def test_a_cloud_model_name_is_never_listed(self):
        spec = _local_spec("gpt-oss:120b-cloud")
        adapter, _ = self._adapter(
            {("GET", "/api/tags"): (200, _tags("gpt-oss:120b-cloud"))}, spec=spec
        )
        assert await adapter.list_models() == []

    async def test_a_malformed_catalog_is_refused(self):
        adapter, _ = self._adapter({("GET", "/api/tags"): (200, {"models": "none"})})
        with pytest.raises(MalformedResponse, match="INVALID_LOCAL_CATALOG"):
            await adapter.list_models()

    async def test_an_unreviewed_model_is_refused(self):
        adapter, _ = self._adapter({("GET", "/api/tags"): (200, _tags())})
        with pytest.raises(AuthenticationFailed, match="LOCAL_REVIEWED_MODEL_REQUIRED"):
            await adapter.complete(_request("other-model"))

    @pytest.mark.parametrize(
        "overrides",
        [
            {"capabilities": ["embedding"]},
            {"capabilities": "completion"},
            {"details": {"format": "safetensors"}},
            {"model_info": "none"},
            {"remote_model": "gpt-4o"},
            {"remote_host": "https://ollama.com"},
        ],
    )
    async def test_weights_that_are_not_local_completion_weights_are_refused(self, overrides):
        adapter, _ = self._adapter(
            {
                ("GET", "/api/tags"): (200, _tags()),
                ("POST", "/api/show"): (200, _show(**overrides)),
            }
        )
        with pytest.raises(AuthenticationFailed, match="LOCAL_COMPLETION_WEIGHTS_REQUIRED"):
            await adapter.complete(_request(LOCAL_MODEL))

    async def test_an_unconfirmed_context_capacity_is_refused(self):
        show = _show(model_info={"general.architecture": "llama", "llama.context_length": 1024})
        adapter, _ = self._adapter(
            {("GET", "/api/tags"): (200, _tags()), ("POST", "/api/show"): (200, show)}
        )
        with pytest.raises(AuthenticationFailed, match="LOCAL_CONTEXT_CAPACITY_NOT_CONFIRMED"):
            await adapter.complete(_request(LOCAL_MODEL))

    async def test_an_oversized_task_is_refused(self):
        adapter, _ = self._adapter(
            {("GET", "/api/tags"): (200, _tags()), ("POST", "/api/show"): (200, _show())}
        )
        with pytest.raises(MalformedResponse, match="CONTEXT_OR_OUTPUT_BUDGET_EXCEEDED"):
            await adapter.complete(_request(LOCAL_MODEL, task="x" * CONTEXT))

    @pytest.mark.parametrize(
        "overrides",
        [
            {"done": False},
            {"done_reason": "cancelled"},
            {"model": "other"},
            {"message": {"role": "user", "content": "pong"}},
            {"message": {"role": "assistant", "content": "   "}},
            {"message": {"role": "assistant", "content": 1}},
            {"message": {"role": "assistant", "content": "p", "tool_calls": [{"a": 1}]}},
        ],
    )
    async def test_a_malformed_local_completion_is_refused(self, overrides):
        adapter, _ = self._adapter(
            {
                ("GET", "/api/tags"): (200, _tags()),
                ("POST", "/api/show"): (200, _show()),
                ("POST", "/api/chat"): (200, _chat(**overrides)),
            }
        )
        with pytest.raises(MalformedResponse, match="INVALID_LOCAL_COMPLETION"):
            await adapter.complete(_request(LOCAL_MODEL))

    async def test_a_revision_mismatch_drops_the_model(self):
        spec = _local_spec()
        spec.models[0] = spec.models[0].model_copy(update={"model_revision": "aaa111"})
        adapter, _ = self._adapter({("GET", "/api/tags"): (200, _tags(digest="bbb222"))}, spec=spec)
        assert await adapter.list_models() == []

    async def test_a_matching_revision_keeps_the_model(self):
        spec = _local_spec()
        spec.models[0] = spec.models[0].model_copy(update={"model_revision": "aaa111"})
        adapter, _ = self._adapter({("GET", "/api/tags"): (200, _tags(digest="aaa111"))}, spec=spec)
        assert [model.model_id for model in await adapter.list_models()] == [LOCAL_MODEL]


class TestGeminiStreamAssembly:
    """Gemini is the one route whose descriptor already allows a large budget."""

    def _adapter(self, events, transport_routes=None):
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        routes = transport_routes or {}
        routes[("GET", GEMINI_MODEL)] = (200, _metadata())
        routes[("POST", ":streamGenerateContent")] = lambda request: httpx.Response(
            200, content=text.encode(), headers={"content-type": "text/event-stream"}
        )
        transport, seen = _transport(routes)
        return (
            GeminiAdapter(
                _gemini_spec(), _settings(), credential=SecretStr("k"), transport=transport
            ),
            seen,
        )

    def _chunk(self, text=None, finish=None, thought=False):
        part = {"text": text} if not thought else {"text": text, "thought": True}
        candidate = {"content": {"role": "model", "parts": [part] if text is not None else []}}
        if finish is not None:
            candidate["finishReason"] = finish
        return {"modelVersion": GEMINI_MODEL, "candidates": [candidate]}

    async def test_streamed_fragments_are_joined(self):
        adapter, _ = self._adapter(
            [self._chunk("Hel"), self._chunk("lo"), self._chunk(finish="STOP")]
        )
        response = await adapter.complete(_request(GEMINI_MODEL))
        assert response.text == "Hello"
        assert response.finish_reason == "stop"

    async def test_the_streaming_endpoint_is_the_one_called(self):
        adapter, seen = self._adapter([self._chunk("x"), self._chunk(finish="STOP")])
        await adapter.complete(_request(GEMINI_MODEL))
        assert any(":streamGenerateContent" in str(r.url) for r in seen)

    async def test_thousands_of_fragments_collapse_below_the_part_bound(self):
        """One part per chunk would trip the 4096-part envelope bound on length alone."""
        events = [self._chunk("a") for _ in range(5000)] + [self._chunk(finish="STOP")]
        adapter, _ = self._adapter(events)
        assert (await adapter.complete(_request(GEMINI_MODEL))).text == "a" * 5000

    async def test_thought_fragments_are_still_excluded_from_the_answer(self):
        events = [
            self._chunk("secret", thought=True),
            self._chunk("visible"),
            self._chunk(finish="STOP"),
        ]
        adapter, _ = self._adapter(events)
        assert (await adapter.complete(_request(GEMINI_MODEL))).text == "visible"

    async def test_a_stream_of_only_thoughts_is_refused(self):
        events = [self._chunk("secret", thought=True), self._chunk(finish="STOP")]
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INVALID_GEMINI_COMPLETION"):
            await adapter.complete(_request(GEMINI_MODEL))

    async def test_a_blocked_prompt_is_refused(self):
        events = [
            {"modelVersion": GEMINI_MODEL, "promptFeedback": {"blockReason": "SAFETY"}},
            self._chunk("x"),
            self._chunk(finish="STOP"),
        ]
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INVALID_GEMINI_COMPLETION"):
            await adapter.complete(_request(GEMINI_MODEL))

    async def test_a_stream_with_two_candidates_is_refused(self):
        events = [
            {"modelVersion": GEMINI_MODEL, "candidates": [{}, {}]},
            self._chunk(finish="STOP"),
        ]
        adapter, _ = self._adapter(events)
        with pytest.raises(MalformedResponse, match="INVALID_GEMINI_COMPLETION"):
            await adapter.complete(_request(GEMINI_MODEL))
