"""CredentialedAdapter and Registry admission.

The guard is the last thing between a provider adapter and a credential
appearing in a response, a log line or an attempt record. The registry is where
a provider is admitted or refused. Both were under 75% covered.
"""

import pytest
from pydantic import BaseModel, SecretStr

from fair.governor.policy import AdmissionDenied
from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.schemas.domain import ProviderHealth, ProviderSpec, QuotaSnapshot
from fair.security.adapter import CredentialedAdapter
from fair.security.credentials import CredentialConfigurationError, ProviderCredentials

SECRET = "cred-0123456789abcdef"


def _spec(provider_id="a", **overrides):
    values = dict(
        provider_id=provider_id,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        models=[{"model_id": "m", "context_window": 32768, "capabilities": {"reasoning"}}],
    )
    return ProviderSpec(**(values | overrides))


class _Echo:
    """An adapter that reflects whatever it is handed — the failure being guarded against."""

    provider_id = "a"

    def __init__(self, payload=None, error=None, diagnostics=None):
        self.payload = payload
        self.error = error
        self.diagnostics = diagnostics
        self.closed = False
        self.admitted = 0

    async def health(self):
        if self.error:
            raise self.error
        return (
            self.payload
            if self.payload is not None
            else ProviderHealth(provider_id="a", state="ACTIVE", source="OFFLINE_FIXTURE")
        )

    async def quota(self):
        return QuotaSnapshot(provider_id="a")

    async def list_models(self):
        return []

    async def complete(self, request):
        return self.payload

    def safe_diagnostics(self):
        return self.diagnostics

    def check_admission(self):
        self.admitted += 1
        if self.error:
            raise self.error

    async def close(self):
        self.closed = True


def _guard(adapter):
    return CredentialedAdapter("a", adapter, SecretStr(SECRET))


# ── Identity ────────────────────────────────────────────────────────────


class TestIdentity:
    def test_a_mismatched_adapter_is_refused(self):
        with pytest.raises(ValueError, match="Adapter identity mismatch"):
            CredentialedAdapter("other", _Echo(), SecretStr(SECRET))

    def test_live_inference_is_carried_from_the_adapter(self):
        adapter = _Echo()
        adapter.live_inference = True
        assert _guard(adapter).live_inference is True

    def test_live_inference_defaults_to_false(self):
        assert _guard(_Echo()).live_inference is False


# ── Credential reflection ───────────────────────────────────────────────


class _Model(BaseModel):
    value: str


class TestCredentialReflection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            SECRET,
            f"prefix {SECRET} suffix",
            {"key": SECRET},
            {SECRET: "value"},
            [SECRET],
            ("nested", [{"deep": SECRET}]),
            SECRET.encode(),
        ],
    )
    async def test_a_reflected_credential_is_blocked(self, payload):
        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_EXPOSURE_BLOCKED"):
            await _guard(_Echo(payload=payload)).health()

    @pytest.mark.asyncio
    async def test_a_credential_inside_a_pydantic_model_is_blocked(self):
        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_EXPOSURE_BLOCKED"):
            await _guard(_Echo(payload=_Model(value=SECRET))).health()

    @pytest.mark.asyncio
    async def test_a_credential_in_an_outgoing_argument_is_blocked(self):
        """The check runs before dispatch, so a leak never reaches the network."""
        adapter = _Echo()
        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_EXPOSURE_BLOCKED"):
            await _guard(adapter).complete({"task": SECRET})

    @pytest.mark.asyncio
    async def test_a_deeply_nested_payload_is_refused_rather_than_recursed(self):
        payload = current = {}
        for _ in range(40):
            current["next"] = {}
            current = current["next"]
        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_CHECK_DEPTH_EXCEEDED"):
            await _guard(_Echo(payload=payload)).health()

    @pytest.mark.asyncio
    async def test_an_unrelated_payload_passes_through(self):
        health = ProviderHealth(provider_id="a", state="ACTIVE", source="OFFLINE_FIXTURE")
        assert await _guard(_Echo(payload=health)).health() == health


# ── Error translation ───────────────────────────────────────────────────


