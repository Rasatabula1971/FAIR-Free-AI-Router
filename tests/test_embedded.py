"""Tests for the embedded FAIR module — no database, no server."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from fair.config import RoutingSettings
from fair.embedded import FAIR, module
from fair.embedded.module import _CLOUD_PROVIDERS
from fair.embedded.performance import MemoryPerformanceRegistry
from fair.embedded.quota import (
    MemoryQuotaGovernor,
    QuotaLedgerUnavailable,
    SharedQuotaLedger,
    next_window_reset,
)
from fair.embedded.router import EmbeddedRouter
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
    StructuredOutputRejected,
)
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.thresholds import DEFAULT_THRESHOLDS, validate_thresholds
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

    @pytest.mark.asyncio
    async def test_async_shared_ledger_paths_are_nonblocking_and_consistent(self, tmp_path):
        now = [1000.0]
        spec = _spec(request_limit=2)
        gov = MemoryQuotaGovernor(
            RoutingSettings(),
            clock=lambda: now[0],
            shared_ledger=SharedQuotaLedger(tmp_path / "quota.sqlite3"),
            application_id="corp",
        )

        assert await gov.available_async(spec)
        assert await gov.remaining_async(spec) == 2
        assert await gov.reserve_async(spec, "corp")
        assert await gov.remaining_async(spec) == 1

        await gov.observe_async(
            spec,
            QuotaSnapshot(
                provider_id="a",
                quota_limit=2,
                quota_remaining_estimate=1,
                reset_at=1100.0,
            ),
        )
        assert await gov.effective_status_async(spec) == "ACTIVE"

        report = await gov.usage_report_async([spec])
        assert report["ledger_available"] is True
        assert report["pools"][0]["applications"] == {"corp": 1}

        await gov.exhaust_async(spec, reset_at=1100.0)
        assert await gov.effective_status_async(spec) == "QUOTA_EXHAUSTED"
        now[0] = 1101.0
        assert await gov.available_async(spec)

    @pytest.mark.asyncio
    async def test_only_one_concurrent_half_open_probe_is_reserved(self):
        import time
        from threading import Lock

        class SlowLedger:
            path = "slow.sqlite3"

            def __init__(self):
                self.calls = 0
                self.lock = Lock()

            def reserve(self, *args):
                with self.lock:
                    self.calls += 1
                time.sleep(0.05)
                return True

        ledger = SlowLedger()
        gov = MemoryQuotaGovernor(
            RoutingSettings(),
            shared_ledger=ledger,
            application_id="corp",
        )
        spec = _spec(request_limit=10)
        state = gov.state("a")
        state.circuit_state = "OPEN"
        state.blocked_until = 0

        results = await __import__("asyncio").gather(
            *(gov.reserve_async(spec, f"app-{index}") for index in range(5))
        )

        assert sum(results) == 1
        assert ledger.calls == 1
        assert gov.state("a").circuit_state == "HALF_OPEN"

    @pytest.mark.asyncio
    async def test_async_locked_ledger_fails_closed_without_exception(self):
        import sqlite3

        class BrokenLedger:
            path = "broken.sqlite3"

            def available(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def remaining(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def reserve(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def observe(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def exhaust(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def report(self, *args):
                raise sqlite3.OperationalError("database is locked")

        spec = _spec(request_limit=5)
        gov = MemoryQuotaGovernor(
            RoutingSettings(),
            shared_ledger=BrokenLedger(),
            application_id="corp",
        )

        assert await gov.available_async(spec) is False
        assert await gov.remaining_async(spec) == 0
        assert await gov.reserve_async(spec, "corp") is False
        await gov.observe_async(
            spec,
            QuotaSnapshot(
                provider_id="a",
                quota_limit=5,
                quota_remaining_estimate=4,
            ),
        )
        await gov.exhaust_async(spec, reset_at=1100.0)
        report = await gov.usage_report_async([spec])
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
    async def test_a_low_ceiling_route_answers_a_request_wanting_more_headroom(self):
        """End to end: wanted 32768, needed 2000, route caps at 4096. The single
        dial dropped this route from selection; the floor keeps it and asks it for
        4096."""
        spec = _spec("a")
        spec.models[0].max_output_tokens = 4096
        router = _router(entries=[(spec, MockAdapter("a", text="345"))])
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
                max_output_tokens=32768,
                min_output_tokens=2000,
            )
        )
        assert result.status == "ACCEPTED"
        assert result.provider_id == "a"

    @pytest.mark.asyncio
    async def test_the_same_route_is_unreachable_without_a_declared_floor(self):
        """The behaviour every existing caller keeps: no floor means the whole
        budget is the floor, so a 4096 route cannot serve a 32768 request."""
        spec = _spec("a")
        spec.models[0].max_output_tokens = 4096
        router = _router(entries=[(spec, MockAdapter("a", text="345"))])
        result = await router.solve(
            _request(
                task="15*23",
                validation={"kind": "arithmetic", "expression": "15*23"},
                max_output_tokens=32768,
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.reason_code == "NO_ELIGIBLE_FREE_MODELS"
        assert result.attempts == []

    @pytest.mark.asyncio
    async def test_a_schema_rejection_is_a_quality_failure_not_an_outage(self):
        router = _router(
            entries=[
                (
                    _spec("a"),
                    MockAdapter(
                        "a", error=StructuredOutputRejected("PROVIDER_REJECTED_GENERATED_SCHEMA")
                    ),
                )
            ],
            cooldown_seconds=1,
        )
        result = await router.solve(_request(task="anything"))
        attempt = result.attempts[0]
        assert attempt.disposition == "QUALITY_FAILURE"
        assert attempt.error_type == "PROVIDER_SCHEMA_VALIDATION_FAILED"
        assert result.reason_code == "ALL_FREE_MODELS_FAILED_QUALITY"
        # The provider is not implicated: no failure recorded, circuit left closed.
        assert router.quota.state("a").failures == []
        assert router.quota.effective_status(_spec("a")) == "ACTIVE"

    @pytest.mark.asyncio
    async def test_a_schema_rejection_spends_the_answer_budget(self):
        """It is an answer, so it counts against max_attempts rather than against
        the separate budget for models that never answered."""
        router = _router(
            entries=[
                (
                    _spec(name),
                    MockAdapter(
                        name, error=StructuredOutputRejected("PROVIDER_REJECTED_GENERATED_SCHEMA")
                    ),
                )
                for name in ("a", "b", "c", "d")
            ],
            max_attempts=2,
            cooldown_seconds=1,
        )
        result = await router.solve(_request(task="anything"))
        assert len(result.attempts) == 2
        assert {a.disposition for a in result.attempts} == {"QUALITY_FAILURE"}

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
        assert router.quota.effective_status(_spec("a")) == "ACTIVE"

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

        original_reserve = router.quota.reserve_probe_async
        lost_once = False

        async def reserve(spec, application_id=None, probe_timeout=None):
            nonlocal lost_once
            if spec.provider_id == "b" and not lost_once:
                lost_once = True
                return False, 0
            return await original_reserve(spec, application_id, probe_timeout)

        router.quota.reserve_probe_async = reserve
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
        spec_a = _spec(
            "a",
            models=[
                {
                    "model_id": "m1",
                    "context_window": 32768,
                    "capabilities": {"structured_output"},
                }
            ],
        )
        spec_b = _spec(
            "b",
            models=[
                {
                    "model_id": "m2",
                    "context_window": 32768,
                    "capabilities": {"structured_output"},
                }
            ],
        )
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
        assert result["entries_removed"] == 1

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


class TestKiloReviewedQuota:
    def test_kilo_free_hourly_limit_is_locally_guarded(self):
        provider = _CLOUD_PROVIDERS["kilo_free"]
        assert provider["request_limit"] == 200
        assert provider["request_limit_window"] == "HOURLY"

    def test_hourly_window_resets_one_hour_later(self):
        assert next_window_reset("HOURLY", 1000.0) == 4600.0


class TestOpenRouterReviewedModels:
    def test_openrouter_free_models_match_reviewed_catalog_and_capabilities(self):
        models = {model.model_id: model for model in _CLOUD_PROVIDERS["openrouter_free"]["models"]}
        assert set(models) == {
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "cohere/north-mini-code:free",
        }
        assert models["nvidia/nemotron-3-ultra-550b-a55b:free"].context_window == 1_000_000
        assert models["cohere/north-mini-code:free"].context_window == 256_000

        assert (
            "structured_output" not in models["nvidia/nemotron-3-ultra-550b-a55b:free"].capabilities
        )
        assert "structured_output" not in models["cohere/north-mini-code:free"].capabilities
        assert all(model_id.endswith(":free") for model_id in models)

    def test_openrouter_currently_has_no_structured_output_route(self):
        """Recorded, not accepted: nex-n2.5-mini carried this and left the catalog.

        A request with an expected_schema needs the structured_output capability, so
        while this holds the selector skips openrouter_free for every such request.
        Restoring a schema-capable free model here is what changes it back.
        """
        models = _CLOUD_PROVIDERS["openrouter_free"]["models"]
        assert not any("structured_output" in model.capabilities for model in models)

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


# ── Dropped schema constraints stay enforced ─────────────────────────────


class TestTransportedConstraintsStillJudgeTheAnswer:
    """Transport drops what a provider cannot parse; the caller's schema still decides."""

    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {"type": "array", "minItems": 2, "items": {"type": "string", "minLength": 1}}
        },
    }

    async def _solve(self, text):
        router = _router(entries=[(_spec(), MockAdapter("a", text=text))])
        return await router.solve(_request(task="list items", expected_schema=self.SCHEMA))

    async def test_a_response_breaking_a_dropped_min_items_is_still_rejected(self):
        result = await self._solve('{"items": ["one"]}')
        assert result.status == "ESCALATION_REQUIRED"
        assert "SCHEMA_FAILURE" in result.attempts[0].quality.reject_reasons

    async def test_a_response_breaking_a_dropped_min_length_is_still_rejected(self):
        result = await self._solve('{"items": ["one", ""]}')
        assert result.status == "ESCALATION_REQUIRED"
        assert "SCHEMA_FAILURE" in result.attempts[0].quality.reject_reasons

    async def test_a_conforming_response_is_accepted(self):
        result = await self._solve('{"items": ["one", "two"]}')
        assert result.status == "ACCEPTED"
        assert result.verification_state == "STRUCTURE_VALIDATED"


