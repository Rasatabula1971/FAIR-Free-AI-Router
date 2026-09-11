from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import provider, qualification
from pydantic import ValidationError
from sqlalchemy import select

from fair.governor.policy import AdmissionDenied, admit_provider
from fair.providers.mock import MockAdapter
from fair.schemas.api import SolveRequest
from fair.schemas.db import AuditEvent, Provider, ProviderQuotaState, SystemState
from fair.schemas.domain import ProviderSpec
from fair.schemas.qualification import FreeStatus, ProviderQualification


def cloud():
    models = [{"model_id": "reviewed-model", "context_window": 4096}]
    return provider(
        "groq",
        access_class="FREE_RECURRING",
        terms_last_verified=datetime.now(UTC),
        models=models,
        qualification=qualification("groq", models),
    )


def test_missing_cloud_evidence_denied_and_local_behavior_preserved():
    spec = cloud()
    admit_provider(spec)
    spec.qualification = None
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)
    admit_provider(provider())


@pytest.mark.parametrize("status", [s for s in FreeStatus if s != FreeStatus.VERIFIED_FREE_PLAN])
def test_nonqualifying_or_mismatched_classification_denied(status):
    spec = cloud()
    spec.qualification.free_status = status
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", "another-account-provider"),
        ("production_allowed", False),
        ("billing_enabled", True),
        ("billing_enabled", None),
        ("payment_method_required", True),
        ("payment_method_required", None),
        ("payment_method_present", None),
        ("can_auto_bill", True),
        ("can_auto_bill", None),
        ("billing_reference", None),
        ("terms_reference", None),
        ("privacy_reference", None),
        ("limits_reference", None),
        ("reviewer_reference", None),
        ("reviewed_at", None),
        ("expires_at", None),
        ("models", []),
    ],
)
def test_unknown_unsafe_or_unreferenced_account_evidence_denied(field, value):
    spec = cloud()
    setattr(spec.qualification, field, value)
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)


@pytest.mark.parametrize("mode", ["expired", "future", "too_long", "stale"])
def test_review_time_boundaries(mode):
    now = datetime.now(UTC)
    spec = cloud()
    evidence = spec.qualification
    if mode == "expired":
        evidence.expires_at = now
    elif mode == "future":
        evidence.reviewed_at = now + timedelta(seconds=1)
    elif mode == "too_long":
        evidence.expires_at = evidence.reviewed_at + timedelta(days=31)
    else:
        evidence.reviewed_at = now - timedelta(days=31)
    with pytest.raises(AdmissionDenied):
        admit_provider(spec, now=now)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "another-model"),
        ("model_revision", "unreviewed-revision"),
        ("input_price_per_million", None),
        ("output_price_per_million", None),
        ("request_price", None),
        ("input_price_per_million", Decimal("1e-1000")),
        ("output_price_per_million", Decimal("0.01")),
        ("request_price", Decimal("0.01")),
        ("pricing_reference", None),
        ("paid_tools_enabled", True),
        ("paid_tools_enabled", None),
        ("live_test_passed", False),
        ("live_test_at", None),
        ("live_test_reference", None),
        ("zero_charge_verified", False),
        ("zero_charge_reference", None),
    ],
)
def test_model_specific_price_and_live_evidence_denied(field, value):
    spec = cloud()
    setattr(spec.qualification.models[0], field, value)
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)


def test_new_active_model_and_changed_revision_require_new_review():
    spec = cloud()
    spec.models.append(spec.models[0].model_copy(update={"model_id": "unreviewed"}))
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)
    spec.models[-1].active = False
    admit_provider(spec)
    spec.models[0].model_revision = "new-revision"
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)


@pytest.mark.parametrize("delta", [-31, 1])
def test_stale_or_post_review_inference_evidence_denied(delta):
    spec = cloud()
    spec.qualification.models[0].live_test_at = spec.qualification.reviewed_at + timedelta(
        days=delta
    )
    with pytest.raises(AdmissionDenied):
        admit_provider(spec)


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_billing_evidence_requires_actual_booleans(value):
    with pytest.raises(ValidationError):
        ProviderQualification(provider_id="groq", billing_enabled=value)


def test_duplicate_models_naive_dates_and_nonfinite_prices_rejected():
    values = cloud().qualification.model_dump()
    with pytest.raises(ValidationError):
        ProviderQualification(**(values | {"models": values["models"] * 2}))
    with pytest.raises(ValidationError):
        ProviderQualification(**(values | {"reviewed_at": datetime(2026, 9, 10)}))
    for price in ("NaN", "Infinity", "-0.1"):
        with pytest.raises(ValidationError):
            cloud().qualification.models[0].request_price = price


async def test_expiry_after_registration_blocks_dispatch(make_router):
    spec = cloud()
    adapter = MockAdapter("groq")
    router = make_router([(spec, adapter)])
    router.registry.providers["groq"].qualification.expires_at = datetime.now(UTC)
    result = await router.solve(SolveRequest(client_id="alice", task="Hello"))
    assert result.attempts == []
    assert adapter.calls == 0


def test_qualification_roundtrip_preserves_runtime_blocks_and_audit(make_router):
    spec = cloud()
    router = make_router([(spec, MockAdapter("groq"))])
    router.stopped = True
    router.quota.reserve(spec)
    router.quota.block_security("groq")
    with router.sessions() as session:
        before = len(list(session.scalars(select(AuditEvent))))
    router.registry.providers["groq"].qualification.billing_reference = "updated-review"
    router.registry.persist(router.sessions)
    with router.sessions() as session:
        row = session.get(Provider, "groq")
        values = spec.model_dump()
        values["qualification"] = row.qualification
        loaded = ProviderSpec.model_validate(values)
        assert loaded.qualification.billing_reference == "updated-review"
        assert loaded.qualification.models[0].request_price == Decimal(0)
        state = session.get(ProviderQuotaState, "groq")
        assert state.used == 1 and state.security_blocked
        assert session.get(SystemState, "global").stopped
        assert len(list(session.scalars(select(AuditEvent)))) == before
