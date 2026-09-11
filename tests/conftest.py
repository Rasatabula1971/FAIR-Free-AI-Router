from datetime import UTC, datetime, timedelta

import pytest

from fair.config import RoutingSettings
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.router.orchestrator import Router
from fair.schemas.db import Base, database
from fair.schemas.domain import ProviderSpec


def qualification(name, models, *, reviewed_at=None, free_status="verified_free_plan"):
    """Synthetic evidence for offline tests only; never an operator approval."""
    reviewed_at = reviewed_at or datetime.now(UTC) - timedelta(seconds=1)
    return {
        "provider_id": name,
        "free_status": free_status,
        "production_allowed": True,
        "billing_enabled": False,
        "payment_method_required": False,
        "payment_method_present": False,
        "can_auto_bill": False,
        "reviewed_at": reviewed_at,
        "expires_at": reviewed_at + timedelta(days=29),
        "reviewer_reference": "offline-fixture-reviewer",
        "billing_reference": "offline-fixture-billing",
        "terms_reference": "offline-fixture-terms",
        "privacy_reference": "offline-fixture-privacy",
        "limits_reference": "offline-fixture-limits",
        "models": [
            {
                "model_id": model["model_id"],
                "model_revision": model.get("model_revision"),
                "input_price_per_million": "0",
                "output_price_per_million": "0",
                "request_price": "0",
                "pricing_reference": "offline-fixture-pricing",
                "paid_tools_enabled": False,
                "live_test_passed": True,
                "live_test_at": reviewed_at,
                "live_test_reference": "offline-fixture-test",
                "zero_charge_verified": True,
                "zero_charge_reference": "offline-fixture-cost",
            }
            for model in models
        ],
    }


def provider(name="a", **changes):
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
        models=[{"model_id": "model", "context_window": 32768}],
    )
    return ProviderSpec(**(values | changes))


@pytest.fixture
def make_router():
    engines = []

    def build(entries=None, **settings):
        engine, sessions = database("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        engines.append(engine)
        registry = Registry()
        for spec, adapter in entries if entries is not None else [(provider(), MockAdapter("a"))]:
            registry.register(spec, adapter)
        return Router(
            registry,
            RoutingSettings(**settings),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            sessions,
        )

    yield build
    for engine in engines:
        engine.dispose()
