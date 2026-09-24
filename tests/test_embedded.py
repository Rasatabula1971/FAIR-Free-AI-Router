"""Tests for the embedded FAIR module — no database, no server."""


import pytest

from fair.config import RoutingSettings
from fair.embedded import FAIR
from fair.embedded.performance import MemoryPerformanceRegistry
from fair.embedded.quota import MemoryQuotaGovernor
from fair.embedded.router import EmbeddedRouter
from fair.providers.base import (
    BillingViolation,
)
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.schemas.domain import ProviderSpec

# ── helpers ──────────────────────────────────────────────────────────────


def _spec(name="a", **overrides):
    values = dict(
        provider_id=name,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        models=[{
            "model_id": "model",
            "context_window": 32768,
            "capabilities": {"reasoning", "coding", "structured_output"},
        }],
    )
    return ProviderSpec(**(values | overrides))


def _thresholds():
    return {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}


def _router(entries=None, on_event=None, **settings_kw):
    registry = Registry()
    for spec, adapter in entries or [(_spec(), MockAdapter("a"))]:
        registry.register(spec, adapter)
    settings = RoutingSettings(**settings_kw)
    return EmbeddedRouter(registry, settings, _thresholds(), on_event=on_event)


# ── MemoryQuotaGovernor ──────────────────────────────────────────────────


class TestMemoryQuota:
    def test_reserve_and_success(self):
        gov = MemoryQuotaGovernor(RoutingSettings())
        spec = _spec(request_limit=100)
        assert gov.available(spec)
        assert gov.reserve(spec)
        assert gov.remaining(spec) == 99
        gov.success(spec.provider_id)
        assert gov.state(spec.provider_id).circuit_state == "CLOSED"

    def test_exhaust(self):
        gov = MemoryQuotaGovernor(RoutingSettings())
        spec = _spec(request_limit=100)
        gov.exhaust(spec.provider_id)
        assert not gov.available(spec)
        assert gov.effective_status(spec) == "QUOTA_EXHAUSTED"

    def test_circuit_breaker(self):
        gov = MemoryQuotaGovernor(RoutingSettings(circuit_failures=2, cooldown_seconds=60))
        spec = _spec(request_limit=100)
        gov.failure(spec.provider_id)
        gov.failure(spec.provider_id)
        assert gov.state(spec.provider_id).circuit_state == "OPEN"
        assert not gov.available(spec)

    def test_security_block(self):
        gov = MemoryQuotaGovernor(RoutingSettings())
        spec = _spec(request_limit=100)
        gov.block_security(spec.provider_id)
        assert not gov.available(spec)
        assert gov.effective_status(spec) == "SECURITY_BLOCKED"

    def test_throttle(self):
        gov = MemoryQuotaGovernor(RoutingSettings(cooldown_seconds=10))
        spec = _spec(request_limit=100)
        gov.throttle(spec.provider_id, retry_after=5)
        assert gov.effective_status(spec) == "THROTTLED"

    def test_reset_recovers(self):
        now = 1000.0
        gov = MemoryQuotaGovernor(RoutingSettings(), clock=lambda: now)
        spec = _spec(request_limit=100)
        gov.exhaust(spec.provider_id, reset_at=1010.0)
        assert not gov.available(spec)
        now = 1011.0
        assert gov.available(spec)
        assert gov.remaining(spec) == 100


# ── MemoryPerformanceRegistry ────────────────────────────────────────────


class TestMemoryPerformance:
    def test_default_scores(self):
        perf = MemoryPerformanceRegistry()
        q, r = perf.scores("x", "m", "general")
        assert q == 0.5
        assert r == 0.5

    def test_record_updates_scores(self):
        from fair.schemas.domain import Attempt, QualityReport

        perf = MemoryPerformanceRegistry()
        attempt = Attempt(
            attempt_number=1,
            provider_id="x",
            model_id="m",
            selection_score=0.5,
            quota_remaining=100,
            disposition="ACCEPTED",
            latency_ms=200,
            quality=QualityReport(overall_score=100.0),
        )
        perf.record(attempt, "general")
        q, r = perf.scores("x", "m", "general")
        assert q > 0.5
        assert r > 0.5


# ── EmbeddedRouter ───────────────────────────────────────────────────────