class TestErrorTranslation:
    @pytest.mark.asyncio
    async def test_a_billing_violation_is_reported_as_a_cost_policy_violation(self):
        with pytest.raises(BillingViolation, match="PROVIDER_REPORTED_NONZERO_OR_INVALID_COST"):
            await _guard(_Echo(error=BillingViolation("upstream detail"))).health()

    @pytest.mark.asyncio
    async def test_an_authentication_failure_is_generic(self):
        with pytest.raises(AuthenticationFailed, match="AUTHENTICATION_FAILED"):
            await _guard(_Echo(error=AuthenticationFailed(f"key {SECRET} rejected"))).health()

    @pytest.mark.asyncio
    async def test_a_quota_exhaustion_keeps_its_reset_time(self):
        error = QuotaExceeded("upstream detail", reset_at=1234.0)
        with pytest.raises(QuotaExceeded) as caught:
            await _guard(_Echo(error=error)).health()
        assert str(caught.value) == "QUOTA_EXHAUSTED"
        assert caught.value.reset_at == 1234.0

    @pytest.mark.asyncio
    async def test_a_rate_limit_keeps_its_retry_after(self):
        error = RateLimited("upstream detail", retry_after=30)
        with pytest.raises(RateLimited) as caught:
            await _guard(_Echo(error=error)).health()
        assert str(caught.value) == "RATE_LIMITED"
        assert caught.value.retry_after == 30

    @pytest.mark.asyncio
    async def test_quota_and_rate_limit_messages_never_carry_upstream_text(self):
        for error in (
            QuotaExceeded(f"quota for {SECRET}", reset_at=1.0),
            RateLimited(f"slow down {SECRET}", retry_after=1),
        ):
            with pytest.raises(type(error)) as caught:
                await _guard(_Echo(error=error)).health()
            assert SECRET not in str(caught.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error,expected",
        [
            (MalformedResponse("PROVIDER_ERROR_ENVELOPE"), "PROVIDER_ERROR_ENVELOPE"),
            (MalformedResponse("lower case detail"), "MALFORMED_PROVIDER_RESPONSE"),
            (MalformedResponse(f"body: {SECRET}"), "MALFORMED_PROVIDER_RESPONSE"),
            (ProviderUnavailable("HTTP_429"), "HTTP_429"),
            (ProviderUnavailable("A" * 65), "PROVIDER_UNAVAILABLE"),
        ],
    )
    async def test_only_well_formed_codes_survive(self, error, expected):
        with pytest.raises(type(error)) as caught:
            await _guard(_Echo(error=error)).health()
        assert str(caught.value) == expected
        assert SECRET not in str(caught.value)

    @pytest.mark.asyncio
    async def test_an_arbitrary_exception_is_reduced(self):
        error = RuntimeError(f"https://api.example.invalid?key={SECRET}")
        with pytest.raises(ProviderUnavailable, match="PROVIDER_UNAVAILABLE"):
            await _guard(_Echo(error=error)).health()


# ── The remaining delegated methods ──────────────────────────────────────


class TestDelegation:
    @pytest.mark.asyncio
    async def test_quota_and_list_models_are_guarded(self):
        guard = _guard(_Echo())
        assert (await guard.quota()).provider_id == "a"
        assert await guard.list_models() == []

    @pytest.mark.asyncio
    async def test_close_is_forwarded(self):
        adapter = _Echo()
        await _guard(adapter).close()
        assert adapter.closed

    @pytest.mark.asyncio
    async def test_close_is_optional(self):
        await _guard(MockAdapter("a")).close()

    def test_check_admission_is_forwarded(self):
        adapter = _Echo()
        _guard(adapter).check_admission()
        assert adapter.admitted == 1

    def test_a_failed_admission_check_is_generic(self):
        adapter = _Echo(error=ValueError(f"bad config {SECRET}"))
        with pytest.raises(AuthenticationFailed, match="AUTHENTICATION_FAILED"):
            _guard(adapter).check_admission()

    def test_check_admission_is_optional(self):
        _guard(MockAdapter("a")).check_admission()

    def test_safe_diagnostics_are_checked(self):
        assert _guard(_Echo(diagnostics={"cost": "ZERO"})).safe_diagnostics() == {"cost": "ZERO"}

    def test_diagnostics_reflecting_a_credential_are_blocked(self):
        with pytest.raises(AuthenticationFailed, match="CREDENTIAL_EXPOSURE_BLOCKED"):
            _guard(_Echo(diagnostics={"token": SECRET})).safe_diagnostics()

    def test_an_adapter_without_diagnostics_reports_none(self):
        assert _guard(MockAdapter("a")).safe_diagnostics() == {}


