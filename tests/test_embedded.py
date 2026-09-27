"""Tests for the embedded FAIR module — no database, no server."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from fair.config import RoutingSettings
from fair.embedded import FAIR, module
from fair.embedded.module import _CLOUD_PROVIDERS
from fair.embedded.performance import MemoryPerformanceRegistry
from fair.embedded.quota import MemoryQuotaGovernor, SharedQuotaLedger
from fair.embedded.router import EmbeddedRouter
from fair.providers.base import (
    AccessDenied,
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ProviderUnavailable,
    RequestNotSupported,
)
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.thresholds import validate_thresholds
from fair.schemas.api import SolveRequest
from fair.schemas.domain import ProviderSpec, QuotaSnapshot
from fair.security.adapter import CredentialedAdapter


def _now():
    return datetime.now(UTC) - timedelta(seconds=10)


def _aged():
    """A built-in review date old enough to fall outside the 29-day window."""
    return datetime.now(UTC) - timedelta(days=90)


# ── helpers ──────────────────────────────────────────────────────────────


def _spec(name="a", **overrides):
    # A request_limit without a reset window is rejected by ProviderSpec, since
    # a counter that never clears permanently disables the provider. Tests that
    # only care about the ceiling get a window for free; a test about the window
    # itself passes its own.
    if "request_limit" in overrides:
        overrides.setdefault("request_limit_window", "DAILY_UTC")
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
        models=[
            {
                "model_id": "model",
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
        ],
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


# ── Configuration / policy hardening ─────────────────────────────────────


class TestConfigurationHardening:
    def test_dotenv_supports_export_and_inline_comments(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "export GROQ_API_KEY=abc123 # local note\nOPENROUTER_API_KEY='quoted # value'\n",
            encoding="utf-8",
        )
        values = module._read_env_file(path)
        assert values["GROQ_API_KEY"] == "abc123"
        assert values["OPENROUTER_API_KEY"] == "quoted # value"

    def test_bare_ollama_host_is_normalized_to_loopback_http(self):
        assert module._loopback("127.0.0.1:11434") == "http://127.0.0.1:11434"
        assert module._loopback("localhost:11434") == "http://127.0.0.1:11434"

    def test_selector_weights_must_sum_to_one(self):
        with pytest.raises(ValueError, match="Selector weights must sum to 1.0"):
            RoutingSettings(
                quality_weight=0.7,
                quota_weight=0.2,
                reliability_weight=0.2,
            )

    def test_selector_weights_are_bounded(self):
        with pytest.raises(ValueError):
            RoutingSettings(
                quality_weight=1.1,
                quota_weight=0.0,
                reliability_weight=0.0,
            )

    def test_quality_threshold_map_must_be_complete(self):
        incomplete = {
            "commodity": 75,
            "standard": 82,
            "advanced": 88,
        }
        with pytest.raises(ValueError, match="All quality levels required"):
            validate_thresholds(incomplete)

    def test_duplicate_provider_is_reported_before_admission(self):
        registry = Registry()
        registry.register(_spec(), MockAdapter("a"))

        invalid_duplicate = _spec(requires_paid_subscription=True)
        with pytest.raises(ValueError, match="Duplicate provider"):
            registry.register_credentialed(
                invalid_duplicate,
                lambda credential: MockAdapter("a"),
                None,
            )


class TestCredentialGuardHardening:
    def _guard(self):
        return CredentialedAdapter(
            "a",
            MockAdapter("a"),
            SecretStr("secret-token"),
        )

    def test_bytes_are_scanned_for_credential_leakage(self):
        guard = self._guard()
        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_EXPOSURE_BLOCKED"):
            guard._check(b"prefix-secret-token-suffix")

    def test_deeply_nested_values_fail_closed(self):
        guard = self._guard()
        value = "safe"
        for _ in range(34):
            value = [value]

        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_CHECK_DEPTH_EXCEEDED"):
            guard._check(value)


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

    def test_provider_success_does_not_erase_retry_after_throttle(self):
        now = [1000.0]
        gov = MemoryQuotaGovernor(
            RoutingSettings(cooldown_seconds=10),
            clock=lambda: now[0],
        )
        spec = _spec(request_limit=100)
        gov.throttle(spec.provider_id, retry_after=30)
        gov.success(spec.provider_id)
        assert gov.effective_status(spec) == "THROTTLED"
        now[0] = 1031.0
        assert gov.effective_status(spec) == "ACTIVE"

    def test_exhaust_without_reset_recovers_instead_of_sticking_forever(self):
        now = [1000.0]
        gov = MemoryQuotaGovernor(RoutingSettings(), clock=lambda: now[0])
        spec = _spec()
        gov.exhaust(spec)
        assert not gov.available(spec)
        now[0] = 4601.0
        assert gov.available(spec)

    def test_reset_recovers(self):
        now = 1000.0
        gov = MemoryQuotaGovernor(RoutingSettings(), clock=lambda: now)
        spec = _spec(request_limit=100)
        gov.exhaust(spec.provider_id, reset_at=1010.0)
        assert not gov.available(spec)
        now = 1011.0
        assert gov.available(spec)
        assert gov.remaining(spec) == 100


# ── SharedQuotaLedger ────────────────────────────────────────────────────


class TestSharedQuotaLedger:
    def _governor(self, path, application_id, *, clock=None, quota_pool_ids=None):
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        return MemoryQuotaGovernor(
            RoutingSettings(),
            shared_ledger=SharedQuotaLedger(path),
            application_id=application_id,
            quota_pool_ids=quota_pool_ids,
            **kwargs,
        )

    def test_two_apps_share_one_account_limit(self, tmp_path):
        path = tmp_path / "quota.sqlite3"
        spec = _spec(request_limit=2)
        corp = self._governor(path, "corp")
        video = self._governor(path, "video")

        assert corp.reserve(spec)
        assert video.reserve(spec)
        assert not corp.reserve(spec)
        assert corp.remaining(spec) == 0
        assert video.remaining(spec) == 0

        report = corp.usage_report([spec])
        assert report["shared"] is True
        assert report["application_id"] == "corp"
        assert report["provider_pools"] == {"a": "a"}
        assert report["pools"][0]["used"] == 2
        assert report["pools"][0]["remaining"] == 0
        assert report["pools"][0]["applications"] == {"corp": 1, "video": 1}

    def test_separate_account_pool_ids_do_not_share_allowance(self, tmp_path):
        path = tmp_path / "quota.sqlite3"
        spec = _spec(request_limit=1)
        account_one = self._governor(path, "corp", quota_pool_ids={"a": "account-one"})
        account_two = self._governor(path, "video", quota_pool_ids={"a": "account-two"})

        assert account_one.reserve(spec)
        assert not account_one.reserve(spec)
        assert account_two.reserve(spec)
        assert account_one.pool_id("a") == "account-one"
        assert account_two.pool_id("a") == "account-two"

    def test_security_block_is_local_to_the_key_not_shared(self, tmp_path):
        path = tmp_path / "quota.sqlite3"
        spec = _spec(request_limit=5)
        corp = self._governor(path, "corp")
        video = self._governor(path, "video")

        corp.block_security("a")
        assert not corp.available(spec)
        assert video.available(spec)

    def test_shared_reset_clears_usage_for_all_apps(self, tmp_path):
        path = tmp_path / "quota.sqlite3"
        now = [datetime(2026, 9, 27, 12, tzinfo=UTC).timestamp()]
        spec = _spec(request_limit=2, request_limit_window="DAILY_UTC")
        corp = self._governor(path, "corp", clock=lambda: now[0])
        video = self._governor(path, "video", clock=lambda: now[0])

        assert corp.reserve(spec)
        now[0] += 2 * 86400
        assert video.available(spec)
        assert video.remaining(spec) == 2
        pool = video.usage_report([spec])["pools"][0]
        assert pool["used"] == 0
        assert pool["applications"] == {}

    def test_known_exhaustion_propagates_across_apps(self, tmp_path):
        path = tmp_path / "quota.sqlite3"
        now = [1000.0]
        spec = _spec(request_limit=5)
        corp = self._governor(path, "corp", clock=lambda: now[0])
        video = self._governor(path, "video", clock=lambda: now[0])

        corp.exhaust(spec, reset_at=1100.0)
        assert not video.available(spec)
        assert video.effective_status(spec) == "QUOTA_EXHAUSTED"
        now[0] = 1101.0
        assert video.available(spec)

    def test_provider_observation_updates_the_shared_pool(self, tmp_path):
        path = tmp_path / "quota.sqlite3"
        now = [1000.0]
        spec = _spec(request_limit=5)
        corp = self._governor(path, "corp", clock=lambda: now[0])
        video = self._governor(path, "video", clock=lambda: now[0])

        corp.observe(
            spec,
            QuotaSnapshot(
                provider_id="a",
                quota_limit=5,
                quota_remaining_estimate=0,
                reset_at=1100.0,
            ),
        )
        assert not video.available(spec)
        assert video.remaining(spec) == 0

    def test_zero_remaining_without_reset_is_not_persisted_forever(self, tmp_path):
        ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
        ledger.observe("a", 5, 0, None, 1000.0)
        assert ledger.available("a", None, 1001.0)
        pool = ledger.report(["a"], 1001.0)[0]
        assert pool["used"] == 5
        assert pool["exhausted"] is False

    def test_ledger_failure_fails_closed_instead_of_crashing(self):
        import sqlite3

        class BrokenLedger:
            path = "broken.sqlite3"

            def available(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def remaining(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def report(self, *args):
                raise sqlite3.OperationalError("database is locked")

        spec = _spec(request_limit=5)
        gov = MemoryQuotaGovernor(
            RoutingSettings(),
            shared_ledger=BrokenLedger(),
            application_id="corp",
        )
        assert gov.available(spec) is False
        assert gov.remaining(spec) == 0
        report = gov.usage_report([spec])
        assert report["ledger_available"] is False
        assert report["pools"] == []

    def test_invalid_application_identity_fails_closed(self, tmp_path):
        with pytest.raises(ValueError, match="application id"):
            self._governor(tmp_path / "quota.sqlite3", "")


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
    async def test_billing_violation_blocks_only_the_offending_provider(self):
        router = _router(
            entries=[
                (_spec("a"), MockAdapter("a", error=BillingViolation())),
                (_spec("b"), MockAdapter("b", text="345")),
            ]
        )
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
        )
        assert result.status == "ACCEPTED"
        assert result.provider_id == "b"
        assert router.stopped is False
        assert router.quota.state("a").security_blocked is True
        assert router.quota.state("b").security_blocked is False

    @pytest.mark.asyncio
    async def test_request_not_supported_does_not_damage_provider_performance(self):
        router = _router(
            entries=[
                (_spec(), MockAdapter("a", error=RequestNotSupported("OUTPUT_BUDGET_INVALID")))
            ]
        )
        result = await router.solve(_request(task="anything"))
        assert result.attempts[0].disposition == "CAPABILITY_MISMATCH"
        assert result.attempts[0].error_type == "REQUEST_NOT_SUPPORTED_BY_ROUTE"
        assert router.performance._stats == {}

    @pytest.mark.asyncio
    async def test_access_denied_is_not_treated_as_bad_credentials(self):
        router = _router(
            entries=[
                (_spec("a"), MockAdapter("a", error=AccessDenied("PROVIDER_ACCESS_DENIED"))),
                (_spec("b"), MockAdapter("b", text="345")),
            ],
            cooldown_seconds=1,
        )
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
        )
        assert result.status == "ACCEPTED"
        assert router.quota.state("a").security_blocked is False
        assert router.quota.effective_status(_spec("a")) == "THROTTLED"

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
    async def test_cross_check_reservation_race_tries_next_independent_model(self):
        spec_a = _spec("a", models=[{"model_id": "m1", "context_window": 32768}])
        spec_b = _spec("b", models=[{"model_id": "m2", "context_window": 32768}])
        spec_c = _spec("c", models=[{"model_id": "m3", "context_window": 32768}])
        adapter_a = MockAdapter("a", text="345")
        adapter_b = MockAdapter("b", text="345")
        adapter_c = MockAdapter("c", text="345")
        router = _router(
            entries=[
                (spec_a, adapter_a),
                (spec_b, adapter_b),
                (spec_c, adapter_c),
            ],
            max_verification_attempts=2,
        )

        original_reserve = router.quota.reserve_async
        lost_once = False

        async def reserve(spec, application_id=None):
            nonlocal lost_once
            if spec.provider_id == "b" and not lost_once:
                lost_once = True
                return False
            return await original_reserve(spec, application_id)

        router.quota.reserve_async = reserve
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
                cross_check_required=True,
            )
        )

        assert result.status == "ACCEPTED"
        assert result.cross_check.state == "PASSED"
        assert adapter_b.calls == 0
        assert adapter_c.calls == 1

    @pytest.mark.asyncio
    async def test_cross_check_accepts_equivalent_fenced_json_documents(self):
        spec_a = _spec("a", models=[{"model_id": "m1", "context_window": 32768}])
        spec_b = _spec("b", models=[{"model_id": "m2", "context_window": 32768}])
        router = _router(
            entries=[
                (spec_a, MockAdapter("a", text='```json\n{"answer": 345}\n```')),
                (spec_b, MockAdapter("b", text='{"answer":345}')),
            ]
        )
        result = await router.solve(
            _request(
                task="return JSON",
                expected_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "integer"}},
                    "required": ["answer"],
                },
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
        with pytest.raises(ValueError, match="at least one safely eligible provider"):
            FAIR()

    def test_mock_provider(self):
        spec = _spec()
        adapter = MockAdapter("a", text="345")
        fair = FAIR(providers=[(spec, adapter)])
        assert len(fair.providers()) == 1
        assert fair.providers()[0]["provider_id"] == "a"

    def test_shared_quota_configuration_is_exposed(self, tmp_path):
        spec = _spec(request_limit=2)
        fair = FAIR(
            providers=[(spec, MockAdapter("a"))],
            application_id="corp",
            shared_quota_path=str(tmp_path / "quota.sqlite3"),
        )
        provider = fair.providers()[0]
        assert provider["quota_pool_id"] == "a"
        assert provider["quota_remaining"] == 2
        usage = fair.quota_usage()
        assert usage["shared"] is True
        assert usage["application_id"] == "corp"

    def test_unknown_quota_pool_provider_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown quota_pool_ids provider"):
            FAIR(
                providers=[(_spec(), MockAdapter("a"))],
                shared_quota_path=str(tmp_path / "quota.sqlite3"),
                quota_pool_ids={"missing": "account"},
            )

    @pytest.mark.asyncio
    async def test_application_id_is_default_quota_attribution(self, tmp_path):
        fair = FAIR(
            providers=[(_spec(request_limit=2), MockAdapter("a", text="345"))],
            application_id="corp",
            shared_quota_path=str(tmp_path / "quota.sqlite3"),
        )
        await fair.solve("15*23", validation={"kind": "arithmetic", "expression": "15*23"})
        pool = fair.quota_usage()["pools"][0]
        assert pool["applications"] == {"corp": 1}

    @pytest.mark.asyncio
    async def test_explicit_client_id_overrides_quota_attribution(self, tmp_path):
        fair = FAIR(
            providers=[(_spec(request_limit=2), MockAdapter("a", text="345"))],
            application_id="fair-service",
            shared_quota_path=str(tmp_path / "quota.sqlite3"),
        )
        await fair.solve(
            "15*23",
            validation={"kind": "arithmetic", "expression": "15*23"},
            client_id="youtube-production",
        )
        pool = fair.quota_usage()["pools"][0]
        assert pool["applications"] == {"youtube-production": 1}

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
        result = await fair.solve("15*23", validation={"kind": "arithmetic", "expression": "15*23"})
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
        result = await fair.solve("15*23", validation={"kind": "arithmetic", "expression": "15*23"})
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


# ── Privacy classification (facade must be able to use it) ───────────────


class TestPrivacyRouting:
    """privacy_class is only a control if the public API can set it.

    The selector has always enforced it, and remote adapters have always
    refused a non-PUBLIC data class. What was missing was any way for an
    application calling FAIR.solve() to say the task was not public, so every
    task was implicitly PUBLIC.
    """

    def _public_only(self):
        """Stands in for any remote provider: PUBLIC is the ProviderSpec default."""
        return _spec("public_only")

    def _restricted_ok(self):
        return _spec("restricted_ok", max_data_class="RESTRICTED")

    @pytest.mark.asyncio
    async def test_confidential_task_never_reaches_a_public_only_provider(self):
        public = MockAdapter("public_only", text="345")
        async with FAIR(providers=[(self._public_only(), public)]) as fair:
            result = await fair.solve(
                "What is 15*23?",
                privacy_class="CONFIDENTIAL",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
        assert public.calls == 0, "confidential task was dispatched to a PUBLIC provider"
        assert result.status == "ESCALATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_confidential_task_routes_to_the_local_provider_only(self):
        public = MockAdapter("public_only", text="345")
        restricted = MockAdapter("restricted_ok", text="345")
        providers = [(self._public_only(), public), (self._restricted_ok(), restricted)]
        async with FAIR(providers=providers) as fair:
            result = await fair.solve(
                "What is 15*23?",
                privacy_class="CONFIDENTIAL",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
        assert result.status == "ACCEPTED"
        assert result.provider_id == "restricted_ok"
        assert public.calls == 0
        assert restricted.calls == 1

    @pytest.mark.asyncio
    async def test_public_task_still_reaches_the_public_provider(self):
        public = MockAdapter("public_only", text="345")
        async with FAIR(providers=[(self._public_only(), public)]) as fair:
            result = await fair.solve(
                "What is 15*23?",
                validation={"kind": "arithmetic", "expression": "15*23"},
            )
        assert result.status == "ACCEPTED"
        assert public.calls == 1

    def test_discovered_local_ollama_accepts_restricted_data(self):
        fair = FAIR(ollama_url="http://127.0.0.1:11434", ollama_models=["llama3"])
        spec = fair._registry.providers["ollama_local"]
        assert spec.max_data_class == "RESTRICTED"

    @pytest.mark.asyncio
    async def test_source_policy_without_a_review_registry_blocks(self):
        """An exposed control must not be silently inert."""
        adapter = MockAdapter("a", text='{"answer": "x"}')
        evidence = [{"source_id": "s1", "review_id": "r1", "text": '{"answer": "x"}'}]
        validation = {
            "kind": "grounded_json",
            "fields": [{"output_key": "answer", "source_id": "s1", "pointer": "/answer"}],
        }
        async with FAIR(providers=[(_spec(), adapter)]) as fair:
            result = await fair.solve(
                "extract",
                validation=validation,
                evidence=evidence,
                source_policy={"min_independent_origins": 1},
            )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.source_policy.state == "BLOCKED"
        assert "SOURCE_REVIEW_REQUIRED" in result.source_policy.reasons
        assert result.attempts[0].quality.source_policy.state == "BLOCKED"


# ── Provider admission failures are per-provider ─────────────────────────


class TestBuiltinReviewDate:
    """The review date is an attestation, so it has to stay internally consistent."""

    def test_the_qualification_reference_cites_the_review_date(self):
        """These were separate literals once, so a bump left them disagreeing."""
        fair = FAIR(gemini_api_key="k", confirmed_free_providers={"google_gemini_api"})
        qualification = fair._registry.providers["google_gemini_api"].qualification
        stamp = qualification.reviewed_at.date().isoformat()
        for reference in (
            qualification.reviewer_reference,
            qualification.billing_reference,
            qualification.terms_reference,
            qualification.privacy_reference,
            qualification.limits_reference,
        ):
            assert reference.startswith(f"builtin-provider-review-{stamp}:")

    def test_the_review_date_is_not_in_the_future(self):
        """A post-dated review fails qualification outright: it buys nothing."""
        assert module._BUILTIN_PROVIDER_REVIEWED_AT <= datetime.now(UTC)

    def test_the_review_date_is_current_enough_to_qualify(self):
        """If this fails the built-in providers are unusable until it is re-verified."""
        fair = FAIR(gemini_api_key="k", confirmed_free_providers={"google_gemini_api"})
        assert "google_gemini_api" in fair._registry.adapters, fair.skipped

    def test_a_future_dated_review_is_refused_rather_than_trusted(self, monkeypatch):
        monkeypatch.setattr(
            module, "_BUILTIN_PROVIDER_REVIEWED_AT", datetime.now(UTC) + timedelta(days=5)
        )
        fair = FAIR(
            gemini_api_key="k",
            confirmed_free_providers={"google_gemini_api"},
            ollama_url="http://127.0.0.1:11434",
            ollama_models=["llama3"],
        )
        assert "google_gemini_api" in fair.skipped
        assert "google_gemini_api" not in fair._registry.adapters


class TestOpenRouterReviewedModels:
    def test_openrouter_free_models_match_reviewed_catalog_and_capabilities(self):
        models = {model.model_id: model for model in _CLOUD_PROVIDERS["openrouter_free"]["models"]}
        assert set(models) == {
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nex-agi/nex-n2.5-mini:free",
            "cohere/north-mini-code:free",
        }
        assert models["nvidia/nemotron-3-ultra-550b-a55b:free"].context_window == 1_000_000
        assert models["nex-agi/nex-n2.5-mini:free"].context_window == 262_144
        assert models["cohere/north-mini-code:free"].context_window == 256_000

        assert (
            "structured_output" not in models["nvidia/nemotron-3-ultra-550b-a55b:free"].capabilities
        )
        assert "structured_output" in models["nex-agi/nex-n2.5-mini:free"].capabilities
        assert "structured_output" not in models["cohere/north-mini-code:free"].capabilities
        assert all(model_id.endswith(":free") for model_id in models)

    def test_openrouter_free_account_daily_limit_is_locally_guarded(self):
        provider = _CLOUD_PROVIDERS["openrouter_free"]
        assert provider["request_limit"] == 50
        assert provider["request_limit_window"] == "DAILY_UTC"


class TestExpiredProviderReview:
    """A stale built-in review must cost one provider, not the whole router."""

    def test_expired_review_skips_the_provider_and_keeps_the_rest(self, monkeypatch):
        monkeypatch.setattr(module, "_BUILTIN_PROVIDER_REVIEWED_AT", _aged())
        fair = FAIR(
            gemini_api_key="k",
            confirmed_free_providers={"google_gemini_api"},
            ollama_url="http://127.0.0.1:11434",
            ollama_models=["llama3"],
        )
        assert "google_gemini_api" in fair.skipped
        assert "expired" in fair.skipped["google_gemini_api"]
        assert "google_gemini_api" not in fair._registry.adapters
        assert "ollama_local" in fair._registry.adapters

    def test_every_provider_expired_is_still_a_clear_configuration_error(self, monkeypatch):
        monkeypatch.setattr(module, "_BUILTIN_PROVIDER_REVIEWED_AT", _aged())
        with pytest.raises(ValueError, match="at least one safely eligible provider"):
            FAIR(gemini_api_key="k", confirmed_free_providers={"google_gemini_api"})

    def test_a_programming_error_in_registration_is_not_swallowed(self, monkeypatch):
        def boom(*args, **kwargs):
            raise TypeError("adapter signature changed")

        monkeypatch.setitem(_CLOUD_PROVIDERS["google_gemini_api"], "adapter", boom)
        with pytest.raises(TypeError, match="adapter signature changed"):
            FAIR(gemini_api_key="k", confirmed_free_providers={"google_gemini_api"})


# ── Locally counted quotas must recover ──────────────────────────────────


class TestRequestLimitWindow:
    def test_a_request_limit_requires_a_reset_window(self):
        with pytest.raises(ValueError, match="requires a request_limit_window"):
            ProviderSpec(
                provider_id="a",
                access_class="FREE_LOCAL",
                status="ACTIVE",
                current_access_cost_usd=0,
                requires_paid_subscription=False,
                requires_credit_purchase=False,
                auto_billing_required=False,
                programmatic_access=True,
                production_eligibility=True,
                request_limit=10,
            )

    def test_exhausted_local_counter_recovers_at_the_next_window(self):
        now = [datetime(2026, 9, 26, 12, tzinfo=UTC).timestamp()]
        gov = MemoryQuotaGovernor(RoutingSettings(), clock=lambda: now[0])
        spec = _spec(request_limit=2, request_limit_window="DAILY_PACIFIC")
        assert gov.reserve(spec) and gov.reserve(spec)
        assert not gov.available(spec), "ceiling should hold inside the window"
        now[0] += 5 * 86400
        assert gov.available(spec), "counter never cleared: provider disabled until restart"
        assert gov.remaining(spec) == 2

    def test_a_provider_reported_reset_overrides_the_window(self):
        now = [1000.0]
        gov = MemoryQuotaGovernor(RoutingSettings(), clock=lambda: now[0])
        spec = _spec(request_limit=5, request_limit_window="DAILY_UTC")
        gov.reserve(spec)
        gov.observe(
            spec,
            QuotaSnapshot(
                provider_id="a", quota_limit=5, quota_remaining_estimate=0, reset_at=1100.0
            ),
        )
        assert gov.state("a").reset_at == 1100.0
        now[0] = 1101.0
        assert gov.available(spec)

    def test_every_builtin_limit_declares_its_window(self):
        for provider_id, entry in _CLOUD_PROVIDERS.items():
            if entry.get("request_limit") is not None:
                assert entry.get("request_limit_window") is not None, provider_id


# ── Sanitized provider diagnostics survive the credential guard ──────────


class TestCredentialedAdapterDiagnostics:
    class _Failing:
        provider_id = "p"

        def __init__(self, error):
            self.error = error

        async def health(self):
            raise self.error

    def _guard(self, error, secret="cred-0123456789"):
        return CredentialedAdapter("p", self._Failing(error), SecretStr(secret))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error,expected",
        [
            (ProviderUnavailable("HTTP_503"), "HTTP_503"),
            (ProviderUnavailable("HTTP_404"), "HTTP_404"),
            (MalformedResponse("INVALID_CHAT_COMPLETION"), "INVALID_CHAT_COMPLETION"),
            (MalformedResponse("PROVIDER_RESPONSE_TOO_LARGE"), "PROVIDER_RESPONSE_TOO_LARGE"),
        ],
    )
    async def test_fair_authored_codes_are_preserved(self, error, expected):
        with pytest.raises(type(error)) as caught:
            await self._guard(error).health()
        assert str(caught.value) == expected

    @pytest.mark.asyncio
    async def test_a_message_carrying_upstream_text_is_replaced(self):
        leak = ProviderUnavailable("https://api.example.com/v1?key=cred-0123456789")
        with pytest.raises(ProviderUnavailable) as caught:
            await self._guard(leak).health()
        assert str(caught.value) == "PROVIDER_UNAVAILABLE"
        assert "cred-0123456789" not in str(caught.value)

    @pytest.mark.asyncio
    async def test_an_unknown_exception_is_still_reduced(self):
        with pytest.raises(ProviderUnavailable) as caught:
            await self._guard(RuntimeError("connect to 10.0.0.1 failed: cred-0123456789")).health()
        assert str(caught.value) == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_the_router_records_the_preserved_code(self):
        adapter = CredentialedAdapter(
            "a",
            MockAdapter("a", error=ProviderUnavailable("HTTP_503")),
            SecretStr("cred-0123456789"),
        )
        router = _router(entries=[(_spec(), adapter)])
        result = await router.solve(_request(task="hello"))
        assert result.attempts[0].error_detail == "HTTP_503"


# ── The removed native contract is gone from the public surface ──────────


class TestRemovedNativeContract:
    def test_native_python_function_is_rejected(self):
        with pytest.raises(ValidationError):
            SolveRequest.model_validate(
                {
                    "client_id": "c",
                    "task": "t",
                    "validation": {
                        "kind": "native_python_function",
                        "function_name": "f",
                        "cases": [{"arguments": [1], "expected": 1}],
                    },
                }
            )

    def test_bounded_python_function_is_still_accepted(self):
        request = SolveRequest.model_validate(
            {
                "client_id": "c",
                "task": "t",
                "validation": {
                    "kind": "python_function",
                    "function_name": "f",
                    "cases": [{"arguments": [1], "expected": 1}],
                },
            }
        )
        assert request.validation.kind == "python_function"