# ── Attempt budgets scale with the requested output ──────────────────────


class TestAttemptDeadline:
    """A fixed budget was a bet that any size of completion arrives inside it."""

    def _settings(self, **kw):
        from fair.config import RoutingSettings

        return RoutingSettings(**kw)

    def test_the_budget_grows_with_the_requested_output(self):
        settings = self._settings(timeout_seconds=15, output_tokens_per_second=30)
        assert settings.attempt_deadline(300) == pytest.approx(25)
        assert settings.attempt_deadline(3000) == pytest.approx(115)

    def test_a_tiny_request_still_gets_the_base_budget(self):
        settings = self._settings(timeout_seconds=15)
        assert settings.attempt_deadline(0) == 15

    def test_no_attempt_may_exceed_the_ceiling(self):
        settings = self._settings(max_timeout_seconds=100)
        assert settings.attempt_deadline(10_000_000) == 100

    def test_a_ceiling_below_the_base_is_rejected(self):
        with pytest.raises(ValueError, match="max_timeout_seconds"):
            self._settings(timeout_seconds=90, max_timeout_seconds=30)

    async def test_a_large_request_is_no_longer_cancelled_by_the_base_budget(self):
        """The router used to cancel at timeout_seconds however many tokens were asked for."""

        class SlowAdapter(MockAdapter):
            async def complete(self, request):
                await asyncio.sleep(0.4)
                return await super().complete(request)

        router = _router(
            entries=[(_spec(), SlowAdapter("a", text='{"items": [1]}'))],
            timeout_seconds=0.2,
            output_tokens_per_second=2000,
            max_timeout_seconds=30,
        )
        # 0.2s base + 2000/2000 = 1.2s for this request; the adapter needs 0.4s.
        assert router.settings.attempt_deadline(2000) == pytest.approx(1.2)
        result = await router.solve(
            _request(
                task="list items",
                max_output_tokens=2000,
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ACCEPTED"

    async def test_the_ceiling_still_cancels_an_attempt_that_overruns(self):
        class StalledAdapter(MockAdapter):
            async def complete(self, request):
                await asyncio.sleep(5)
                return await super().complete(request)

        router = _router(
            entries=[(_spec(), StalledAdapter("a", text='{"items": [1]}'))],
            timeout_seconds=0.1,
            output_tokens_per_second=10000,
            max_timeout_seconds=0.3,
        )
        result = await router.solve(
            _request(
                task="list items",
                max_output_tokens=2000,
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.attempts[0].disposition == "INFRA_FAILURE"

    def test_the_assumed_rate_comes_from_the_environment_when_unset(self, monkeypatch):
        monkeypatch.setenv("FAIR_OUTPUT_TOKENS_PER_SECOND", "12")
        fair = FAIR(providers=[(_spec(), MockAdapter("a"))])
        assert fair._router.settings.output_tokens_per_second == 12

    def test_an_explicit_rate_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("FAIR_OUTPUT_TOKENS_PER_SECOND", "12")
        fair = FAIR(providers=[(_spec(), MockAdapter("a"))], output_tokens_per_second=99)
        assert fair._router.settings.output_tokens_per_second == 99

    def test_an_unreadable_rate_leaves_the_default(self, monkeypatch):
        monkeypatch.setenv("FAIR_OUTPUT_TOKENS_PER_SECOND", "fast")
        fair = FAIR(providers=[(_spec(), MockAdapter("a"))])
        assert fair._router.settings.output_tokens_per_second == 40

    def test_the_adapter_and_the_router_size_budgets_from_the_same_rate(self):
        fair = FAIR(providers=[(_spec(), MockAdapter("a"))], output_tokens_per_second=7)
        assert fair._router.settings.output_tokens_per_second == 7


class TestProbedOutputLimits:
    """Each figure was measured against the live endpoint, not read from a document."""

    def _limits(self, provider):
        return {
            m.model_id: m.max_output_tokens for m in module._CLOUD_PROVIDERS[provider]["models"]
        }

    def test_cloudflare_carries_the_budgets_its_endpoints_accepted(self):
        limits = self._limits("cloudflare_workers_ai")
        assert limits["@cf/openai/gpt-oss-20b"] == 32768
        assert limits["@cf/meta/llama-4-scout-17b-16e-instruct"] == 32768
        # Refused at 32768 by the context guard, not by the endpoint: a 24000-token
        # context cannot hold a larger answer.
        assert limits["@cf/meta/llama-3.3-70b-instruct-fp8-fast"] == 16384

    def test_gemini_still_declares_no_local_cap(self):
        """Its ceiling is the one Google publishes per model, which FAIR reads live."""
        assert set(self._limits("google_gemini_api").values()) == {None}

    def test_groq_and_mistral_carry_the_budgets_their_endpoints_accepted(self):
        for provider in ("groq", "mistral"):
            assert set(self._limits(provider).values()) == {32768}, provider

    def test_an_unprobed_provider_keeps_the_conservative_default(self):
        """Kilo was rate-limited and OpenRouter refused on data policy, so neither answered."""
        for provider in ("kilo_free", "openrouter_free"):
            assert set(self._limits(provider).values()) == {4096}, provider

    def test_raising_the_ceiling_alone_does_not_raise_an_unprobed_model(self):
        from fair.providers.live import MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS_CEILING

        assert MAX_OUTPUT_TOKENS_CEILING > MAX_OUTPUT_TOKENS
        assert self._limits("kilo_free")["qwen/qwen3.8-27b:free"] == MAX_OUTPUT_TOKENS


# ── Returning an answer nothing could verify ─────────────────────────────


_router_factory = _router


class TestAcceptUnverified:
    """FAIR's promise is not to present unverified text as verified, which is not the
    same promise as never returning it."""

    def _router(self, text="A considered answer with no deterministic contract."):
        return _router_factory(entries=[(_spec(), MockAdapter("a", text=text))])

    async def test_open_ended_work_escalates_without_the_flag(self):
        """The state of affairs this exists to change."""
        result = await self._router().solve(_request(task="explain the trade-offs"))
        assert result.status == "ESCALATION_REQUIRED"
        assert result.reason_code == "QUALITY_VERIFICATION_UNAVAILABLE"
        assert result.output is None

    async def test_the_answer_comes_back_when_the_caller_accepts_that(self):
        result = await self._router().solve(
            _request(task="explain the trade-offs", accept_unverified=True)
        )
        assert result.status == "ACCEPTED_UNVERIFIED"
        assert result.reason_code == "RETURNED_WITHOUT_VERIFICATION"
        assert result.output == "A considered answer with no deterministic contract."

    async def test_it_is_never_reported_as_accepted(self):
        """A caller comparing against ACCEPTED keeps refusing what FAIR cannot vouch for."""
        result = await self._router().solve(
            _request(task="explain the trade-offs", accept_unverified=True)
        )
        assert result.status != "ACCEPTED"
        assert result.verification_state == "UNVERIFIED"
        assert result.best_quality_score is None
        assert result.quality.overall_score is None

    async def test_the_attempt_log_still_calls_it_unverified(self):
        """What FAIR knows does not change because of what the caller will take."""
        result = await self._router().solve(
            _request(task="explain the trade-offs", accept_unverified=True)
        )
        assert result.attempts[0].disposition == "UNVERIFIED"

    async def test_a_verifiable_answer_is_still_verified_properly(self):
        router = _router_factory(entries=[(_spec(), MockAdapter("a", text='{"items": [1]}'))])
        result = await router.solve(
            _request(
                task="list items",
                accept_unverified=True,
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ACCEPTED"
        assert result.verification_state == "STRUCTURE_VALIDATED"

    @pytest.mark.parametrize(
        ("text", "schema"),
        [
            ("", None),
            ("not json", {"type": "object", "required": ["items"]}),
        ],
    )
    async def test_an_answer_that_is_wrong_is_still_refused(self, text, schema):
        """Unverified means nothing could prove it right, not that nothing checked it."""
        router = _router_factory(entries=[(_spec(), MockAdapter("a", text=text or " "))])
        result = await router.solve(
            _request(task="do the thing", accept_unverified=True, expected_schema=schema)
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert result.output is None

    async def test_a_truncated_answer_is_still_refused(self):
        class _Truncated(MockAdapter):
            async def complete(self, request):
                response = await super().complete(request)
                return response.model_copy(update={"finish_reason": "length"})

        router = _router_factory(entries=[(_spec(), _Truncated("a", text="half an ans"))])
        result = await router.solve(_request(task="explain", accept_unverified=True))
        assert result.status == "ESCALATION_REQUIRED"

    def test_it_cannot_be_combined_with_a_demand_for_corroboration(self):
        for demand in ({"cross_check_required": True}, {"quality_level": "high_impact_support"}):
            with pytest.raises(ValidationError, match="accept_unverified"):
                _request(task="explain", accept_unverified=True, **demand)

    async def test_an_unverified_answer_is_never_cached(self):
        """The cache only keeps answers a deterministic contract settled."""
        router = _router_factory(
            entries=[(_spec(), MockAdapter("a", text="an answer"))], cache_enabled=True
        )
        request = _request(task="explain", accept_unverified=True)
        first = await router.solve(request)
        second = await router.solve(request)
        assert first.status == "ACCEPTED_UNVERIFIED"
        assert second.cache_hit is False


class TestIndependenceGroups:
    """Two gateways serving one model gave it two ids, and comparing ids alone then
    called the same model its own independent verifier."""

    def _model(self, provider_id, model_id):
        return next(
            m for m in module._CLOUD_PROVIDERS[provider_id]["models"] if m.model_id == model_id
        )

    def _independent(self, a, b):
        from fair.quality.consensus import independent

        class _Spec:
            def __init__(self, provider_id):
                self.provider_id = provider_id

        return independent((0, _Spec(a[0]), self._model(*a)), (0, _Spec(b[0]), self._model(*b)))

    def test_one_model_behind_two_gateways_is_not_its_own_verifier(self):
        assert not self._independent(
            ("groq", "openai/gpt-oss-20b"),
            ("cloudflare_workers_ai", "@cf/openai/gpt-oss-20b"),
        )

    def test_the_same_free_model_on_two_gateways_is_not_either(self):
        assert not self._independent(
            ("openrouter_free", "cohere/north-mini-code:free"),
            ("kilo_free", "cohere/north-mini-code:free"),
        )

    def test_genuinely_different_models_still_verify_each_other(self):
        assert self._independent(
            ("groq", "openai/gpt-oss-120b"),
            ("cloudflare_workers_ai", "@cf/meta/llama-4-scout-17b-16e-instruct"),
        )

    def test_every_id_naming_one_model_carries_one_group(self):
        groups: dict = {}
        for provider_id, entry in module._CLOUD_PROVIDERS.items():
            for model in entry["models"]:
                if model.independence_group:
                    groups.setdefault(model.independence_group, []).append(
                        (provider_id, model.model_id)
                    )
        # A group with one member would be doing nothing; each names a real duplicate.
        assert all(len(members) > 1 for members in groups.values()), groups


class TestContextIsEstimatedInTokens:
    """A prompt was counted in bytes, and the output budget shares the same window,
    so raising budgets to 32768 hid models whose context was ample."""

    def _estimate(self, chars, tokens, **kwargs):
        from fair.classifier.task_profiler import profile_task

        request = _request(task="x" * chars, max_output_tokens=tokens, **kwargs)
        return profile_task(request, dict(DEFAULT_THRESHOLDS)).context_tokens_estimate

    def test_a_prompt_is_no_longer_counted_as_one_token_per_byte(self):
        from fair.constants import BYTES_PER_TOKEN

        estimate = self._estimate(60_000, 1024)
        assert estimate < 60_000 / (BYTES_PER_TOKEN - 1) + 1024 + 600

    def test_a_request_a_window_can_hold_is_no_longer_excluded(self):
        """98,000 characters with a 32768 budget needs ~57k tokens; it was refused."""
        assert self._estimate(98_000, 32768) < 131_072

    def test_a_request_that_genuinely_does_not_fit_is_still_excluded(self):
        assert self._estimate(100_000, 32768) > 64_000

    def test_the_output_budget_still_counts_against_the_window(self):
        assert self._estimate(1_000, 32768) - self._estimate(1_000, 1024) == 32768 - 1024

    def test_a_schema_counts_too_because_the_provider_is_sent_it(self):
        schema = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(200)}}
        assert self._estimate(1_000, 1024, expected_schema=schema) > self._estimate(1_000, 1024)

    def test_the_selector_and_the_adapter_agree_about_what_fits(self):
        """They estimated separately before, so one could route what the other refused."""
        from fair.classifier.task_profiler import model_task, profile_task
        from fair.constants import estimated_tokens

        request = _request(task="x" * 50_000, max_output_tokens=8192)
        profile = profile_task(request, dict(DEFAULT_THRESHOLDS))
        adapter_view = estimated_tokens(model_task(request)) + request.max_output_tokens
        # The profile carries the caller's margin on top; neither may be the smaller.
        assert profile.context_tokens_estimate >= adapter_view

    async def test_a_large_prompt_still_reaches_a_model_with_room(self):
        router = _router(entries=[(_spec(), MockAdapter("a", text='{"items": [1]}'))])
        result = await router.solve(
            _request(
                task="x" * 20_000,
                max_output_tokens=2048,
                expected_schema={"type": "object", "required": ["items"]},
            )
        )
        assert result.status == "ACCEPTED"


class _MovableClock:
    """A clock the test advances by hand, so a cooldown elapses without waiting."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestQuotaUnderConcurrency:
    """The reservation path is safe because nothing awaits between the check and the
    increment. That is a property of the code's shape, not of its intent, so a future
    await slipped in there would break the guarantee silently. These pin it."""

    def _spec(self, **kw):
        return _spec("p", **kw)

    async def _granted(self, governor, spec, concurrency):
        return sum(
            await asyncio.gather(*(governor.reserve_async(spec) for _ in range(concurrency)))
        )

    @pytest.mark.parametrize(("limit", "concurrency"), [(1, 50), (5, 200), (10, 400)])
    async def test_concurrent_reservations_never_exceed_a_local_limit(self, limit, concurrency):
        governor = MemoryQuotaGovernor(RoutingSettings())
        spec = self._spec(request_limit=limit, request_limit_window="DAILY_UTC")
        assert await self._granted(governor, spec, concurrency) == limit

    async def test_a_yielding_event_loop_cannot_interleave_a_reservation(self):
        """If a check and its increment ever straddle an await, this is what finds it."""

        async def churn():
            for _ in range(2000):
                await asyncio.sleep(0)

        governor = MemoryQuotaGovernor(RoutingSettings())
        spec = self._spec(request_limit=10, request_limit_window="DAILY_UTC")
        granted, _ = await asyncio.gather(self._granted(governor, spec, 400), churn())
        assert granted == 10

    async def test_only_one_probe_is_dispatched_into_an_open_circuit(self):
        """Several coroutines observing OPEN at once must not all probe the provider."""
        settings = RoutingSettings(circuit_failures=2, cooldown_seconds=60)
        clock = _MovableClock()
        governor = MemoryQuotaGovernor(settings, clock=clock)
        spec = self._spec()
        for _ in range(settings.circuit_failures):
            governor.failure("p")
        assert governor._state("p").circuit_state == "OPEN"
        clock.now += settings.cooldown_seconds + 1
        assert await self._granted(governor, spec, 200) == 1

    async def test_a_released_probe_leaves_the_circuit_open_not_closed(self):
        settings = RoutingSettings(circuit_failures=2, cooldown_seconds=60)
        clock = _MovableClock()
        governor = MemoryQuotaGovernor(settings, clock=clock)
        spec = self._spec()
        for _ in range(settings.circuit_failures):
            governor.failure("p")
        clock.now += settings.cooldown_seconds + 1
        granted, token = await governor.reserve_probe_async(spec)
        assert granted is True and token
        state = governor._state("p")
        assert state.circuit_state == "HALF_OPEN"
        governor.release_probe("p", token)
        assert state.circuit_state == "OPEN"


class _Slow(MockAdapter):
    """Blocks in complete() so a solve can be cancelled mid-attempt."""

    async def complete(self, request):
        await asyncio.sleep(10)
        return await super().complete(request)


class TestFailoverUnderInjectedFailures:
    """Every way a provider can fail must reach the next one without poisoning it."""

    FAILURES = [
        ("pre_dispatch_budget", RequestNotSupported("OUTPUT_BUDGET_INVALID"), False),
        ("delisted_model", ModelUnavailable("REVIEWED_MODEL_UNAVAILABLE"), False),
        ("denied_403", AccessDenied("PROVIDER_ACCESS_DENIED"), False),
        ("unauthenticated_401", AuthenticationFailed("AUTHENTICATION_FAILED"), True),
        ("throttled_429", RateLimited("RATE_LIMITED"), False),
        ("quota_spent", QuotaExceeded("QUOTA_EXHAUSTED"), True),
        ("upstream_5xx", ProviderUnavailable("HTTP_503"), False),
        ("malformed_answer", MalformedResponse("INVALID_CHAT_COMPLETION"), False),
        ("nonzero_cost", BillingViolation("COST_NOT_CONFIRMED"), True),
    ]

    async def _solve(self, error):
        broken = _spec("broken", request_limit=50, request_limit_window="DAILY_UTC")
        healthy = _spec("healthy", request_limit=50, request_limit_window="DAILY_UTC")
        adapter = MockAdapter("healthy", text='{"items": [1]}')
        router = _router(entries=[(broken, MockAdapter("broken", error=error)), (healthy, adapter)])
        before = router.quota.remaining(healthy)
        result = await router.solve(
            _request(task="list items", expected_schema={"type": "object", "required": ["items"]})
        )
        return router, broken, healthy, adapter, result, before

    @pytest.mark.parametrize(
        ("label", "error", "_sidelined"), FAILURES, ids=[f[0] for f in FAILURES]
    )
    async def test_every_failure_reaches_the_next_provider(self, label, error, _sidelined):
        _router_, _b, _h, adapter, result, _before = await self._solve(error)
        assert adapter.calls == 1, label
        assert result.status == "ACCEPTED", label

    @pytest.mark.parametrize(
        ("label", "error", "_sidelined"), FAILURES, ids=[f[0] for f in FAILURES]
    )
    async def test_one_provider_failing_never_charges_another(self, label, error, _sidelined):
        router, _b, healthy, _a, _r, before = await self._solve(error)
        assert before - router.quota.remaining(healthy) == 1, label

    @pytest.mark.parametrize(
        ("label", "error", "sidelined"), FAILURES, ids=[f[0] for f in FAILURES]
    )
    async def test_only_a_failure_about_the_account_sidelines_a_provider(
        self, label, error, sidelined
    ):
        """A bad key, a spent quota and an unconfirmed cost are the three that should."""
        router, broken, _h, _a, _r, _before = await self._solve(error)
        state = router.quota.state("broken")
        assert (state.security_blocked or state.exhausted) is sidelined, label


class TestQuotaIsNotSpentOnRequestsNeverSent:
    """A reservation buys one request from a provider. If none was sent, it is owed back."""

    async def _spent(self, error):
        spec = _spec("p", request_limit=50, request_limit_window="DAILY_UTC")
        router = _router(entries=[(spec, MockAdapter("p", error=error))])
        before = router.quota.remaining(spec)
        await router.solve(_request(task="t"))
        return before - router.quota.remaining(spec)

    async def test_a_budget_fair_refused_before_dispatch_costs_nothing(self):
        assert await self._spent(RequestNotSupported("OUTPUT_BUDGET_INVALID")) == 0

    async def test_a_model_delisted_upstream_costs_nothing(self):
        """Otherwise a model gone from the catalog spends a free request every solve."""
        assert await self._spent(ModelUnavailable("REVIEWED_MODEL_UNAVAILABLE")) == 0

    @pytest.mark.parametrize(
        "error",
        [
            AccessDenied("PROVIDER_ACCESS_DENIED"),
            ProviderUnavailable("HTTP_503"),
            MalformedResponse("INVALID_CHAT_COMPLETION"),
            # The model generated and the provider's own validator refused the shape,
            # which is an answer, not an unsent request.
            StructuredOutputRejected("PROVIDER_SCHEMA_REJECTED"),
        ],
    )
    async def test_a_provider_that_was_called_still_costs_a_request(self, error):
        """It answered, however badly; the provider counted it and so must FAIR."""
        assert await self._spent(error) == 1

    async def test_a_refund_cannot_drive_a_count_below_zero(self):
        governor = MemoryQuotaGovernor(RoutingSettings())
        spec = _spec("p", request_limit=50, request_limit_window="DAILY_UTC")
        for _ in range(5):
            governor.release(spec)
        assert governor.state("p").used == 0

    async def test_repeated_pre_dispatch_refusals_do_not_drain_a_quota(self):
        spec = _spec("p", request_limit=3, request_limit_window="DAILY_UTC")
        router = _router(
            entries=[(spec, MockAdapter("p", error=RequestNotSupported("OUTPUT_BUDGET_INVALID")))]
        )
        for _ in range(10):
            await router.solve(_request(task="t"))
        assert router.quota.remaining(spec) == 3


class TestCancellationLeavesNoResidue:
    """Cancelling says nothing about a provider, so it must cost the provider nothing."""

    async def _cancel(self, router):
        task = asyncio.create_task(router.solve(_request(task="t")))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # wait_for, not a bare await: a cancellation that never lands fails
            # the test instead of hanging it.
            await asyncio.wait_for(task, timeout=5)

    async def test_cancelling_never_opens_a_circuit(self):
        spec = _spec("p")
        router = _router(entries=[(spec, _Slow("p"))], circuit_failures=2, cooldown_seconds=60)
        for _ in range(6):
            await self._cancel(router)
        state = router.quota.state("p")
        assert state.circuit_state == "CLOSED"
        assert not state.security_blocked and not state.exhausted
        assert router.quota.available(spec)

    async def test_a_cancelled_probe_does_not_sideline_a_provider(self):
        """A claimed probe left held costs the probe window and a fresh cooldown on top."""
        settings = dict(circuit_failures=2, cooldown_seconds=360)
        spec = _spec("p")
        router = _router(entries=[(spec, _Slow("p"))], **settings)
        for _ in range(settings["circuit_failures"]):
            router.quota.failure("p")
        router.quota._state("p").blocked_until = 0
        await self._cancel(router)
        assert router.quota.state("p").circuit_state == "OPEN"
        assert router.quota.available(spec) is True

    async def test_a_cancelled_request_still_counts_against_quota(self):
        """It may have been torn down in flight; over-counting is the safe direction."""
        spec = _spec("p", request_limit=50, request_limit_window="DAILY_UTC")
        router = _router(entries=[(spec, _Slow("p"))])
        before = router.quota.remaining(spec)
        await self._cancel(router)
        assert before - router.quota.remaining(spec) == 1


class TestSharedLedgerRelease:
    """Several FAIR processes share this counter, so a refund has to be transactional."""

    def _ledger(self, tmp_path):
        from fair.embedded.quota import SharedQuotaLedger

        return SharedQuotaLedger(str(tmp_path / "quota.sqlite3"))

    def test_a_release_gives_the_request_back(self, tmp_path):
        ledger, now = self._ledger(tmp_path), 1_000_000.0
        for _ in range(3):
            ledger.reserve("pool", "app", 10, "DAILY_UTC", now)
        assert ledger.remaining("pool", 10, now) == 7
        ledger.release("pool", "app", now)
        assert ledger.remaining("pool", 10, now) == 8

    def test_releases_cannot_manufacture_requests(self, tmp_path):
        """A window reset between reserve and release would otherwise go negative."""
        ledger, now = self._ledger(tmp_path), 1_000_000.0
        ledger.reserve("pool", "app", 10, "DAILY_UTC", now)
        for _ in range(20):
            ledger.release("pool", "app", now)
        assert ledger.remaining("pool", 10, now) == 10
        assert ledger.report(["pool"], now)[0]["used"] == 0

    def test_per_application_usage_is_given_back_too(self, tmp_path):
        ledger, now = self._ledger(tmp_path), 1_000_000.0
        ledger.reserve("pool", "one", 10, "DAILY_UTC", now)
        ledger.reserve("pool", "two", 10, "DAILY_UTC", now)
        ledger.release("pool", "one", now)
        applications = ledger.report(["pool"], now)[0]["applications"]
        assert applications == {"one": 0, "two": 1}

    async def test_the_governor_refunds_through_the_ledger(self, tmp_path):
        from fair.embedded.quota import SharedQuotaLedger

        ledger = SharedQuotaLedger(str(tmp_path / "quota.sqlite3"))
        governor = MemoryQuotaGovernor(RoutingSettings(), shared_ledger=ledger)
        spec = _spec("p", request_limit=10, request_limit_window="DAILY_UTC")
        assert await governor.reserve_async(spec, "app") is True
        assert governor.remaining(spec) == 9
        await governor.release_async(spec, "app")
        assert governor.remaining(spec) == 10


# ── The shared ledger under contention ───────────────────────────────────


def _is_open(database):
    try:
        database.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


class _Impatient(SharedQuotaLedger):
    """The real ledger with the wait removed, so contention is observed at once."""

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=0)
        database.execute("PRAGMA busy_timeout=0")
        database.execute("PRAGMA foreign_keys=ON")
        return database


class TestTheLedgerDoesNotLeakConnections:
    """`with sqlite3.connect(...)` commits; it does not close."""

    def test_every_call_closes_the_connection_it_opened(self, tmp_path, monkeypatch):
        opened = []
        real = sqlite3.connect

        def tracked(*args, **kwargs):
            database = real(*args, **kwargs)
            opened.append(database)
            return database

        monkeypatch.setattr(sqlite3, "connect", tracked)
        ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
        now = 1_000.0
        ledger.reserve("pool", "app", 50, "DAILY_UTC", now)
        ledger.remaining("pool", 50, now)
        ledger.available("pool", 50, now)
        ledger.release("pool", "app", now)
        ledger.observe("pool", 50, 40, now + 60, now)
        ledger.exhaust("pool", now + 60, now)
        ledger.report(["pool"], now)
        assert len(opened) >= 8
        # A connection still open pins the WAL file; one per solve accumulated
        # for the life of the process.
        still_open = [database for database in opened if _is_open(database)]
        assert still_open == []

    def test_a_failed_call_closes_its_connection_too(self, tmp_path):
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        ledger.reserve("pool", "app", 50, "DAILY_UTC", 1_000.0)
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(QuotaLedgerUnavailable):
                ledger.reserve("pool", "app", 50, "DAILY_UTC", 1_000.0)
        # The lock is gone, so the ledger is usable again -- which it would not be
        # if the failed call had left its own transaction open.
        assert ledger.reserve("pool", "app", 50, "DAILY_UTC", 1_000.0) is True


class TestContentionIsReportedNotGuessed:
    """A ledger that cannot be read is not the same as a quota that is spent."""

    def test_a_locked_ledger_refuses_rather_than_answering(self, tmp_path):
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            for call in (
                lambda: ledger.reserve("pool", "app", 50, "DAILY_UTC", 1_000.0),
                lambda: ledger.remaining("pool", 50, 1_000.0),
                lambda: ledger.available("pool", 50, 1_000.0),
                lambda: ledger.release("pool", "app", 1_000.0),
            ):
                with pytest.raises(QuotaLedgerUnavailable, match="LEDGER_LOCKED"):
                    call()

    def test_the_failure_code_never_carries_the_database_path(self, tmp_path):
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(QuotaLedgerUnavailable) as caught:
                ledger.reserve("pool", "app", 50, "DAILY_UTC", 1_000.0)
        assert str(caught.value) == "LEDGER_LOCKED"
        assert str(tmp_path) not in str(caught.value)

    def test_a_reserve_that_could_not_be_recorded_is_not_counted(self, tmp_path):
        """Failing closed: the refusal must not also spend the request."""
        ledger = _Impatient(tmp_path / "quota.sqlite3")
        with closing(sqlite3.connect(ledger.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(QuotaLedgerUnavailable):
                ledger.reserve("pool", "app", 50, "DAILY_UTC", 1_000.0)
        assert ledger.remaining("pool", 50, 1_000.0) == 50


class TestSharedExhaustionSurvivesStaleObservations:
    """An older positive observation must not undo a still-active shared block."""

    def _ledger(self, tmp_path):
        return SharedQuotaLedger(tmp_path / "quota.sqlite3")

    def _reset(self, ledger, pool="pool"):
        (row,) = ledger.report([pool], 1002.0)
        return row["reset_at"]

    def test_stale_positive_observation_does_not_clear_explicit_exhaustion(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.exhaust("pool", 2000.0, 1000.0)
        ledger.observe("pool", 100, 99, 1500.0, 1001.0)
        assert ledger.available("pool", None, 1002.0) is False
        assert ledger.available("pool", 100, 1002.0) is False

    def test_stale_positive_observation_cannot_shorten_the_reset(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.exhaust("pool", 2000.0, 1000.0)
        ledger.observe("pool", 100, 99, 1500.0, 1001.0)
        assert self._reset(ledger) == 2000.0

    def test_stale_positive_observation_cannot_extend_the_block_either(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.exhaust("pool", 2000.0, 1000.0)
        ledger.observe("pool", 100, 99, 9000.0, 1001.0)
        assert self._reset(ledger) == 2000.0

    def test_a_later_zero_observation_keeps_the_longer_reset(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.exhaust("pool", 5000.0, 1000.0)
        ledger.observe("pool", 100, 0, 1500.0, 1001.0)
        assert self._reset(ledger) == 5000.0
        assert ledger.available("pool", None, 1002.0) is False

    def test_a_zero_observation_can_lengthen_the_reset(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.exhaust("pool", 2000.0, 1000.0)
        ledger.observe("pool", 100, 0, 4000.0, 1001.0)
        assert self._reset(ledger) == 4000.0

    def test_exhaustion_is_still_recovered_at_the_trusted_reset(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.exhaust("pool", 2000.0, 1000.0)
        ledger.observe("pool", 100, 99, 1500.0, 1001.0)
        assert ledger.available("pool", None, 1999.0) is False
        assert ledger.available("pool", None, 2000.0) is True

    def test_a_positive_observation_still_updates_an_unexhausted_pool(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.observe("pool", 100, 40, 1500.0, 1001.0)
        assert ledger.remaining("pool", 100, 1002.0) == 40
        assert self._reset(ledger) == 1500.0

    def test_the_block_is_visible_to_another_instance_on_the_same_file(self, tmp_path):
        first, second, third = (self._ledger(tmp_path) for _ in range(3))
        first.exhaust("pool", 2000.0, 1000.0)
        second.observe("pool", 100, 99, 1500.0, 1001.0)
        assert third.available("pool", None, 1002.0) is False


class TestAcceptUnverifiedThroughThePublicApi:
    """The documented call is FAIR.solve(), not EmbeddedRouter.solve(SolveRequest)."""

    TASK = "Explain the trade-offs between X and Y"
    TEXT = "A considered answer with no deterministic contract."

    def _fair(self, text=None, **kwargs):
        adapter = MockAdapter("a", text=self.TEXT if text is None else text)
        return FAIR(providers=[(_spec(), adapter)], **kwargs), adapter

    async def test_open_ended_work_still_escalates_by_default(self):
        fair, _ = self._fair()
        result = await fair.solve(self.TASK)
        assert result.status == "ESCALATION_REQUIRED"
        assert result.reason_code == "QUALITY_VERIFICATION_UNAVAILABLE"
        assert result.output is None

    async def test_the_readme_example_returns_an_unverified_answer(self):
        fair, _ = self._fair()
        result = await fair.solve(self.TASK, accept_unverified=True)
        assert result.status == "ACCEPTED_UNVERIFIED"
        assert result.verification_state == "UNVERIFIED"
        assert result.best_quality_score is None
        assert result.output == self.TEXT

    async def test_an_answer_that_is_wrong_is_still_refused(self):
        fair, _ = self._fair(text="   ")
        result = await fair.solve(self.TASK, accept_unverified=True)
        assert result.status != "ACCEPTED_UNVERIFIED"
        assert result.output is None

    async def test_a_schema_mismatch_is_still_refused(self):
        fair, _ = self._fair(text="not json at all")
        result = await fair.solve(
            self.TASK,
            accept_unverified=True,
            expected_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        )
        assert result.status != "ACCEPTED_UNVERIFIED"
        assert result.output is None

    async def test_a_verifiable_answer_is_still_reported_as_verified(self):
        fair, _ = self._fair(text="345")
        result = await fair.solve(
            "15*23",
            accept_unverified=True,
            validation={"kind": "arithmetic", "expression": "15*23"},
        )
        assert result.status == "ACCEPTED"

    async def test_it_cannot_be_combined_with_cross_check(self):
        fair, _ = self._fair()
        with pytest.raises(ValidationError, match="accept_unverified"):
            await fair.solve(self.TASK, accept_unverified=True, cross_check_required=True)

    async def test_it_cannot_be_combined_with_high_impact_support(self):
        fair, _ = self._fair(quality_level="high_impact_support")
        with pytest.raises(ValidationError, match="accept_unverified"):
            await fair.solve(self.TASK, accept_unverified=True)

    async def test_an_instance_wide_cross_check_is_not_silently_dropped(self):
        fair, _ = self._fair(cross_check_required=True)
        with pytest.raises(ValidationError, match="accept_unverified"):
            await fair.solve(self.TASK, accept_unverified=True)

    async def test_unverified_answers_are_never_cached(self):
        fair, adapter = self._fair()
        await fair.solve(self.TASK, accept_unverified=True)
        await fair.solve(self.TASK, accept_unverified=True)
        assert adapter.calls == 2


class TestHalfOpenProbeOwnsItsAttempt:
    """A half-open probe stays exclusively owned for its real attempt deadline."""

    def _tripped(self, now):
        gov = MemoryQuotaGovernor(RoutingSettings(), clock=lambda: now[0])
        state = gov.state("a")
        state.circuit_state = "OPEN"
        state.blocked_until = 0
        return gov

    def test_a_direct_caller_keeps_the_default_lease(self):
        now = [1000.0]
        gov = self._tripped(now)
        assert gov.reserve(_spec()) is True
        now[0] += 19
        assert gov.state("a").circuit_state == "HALF_OPEN"
        now[0] += 2
        assert gov.state("a").circuit_state == "OPEN"

    def test_the_lease_covers_the_requested_attempt_deadline(self):
        now = [1000.0]
        gov = self._tripped(now)
        assert gov.reserve(_spec(), probe_timeout=300) is True
        now[0] += 120  # past the old 20 second lease, inside the attempt deadline
        state = gov.state("a")
        assert state.circuit_state == "HALF_OPEN"
        assert state.blocked_until == 0
        assert gov.reserve(_spec()) is False  # still exclusively owned

    def test_an_abandoned_probe_still_expires_after_its_deadline(self):
        now = [1000.0]
        gov = self._tripped(now)
        gov.reserve(_spec(), probe_timeout=300)
        now[0] += 306
        assert gov.state("a").circuit_state == "OPEN"

    def test_releasing_a_probe_reopens_without_a_new_cooldown(self):
        now = [1000.0]
        gov = self._tripped(now)
        granted, token = gov.reserve_probe(_spec(), probe_timeout=300)
        assert granted and token
        gov.release_probe("a", token)
        state = gov.state("a")
        assert state.circuit_state == "OPEN"
        assert state.blocked_until == 0
        assert gov.reserve(_spec()) is True  # the next probe may start at once

    def test_a_stale_probe_cannot_release_a_newer_one(self):
        now = [1000.0]
        gov = self._tripped(now)
        _, old = gov.reserve_probe(_spec(), probe_timeout=10)
        now[0] += 16  # the first lease expired
        assert gov.state("a").circuit_state == "OPEN"
        now[0] += 1000  # well past the cooldown the expiry started
        _, new = gov.reserve_probe(_spec(), probe_timeout=300)
        assert new and new != old
        gov.release_probe("a", old)
        assert gov.state("a").circuit_state == "HALF_OPEN"

    def test_a_stale_completion_cannot_close_a_newer_probes_circuit(self):
        now = [1000.0]
        gov = self._tripped(now)
        _, old = gov.reserve_probe(_spec(), probe_timeout=10)
        now[0] += 16  # the first lease expired; observing it starts the cooldown
        assert gov.state("a").circuit_state == "OPEN"
        now[0] += 1000  # past that cooldown
        _, new = gov.reserve_probe(_spec(), probe_timeout=300)
        assert new and new != old
        gov.success("a", probe_token=old)
        assert gov.state("a").circuit_state == "HALF_OPEN"
        gov.failure("a", probe_token=old)
        assert gov.state("a").circuit_state == "HALF_OPEN"
        gov.success("a", probe_token=new)
        assert gov.state("a").circuit_state == "CLOSED"


class _GatedAdapter(MockAdapter):
    """Holds its answer until released, so a test can act while the probe is in flight."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def complete(self, request):
        self.started.set()
        await self.gate.wait()
        return await super().complete(request)


class TestHalfOpenProbeThroughTheRouter:
    ARITHMETIC = {"kind": "arithmetic", "expression": "15*23"}

    async def _start(self):
        adapter = _GatedAdapter("a", text="345")
        router = _router(entries=[(_spec(), adapter)])
        now = [1000.0]
        router.quota.clock = lambda: now[0]
        state = router.quota.state("a")
        state.circuit_state = "OPEN"
        state.blocked_until = 0
        task = asyncio.create_task(router.solve(_request(task="15*23", validation=self.ARITHMETIC)))
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        return router, adapter, task, now

    async def test_a_slow_probe_keeps_its_half_open_slot_past_the_old_lease(self):
        router, adapter, task, now = await self._start()
        assert router.quota.state("a").circuit_state == "HALF_OPEN"
        now[0] += 30  # beyond the old 20 second lease, inside the attempt deadline
        state = router.quota.state("a")
        assert state.circuit_state == "HALF_OPEN"
        assert state.blocked_until == 0
        adapter.gate.set()
        result = await task
        assert result.status == "ACCEPTED"
        assert router.quota.state("a").circuit_state == "CLOSED"

    async def test_cancelling_the_probe_releases_its_slot_at_once(self):
        router, adapter, task, now = await self._start()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # wait_for, not a bare await: a cancellation that never lands fails
            # the test instead of hanging it.
            await asyncio.wait_for(task, timeout=5)
        state = router.quota.state("a")
        assert state.circuit_state == "OPEN"
        assert state.blocked_until == 0
        assert await router.quota.reserve_async(_spec()) is True


class TestSharedLedgerClosesItsConnections:
    """Every ledger operation closes the connection it opened, however it ends."""

    @pytest.fixture
    def opened(self, monkeypatch):
        import sqlite3

        connections = []
        real = sqlite3.connect

        class Tracked(sqlite3.Connection):
            # close() is recorded on the object, so the check also works for a
            # connection opened in a worker thread, which SQLite will not let the
            # test thread query.
            was_closed = False

            def close(self):
                self.was_closed = True
                super().close()

        def spy(*args, **kwargs):
            connection = real(*args, factory=Tracked, **kwargs)
            connections.append(connection)
            return connection

        monkeypatch.setattr(sqlite3, "connect", spy)
        return connections

    @staticmethod
    def _is_closed(connection):
        return getattr(connection, "was_closed", False)

    def _assert_all_closed(self, connections):
        assert connections, "the operation should have opened a connection"
        assert all(self._is_closed(c) for c in connections)

    def test_initialisation_closes_its_connection(self, tmp_path, opened):
        SharedQuotaLedger(tmp_path / "quota.sqlite3")
        self._assert_all_closed(opened)

    @pytest.mark.parametrize(
        "call",
        [
            lambda ledger: ledger.remaining("pool", 10, 1000.0),
            lambda ledger: ledger.available("pool", 10, 1000.0),
            lambda ledger: ledger.reserve("pool", "corp", 10, "DAILY_UTC", 1000.0),
            lambda ledger: ledger.observe("pool", 10, 4, 1500.0, 1000.0),
            lambda ledger: ledger.exhaust("pool", 2000.0, 1000.0),
            lambda ledger: ledger.report(["pool"], 1000.0),
            lambda ledger: ledger.release("pool", "corp", 1000.0),
        ],
    )
    def test_every_operation_closes_its_connection(self, tmp_path, opened, call):
        ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
        opened.clear()
        call(ledger)
        self._assert_all_closed(opened)

    def test_a_denied_reservation_closes_its_connection(self, tmp_path, opened):
        ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
        ledger.exhaust("pool", 2000.0, 1000.0)
        opened.clear()
        assert ledger.reserve("pool", "corp", 10, "DAILY_UTC", 1001.0) is False
        self._assert_all_closed(opened)

    def test_a_failed_transaction_rolls_back_and_closes(self, tmp_path, opened):
        import sqlite3

        path = tmp_path / "quota.sqlite3"
        ledger = SharedQuotaLedger(path)

        def boom(database, pool_id, now):
            database.execute("INSERT INTO quota_pool_state(pool_id) VALUES (?)", (pool_id,))
            raise RuntimeError("mid-transaction failure")

        ledger._recover = boom
        opened.clear()
        with pytest.raises(RuntimeError):
            ledger.reserve("pool", "corp", 10, "DAILY_UTC", 1000.0)
        self._assert_all_closed(opened)
        with sqlite3.connect(path) as check:
            count = check.execute(
                "SELECT COUNT(*) FROM quota_pool_state WHERE pool_id = 'pool'"
            ).fetchone()[0]
        check.close()
        assert count == 0  # rolled back, not committed

    def test_a_connection_that_fails_during_setup_is_closed(self, tmp_path, monkeypatch):
        import sqlite3

        path = tmp_path / "quota.sqlite3"
        ledger = SharedQuotaLedger(path)
        real_connections = []
        real = sqlite3.connect

        class Flaky(sqlite3.Connection):
            was_closed = False

            def execute(self, sql, *args):
                if "foreign_keys" in sql:
                    raise sqlite3.OperationalError("setup failed")
                return super().execute(sql, *args)

            def close(self):
                self.was_closed = True
                super().close()

        def spy(*args, **kwargs):
            connection = real(*args, factory=Flaky, **kwargs)
            real_connections.append(connection)
            return connection

        monkeypatch.setattr(sqlite3, "connect", spy)
        with pytest.raises(sqlite3.OperationalError):
            ledger.available("pool", 10, 1000.0)
        assert real_connections
        assert all(self._is_closed(c) for c in real_connections)

    def test_a_concurrent_burst_leaves_no_open_connections(self, tmp_path, opened):
        from concurrent.futures import ThreadPoolExecutor

        ledger = SharedQuotaLedger(tmp_path / "quota.sqlite3")
        opened.clear()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda i: ledger.reserve("pool", f"app-{i % 4}", 1000, "DAILY_UTC", 1000.0),
                    range(200),
                )
            )
        assert all(results)
        assert len(opened) >= 200
        assert all(self._is_closed(c) for c in opened)


class TestClearCacheDefaultIdentity:
    """clear_cache() with no argument clears the identity solve() caches under."""

    ARITHMETIC = {"kind": "arithmetic", "expression": "15*23"}

    def _fair(self, **kwargs):
        adapter = MockAdapter("a", text="345")
        fair = FAIR(providers=[(_spec(), adapter)], cache_enabled=True, **kwargs)
        return fair, adapter

    async def _solve(self, fair, **kwargs):
        return await fair.solve("15*23", validation=self.ARITHMETIC, **kwargs)

    async def test_a_named_application_clears_its_own_entries_by_default(self):
        fair, adapter = self._fair(application_id="corp")
        await self._solve(fair)
        assert fair.clear_cache() == {"entries_removed": 1}
        await self._solve(fair)
        assert adapter.calls == 2  # the cleared answer was not served from cache

    async def test_an_unnamed_instance_still_clears_the_embedded_identity(self):
        fair, adapter = self._fair()
        await self._solve(fair)
        assert fair.clear_cache() == {"entries_removed": 1}
        await self._solve(fair)
        assert adapter.calls == 2

    async def test_an_explicit_client_id_clears_only_that_client(self):
        fair, adapter = self._fair(application_id="corp")
        await self._solve(fair)
        await self._solve(fair, client_id="video")
        assert fair.clear_cache("video") == {"entries_removed": 1}
        await self._solve(fair)
        assert adapter.calls == 2  # corp's entry survived; video's did not

    async def test_the_old_embedded_default_does_not_clear_a_named_application(self):
        fair, _ = self._fair(application_id="corp")
        await self._solve(fair)
        assert fair.clear_cache("embedded") == {"entries_removed": 0}
        assert fair.clear_cache() == {"entries_removed": 1}

    async def test_clearing_an_empty_cache_removes_nothing(self):
        fair, _ = self._fair(application_id="corp")
        assert fair.clear_cache() == {"entries_removed": 0}