# ── Registry admission ───────────────────────────────────────────────────


class TestRegistry:
    def test_an_eligible_provider_registers(self):
        registry = Registry()
        registry.register(_spec(), MockAdapter("a"))
        assert set(registry.providers) == {"a"}
        assert set(registry.adapters) == {"a"}

    def test_a_spec_may_register_without_an_adapter(self):
        registry = Registry()
        registry.register(_spec())
        assert registry.adapters == {}

    def test_a_duplicate_provider_is_refused(self):
        registry = Registry()
        registry.register(_spec(), MockAdapter("a"))
        with pytest.raises(ValueError, match="Duplicate provider"):
            registry.register(_spec(), MockAdapter("a"))

    def test_an_adapter_for_another_provider_is_refused(self):
        with pytest.raises(ValueError, match="Adapter identity mismatch"):
            Registry().register(_spec("a"), MockAdapter("b"))

    def test_a_paid_provider_is_not_admitted(self):
        with pytest.raises(AdmissionDenied):
            Registry().register(_spec(requires_paid_subscription=True), MockAdapter("a"))

    def test_a_provider_with_an_unknown_access_cost_is_not_admitted(self):
        with pytest.raises(AdmissionDenied):
            Registry().register(_spec(current_access_cost_usd=None), MockAdapter("a"))

    def test_an_inactive_spec_skips_admission(self):
        """A spec under review is recorded without being admitted for dispatch."""
        registry = Registry()
        registry.register(_spec(status="TERMS_REVIEW", requires_paid_subscription=True))
        assert set(registry.providers) == {"a"}

    def test_the_stored_spec_is_a_copy(self):
        registry = Registry()
        spec = _spec()
        registry.register(spec, MockAdapter("a"))
        spec.models[0].active = False
        assert registry.providers["a"].models[0].active is True

    def test_register_credentialed_resolves_and_wraps(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_A_KEY", SECRET)
        registry = Registry()
        registry.register_credentialed(
            _spec(),
            lambda credential: MockAdapter("a"),
            ProviderCredentials({"a": "PROVIDER_A_KEY"}),
        )
        assert isinstance(registry.adapters["a"], CredentialedAdapter)

    def test_register_credentialed_refuses_a_duplicate(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_A_KEY", SECRET)
        registry = Registry()
        registry.register(_spec(), MockAdapter("a"))
        with pytest.raises(ValueError, match="Duplicate provider"):
            registry.register_credentialed(
                _spec(),
                lambda credential: MockAdapter("a"),
                ProviderCredentials({"a": "PROVIDER_A_KEY"}),
            )

    def test_register_credentialed_requires_an_admissible_spec(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_A_KEY", SECRET)
        with pytest.raises(AdmissionDenied):
            Registry().register_credentialed(
                _spec(requires_credit_purchase=True),
                lambda credential: MockAdapter("a"),
                ProviderCredentials({"a": "PROVIDER_A_KEY"}),
            )

    def test_a_factory_failure_is_a_configuration_error(self, monkeypatch):
        """The factory's own exception text could name the credential."""
        monkeypatch.setenv("PROVIDER_A_KEY", SECRET)

        def factory(credential):
            raise RuntimeError(f"cannot start with {SECRET}")

        with pytest.raises(CredentialConfigurationError) as caught:
            Registry().register_credentialed(
                _spec(), factory, ProviderCredentials({"a": "PROVIDER_A_KEY"})
            )
        assert SECRET not in str(caught.value)

    def test_a_missing_credential_is_a_configuration_error(self, monkeypatch):
        monkeypatch.delenv("PROVIDER_A_KEY", raising=False)
        with pytest.raises(CredentialConfigurationError):
            Registry().register_credentialed(
                _spec(),
                lambda credential: MockAdapter("a"),
                ProviderCredentials({"a": "PROVIDER_A_KEY"}),
            )