class TestEmbeddedRouter:
    @pytest.mark.asyncio
    async def test_arithmetic_accepted(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text="345"))])
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ACCEPTED"
        assert result.output == "345"
        assert result.quality.verification_state == "DETERMINISTIC_ARITHMETIC"
        assert result.quality.overall_score == 100.0

    @pytest.mark.asyncio
    async def test_arithmetic_rejected(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text="999"))])
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.output is None

    @pytest.mark.asyncio
    async def test_code_validation_accepted(self):
        code = "def add(a, b):\n    return a + b"
        router = _router(entries=[(_spec(), MockAdapter("a", text=code))])
        result = await router.solve(
            _request(
                task="add two numbers",
                validation={
                    "kind": "python_function",
                    "function_name": "add",
                    "cases": [
                        {"arguments": [1, 2], "expected": 3},
                        {"arguments": [0, 0], "expected": 0},
                    ],
                },
            )
        )
        assert result.status == "ACCEPTED"
        assert result.quality.verification_state == "BOUNDED_CODE_TESTS"

    @pytest.mark.asyncio
    async def test_code_validation_rejected(self):
        code = "def add(a, b):\n    return a * b"
        router = _router(entries=[(_spec(), MockAdapter("a", text=code))])
        result = await router.solve(
            _request(
                task="add two numbers",
                validation={
                    "kind": "python_function",
                    "function_name": "add",
                    "cases": [{"arguments": [1, 2], "expected": 3}],
                },
            )
        )
        assert result.status == "ESCALATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_reference_json_accepted(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"x": 1}'))])
        result = await router.solve(
            _request(
                task="return json",
                validation={"kind": "reference_json", "expected": {"x": 1}},
            )
        )
        assert result.status == "ACCEPTED"
        assert result.quality.verification_state == "HOST_REFERENCE_MATCH"

    @pytest.mark.asyncio
    async def test_schema_only_accepted_at_standard(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"items": [1, 2]}'))])
        result = await router.solve(
            _request(
                task="list two items",
                expected_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {"items": {"type": "array"}},
                },
            )
        )
        assert result.status == "ACCEPTED"
        assert result.output == '{"items": [1, 2]}'
        assert result.verification_state == "STRUCTURE_VALIDATED"
        assert result.best_quality_score == 85.0

    @pytest.mark.asyncio
    async def test_schema_only_escalated_at_advanced(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"items": []}'))])
        result = await router.solve(
            _request(
                task="list items",
                quality_level="advanced",
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.output is None
        assert result.attempts[0].quality.verification_state == "STRUCTURE_VALIDATED"

    @pytest.mark.asyncio
    async def test_schema_mismatch_rejected(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"wrong": 1}'))])
        result = await router.solve(
            _request(task="list items", expected_schema={"type": "object", "required": ["items"]})
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.attempts[0].quality.hard_reject
        assert "SCHEMA_FAILURE" in result.attempts[0].quality.reject_reasons

    @pytest.mark.asyncio
    async def test_schema_only_not_accepted_when_task_needs_coding(self):
        # The profiler infers a coding requirement; a schema check cannot vouch for code.
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"items": []}'))])
        result = await router.solve(
            _request(
                task="list the python functions",
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        checks = result.attempts[0].quality.validator_results
        assert checks["coverage"] == "TASK_VALIDATOR_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_explicit_task_type_disables_keyword_inference(self):
        # The task embeds third-party text mentioning python and sources; the
        # caller declared an extraction task, so neither coding nor grounding
        # is inferred and the schema-validated answer is accepted.
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"items": []}'))])
        result = await router.solve(
            _request(
                task="Extract the problems from this comment: "
                "'my python script breaks, see the sources in the README'",
                task_type="extraction",
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ACCEPTED"
        assert result.verification_state == "STRUCTURE_VALIDATED"

    @pytest.mark.asyncio
    async def test_explicit_coding_task_type_still_requires_coding(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"items": []}'))])
        result = await router.solve(
            _request(
                task="list them",
                task_type="coding",
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        checks = result.attempts[0].quality.validator_results
        assert checks["coverage"] == "TASK_VALIDATOR_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_provider_failure_retries(self):
        spec_a = _spec("a")
        spec_b = _spec("b")
        mock_a = MockAdapter("a", error=Exception("down"))
        mock_b = MockAdapter("b", text="345")
        router = _router(
            entries=[(spec_a, mock_a), (spec_b, mock_b)],
            max_attempts=3,
        )
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ACCEPTED"
        assert result.provider_id == "b"

    @pytest.mark.asyncio
    async def test_unanswered_providers_do_not_spend_the_answer_budget(self):
        """Three providers down (timeouts, 503s, connection errors) used to end
        the search at max_attempts=3 with healthy models untried, reported as
        ALL_FREE_MODELS_UNAVAILABLE as if every model had been asked."""
        entries = [
            (_spec(f"down{i}"), MockAdapter(f"down{i}", error=TimeoutError())) for i in range(4)
        ] + [(_spec("zz-healthy"), MockAdapter("zz-healthy", text="345"))]
        router = _router(entries=entries, max_attempts=1, max_unanswered_attempts=6)
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ACCEPTED"
        assert result.provider_id == "zz-healthy"
        dispositions = [a.disposition for a in result.attempts]
        assert dispositions.count("INFRA_FAILURE") == 4 and dispositions[-1] == "ACCEPTED"

    @pytest.mark.asyncio
    async def test_unanswered_budget_still_bounds_a_fleet_wide_outage(self):
        entries = [
            (_spec(f"down{i}"), MockAdapter(f"down{i}", error=TimeoutError())) for i in range(5)
        ] + [(_spec("zz-healthy"), MockAdapter("zz-healthy", text="345"))]
        router = _router(entries=entries, max_attempts=3, max_unanswered_attempts=2)
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.reason_code == "ALL_FREE_MODELS_UNAVAILABLE"
        # Two unanswered attempts tolerated, the third ends the search.
        assert len(result.attempts) == 3
        assert all(a.disposition == "INFRA_FAILURE" for a in result.attempts)

    @pytest.mark.asyncio
    async def test_answered_budget_is_unchanged_by_unanswered_attempts(self):
        """Three quality failures still escalate at max_attempts=3, exactly as before."""
        entries = [(_spec(f"bad{i}"), MockAdapter(f"bad{i}", text="wrong")) for i in range(4)]
        router = _router(entries=entries, max_attempts=3)
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.reason_code == "ALL_FREE_MODELS_FAILED_QUALITY"
        assert len(result.attempts) == 3

    @pytest.mark.asyncio
    async def test_attempt_records_why_it_failed(self):
        from fair.providers.base import ProviderUnavailable

        class Boom(Exception):
            def __str__(self):
                return "https://api.example/v1?key=SECRET"

        entries = [
            (_spec("a-timeout"), MockAdapter("a-timeout", error=TimeoutError())),
            (_spec("b-http"), MockAdapter("b-http", error=ProviderUnavailable("HTTP_503"))),
            (_spec("c-boom"), MockAdapter("c-boom", error=Boom())),
        ]
        router = _router(entries=entries, max_attempts=1, max_unanswered_attempts=3)
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        details = {a.provider_id: (a.error_type, a.error_detail) for a in result.attempts}
        assert details["a-timeout"] == ("PROVIDER_UNAVAILABLE", "TimeoutError")
        assert details["b-http"] == ("PROVIDER_UNAVAILABLE", "HTTP_503")
        # An arbitrary exception's text may carry a credential: class name only.
        assert details["c-boom"] == ("PROVIDER_UNAVAILABLE", "Boom")
        assert "SECRET" not in result.model_dump_json()

    @pytest.mark.asyncio
    async def test_fenced_json_answer_passes_the_schema_check(self):
        """Free models wrap JSON in a markdown fence even when told not to; the
        document inside is what the schema is about. Scoring it 0 discarded
        otherwise-correct answers and escalated."""
        router = _router(
            entries=[(_spec(), MockAdapter("a", text='```json\n{"items": [1, 2]}\n```'))]
        )
        result = await router.solve(
            _request(
                task="list them",
                task_type="extraction",
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ACCEPTED"
        assert result.attempts[0].quality.validator_results["schema"] == "PASS"

    @pytest.mark.asyncio
    async def test_prose_around_json_is_still_a_schema_failure(self):
        router = _router(
            entries=[(_spec(), MockAdapter("a", text='Here you go:\n{"items": []}\nEnjoy!'))]
        )
        result = await router.solve(
            _request(
                task="list them",
                task_type="extraction",
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.attempts[0].quality.validator_results["schema"] == "FAIL"

    @pytest.mark.asyncio
    async def test_billing_violation_stops_system(self):
        router = _router(entries=[(_spec(), MockAdapter("a", error=BillingViolation()))])
        with pytest.raises(BillingViolation):
            await router.solve(_request(task="anything"))
        assert router.stopped

    @pytest.mark.asyncio
    async def test_on_event_callback(self):
        events = []
        router = _router(
            entries=[(_spec(), MockAdapter("a", text="345"))],
            on_event=lambda t, p: events.append(t),
        )
        await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert "PROFILED" in events
        assert "ATTEMPT_COMPLETED" in events
        assert "ACCEPTED" in events

    @pytest.mark.asyncio
    async def test_stopped_router_fails(self):
        router = _router()
        router.stopped = True
        result = await router.solve(_request(task="anything"))
        assert result.status != "ACCEPTED"

    @pytest.mark.asyncio
    async def test_empty_response_rejected(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text="   "))])
        result = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result.status == "ESCALATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_cross_check_agreement(self):
        spec_a = _spec("a", models=[{"model_id": "m1", "context_window": 32768}])
        spec_b = _spec("b", models=[{"model_id": "m2", "context_window": 32768}])
        router = _router(
            entries=[
                (spec_a, MockAdapter("a", text="345")),
                (spec_b, MockAdapter("b", text="345")),
            ],
        )
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
                cross_check_required=True,
            )
        )
        assert result.status == "ACCEPTED"
        assert result.cross_check.state == "PASSED"

    @pytest.mark.asyncio
    async def test_cross_check_disagreement(self):
        spec_a = _spec("a", models=[{"model_id": "m1", "context_window": 32768}])
        spec_b = _spec("b", models=[{"model_id": "m2", "context_window": 32768}])
        router = _router(
            entries=[
                (spec_a, MockAdapter("a", text="345")),
                (spec_b, MockAdapter("b", text="999")),
            ],
        )
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
                cross_check_required=True,
            )
        )
        assert result.status != "ACCEPTED"
        assert result.cross_check.state == "DISAGREEMENT"
        assert result.model_disagreement == "DETECTED"


# ── Cache ────────────────────────────────────────────────────────────────


class TestMemoryCache:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        router = _router(
            entries=[(_spec(), MockAdapter("a", text="345"))],
            cache_enabled=True,
        )
        request = _request(
            task="15*23",
            validation={"kind": "arithmetic", "expression": "15*23"},
        )
        result1 = await router.solve(request)
        assert result1.status == "ACCEPTED"
        assert not result1.cache_hit
        result2 = await router.solve(request)
        assert result2.status == "ACCEPTED"
        assert result2.cache_hit

    @pytest.mark.asyncio
    async def test_cache_bypass(self):
        router = _router(
            entries=[(_spec(), MockAdapter("a", text="345"))],
            cache_enabled=True,
        )
        result1 = await router.solve(
            _request(task="15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        )
        assert result1.status == "ACCEPTED"
        result2 = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
                cache_mode="bypass",
            )
        )
        assert not result2.cache_hit


# ── FAIR entry point ─────────────────────────────────────────────────────


class TestFAIRModule:
    def test_no_providers_raises(self):
        with pytest.raises(ValueError, match="at least one provider"):
            FAIR()

    def test_mock_provider(self):
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        fair = FAIR(providers=[(spec, adapter)])
        assert len(fair.providers()) == 1
        assert fair.providers()[0]["provider_id"] == "a"

    @pytest.mark.asyncio
    async def test_solve_with_mock(self):
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        async with FAIR(providers=[(spec, adapter)]) as fair:
            result = await fair.solve(
                "What is 15*23?",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
            assert result.status == "ACCEPTED"
            assert result.output == "345"

    @pytest.mark.asyncio
    async def test_quality_level_override(self):
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        fair = FAIR(providers=[(spec, adapter)], quality_level="commodity")
        result = await fair.solve(
            "15*23", validation={"kind": "arithmetic", "expression": "15*23"}
        )
        assert result.status == "ACCEPTED"
        assert result.minimum_required == 75

    @pytest.mark.asyncio
    async def test_stop_and_resume(self):
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        fair = FAIR(providers=[(spec, adapter)])
        fair.stopped = True
        result = await fair.solve("anything")
        assert result.status != "ACCEPTED"
        fair.stopped = False
        result = await fair.solve(
            "15*23", validation={"kind": "arithmetic", "expression": "15*23"}
        )
        assert result.status == "ACCEPTED"

    @pytest.mark.asyncio
    async def test_clear_cache(self):
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        fair = FAIR(providers=[(spec, adapter)], cache_enabled=True)
        await fair.solve("15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        result = fair.clear_cache()
        assert result["entries_removed"] >= 0

    @pytest.mark.asyncio
    async def test_event_callback(self):
        events = []
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        fair = FAIR(
            providers=[(spec, adapter)],
            on_event=lambda t, p: events.append(t),
        )
        await fair.solve("15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        assert len(events) > 0


# ── helpers ──────────────────────────────────────────────────────────────


def _request(task="test", **kwargs):
    from fair.schemas.api import SolveRequest

    defaults = dict(client_id="test", task=task, quality_level="standard")
    return SolveRequest(**(defaults | kwargs))
