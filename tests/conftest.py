import pytest

from fair.config import RoutingSettings
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.router.orchestrator import Router
from fair.schemas.db import Base, database
from fair.schemas.domain import ProviderSpec


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
