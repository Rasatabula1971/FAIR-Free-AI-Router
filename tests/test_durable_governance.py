import pytest
from alembic import command
from alembic.config import Config
from conftest import provider
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fair.config import RoutingSettings
from fair.providers.base import AuthenticationFailed, QuotaExceeded
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quota.governor import QuotaGovernor
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import (
    AuditEvent,
    Base,
    Model,
    Provider,
    ProviderHealthEvent,
    ProviderQuotaState,
    TaskProfile,
    database,
)


def request():
    return SolveRequest(client_id="alice", task="Hello")


@pytest.fixture
def restartable(tmp_path):
    url = "sqlite:///" + (tmp_path / "restart.db").as_posix()
    engines = []

    def start(spec=None, adapter=None):
        engine, sessions = database(url)
        engines.append(engine)
        Base.metadata.create_all(engine)
        registry = Registry()
        registry.register(spec or provider(), adapter or MockAdapter("a"))
        router = Router(registry, RoutingSettings(), {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}, sessions)
        return router

    yield start
    for engine in engines:
        engine.dispose()


async def test_stop_survives_restart_and_resume_is_audited(restartable):
    first = restartable()
    first.stopped = True
    second = restartable()
    assert second.stopped
    assert (await second.solve(request())).reason_code == "SYSTEM_STOPPED"
    assert second.registry.adapters["a"].calls == 0
    second.stopped = False
    third = restartable()
    assert not third.stopped
    assert len((await third.solve(request())).attempts) == 1
    with third.sessions() as session:
        events = list(session.scalars(select(AuditEvent.event_type)))
        assert events.count("SYSTEM_STOP") == events.count("SYSTEM_RESUME") == 1


async def test_reservation_survives_restart_and_config_reconcile(restartable):
    first = restartable(provider(request_limit=1))
    await first.solve(request())
    second = restartable(provider(request_limit=1))
    assert await second.quota.remaining(second.registry.providers["a"]) == 0
    assert not (await second.solve(request())).attempts


@pytest.mark.parametrize(
    "error,status",
    [
        (QuotaExceeded("secret-sentinel"), "QUOTA_EXHAUSTED"),
        (AuthenticationFailed("secret-sentinel"), "SECURITY_BLOCKED"),
    ],
)
async def test_failure_blocks_survive_restart(restartable, error, status):
    await restartable(adapter=MockAdapter("a", error=error)).solve(request())
    second = restartable()
    assert await second.quota.effective_status(second.registry.providers["a"]) == status
    assert not (await second.solve(request())).attempts
    with second.sessions() as session:
        assert session.scalar(select(ProviderHealthEvent.event_type)) in {
            "QUOTA_EXHAUSTED",
            "AUTHENTICATION_FAILED",
        }
        # Persisted fields contain normalized events, not exception text or credentials.
        for table in Base.metadata.sorted_tables:
            assert "secret-sentinel" not in str(session.execute(table.select()).all())


async def test_throttle_uses_durable_wall_clock(restartable):
    first = restartable()
    first.quota.clock = lambda: 100
    await first.quota.throttle("a")
    second = restartable()
    second.quota.clock = lambda: 120
    assert not await second.quota.available(second.registry.providers["a"])
    second.quota.clock = lambda: 161
    assert await second.quota.available(second.registry.providers["a"])


async def test_circuit_failure_window_survives_restart_and_single_probe(restartable):
    first = restartable()
    first.quota.clock = lambda: 100
    await first.quota.failure("a")
    await first.quota.failure("a")
    second = restartable()
    second.quota.clock = lambda: 101
    await second.quota.failure("a")
    spec = second.registry.providers["a"]
    assert (await second.quota.state("a")).circuit_state == "OPEN"
    assert not await second.quota.reserve(spec)
    second.quota.clock = lambda: 162
    assert await second.quota.reserve(spec)
    assert (await second.quota.state("a")).circuit_state == "HALF_OPEN"
    competitor = QuotaGovernor(second.settings, second.sessions, clock=lambda: 162)
    assert not await competitor.reserve(spec)
    await second.quota.success("a")
    assert (await competitor.state("a")).circuit_state == "CLOSED"
    with second.sessions() as session:
        assert "PROBE_STARTED" in list(session.scalars(select(ProviderHealthEvent.event_type)))


async def test_failed_or_abandoned_probe_reopens(restartable):
    router = restartable()
    router.quota.clock = lambda: 100
    for _ in range(3):
        await router.quota.failure("a")
    router.quota.clock = lambda: 161
    assert await router.quota.reserve(router.registry.providers["a"])
    await router.quota.failure("a")
    assert (await router.quota.state("a")).circuit_state == "OPEN"
    router.quota.clock = lambda: 222
    assert await router.quota.reserve(router.registry.providers["a"])
    restarted = restartable()
    restarted.quota.clock = lambda: 250
    assert not await restarted.quota.available(restarted.registry.providers["a"])
    assert (await restarted.quota.state("a")).circuit_state == "OPEN"
    assert (await restarted.quota.state("a")).blocked_until == 310


async def test_observed_quota_reset_survives_restart(restartable):
    first = restartable(adapter=MockAdapter("a", error=QuotaExceeded(reset_at=200)))
    first.quota.clock = lambda: 100
    await first.solve(request())
    second = restartable()
    second.quota.clock = lambda: 199
    assert not await second.quota.available(second.registry.providers["a"])
    second.quota.clock = lambda: 200
    assert await second.quota.available(second.registry.providers["a"])
    assert (await second.quota.state("a")).used == 0
    with second.sessions() as session:
        assert "QUOTA_RESET" in list(session.scalars(select(ProviderHealthEvent.event_type)))


@pytest.mark.parametrize("reset", [None, 50, float("nan"), float("inf")])
async def test_unknown_or_invalid_reset_stays_exhausted(restartable, reset):
    first = restartable()
    first.quota.clock = lambda: 100
    await first.quota.exhaust("a", reset_at=reset)
    second = restartable()
    second.quota.clock = lambda: 1_000_000
    assert not await second.quota.available(second.registry.providers["a"])


async def test_relational_registry_and_profile_written(restartable):
    router = restartable()
    result = await router.solve(request())
    with router.sessions() as session:
        assert session.get(Provider, "a").current_access_cost_usd == 0
        assert session.get(Model, ("a", "model")).active
        assert session.get(TaskProfile, result.request_id).minimum_quality_score == 82
        assert session.get(ProviderQuotaState, "a").used == 1


def test_removed_provider_disabled_without_deleting_history(restartable):
    router = restartable()
    registry = Registry()
    registry.persist(router.sessions)
    with router.sessions() as session:
        assert session.get(Provider, "a").status == "DISABLED"
        assert session.get(ProviderQuotaState, "a") is not None


async def test_health_endpoint_reports_durable_observations(make_router):
    router = make_router()
    await router.quota.exhaust("a")
    with TestClient(create_app(router, {"alice": "key"}, "admin")) as client:
        assert client.get("/v1/providers/a/health").status_code == 401
        response = client.get("/v1/providers/a/health", headers={"X-API-Key": "key"})
        assert response.json()["status"] == "QUOTA_EXHAUSTED"
        assert response.json()["source"] == "persisted_observations"
        assert (
            client.get("/v1/providers/missing/health", headers={"X-API-Key": "key"}).status_code
            == 404
        )


async def test_mock_adapter_discovery_contract():
    spec = provider()
    adapter = MockAdapter("a", models=spec.models)
    assert (await adapter.health()).source == "OFFLINE_FIXTURE"
    assert (await adapter.quota()).quota_limit is None
    assert (await adapter.list_models())[0].model_id == "model"


def test_real_application_lifespan_restart(tmp_path, monkeypatch):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("FAIR_DATABASE_URL", "sqlite:///" + (tmp_path / "app.db").as_posix())
    monkeypatch.setenv("FAIR_DEMO_MODE", "1")
    monkeypatch.setenv("FAIR_CONFIG_DIR", str(root / "config"))
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    headers = {"X-API-Key": "client-test-key"}
    admin = {"X-API-Key": "admin-test-key"}
    with TestClient(
        create_app(client_keys={"alice": "client-test-key"}, admin_key="admin-test-key")
    ) as client:
        result = client.post("/v1/solve", headers=headers, json=request().model_dump(mode="json"))
        assert result.status_code == 200
        assert len(result.json()["attempts"]) == 2
        request_id = result.json()["request_id"]
        assert client.post("/v1/system/stop", headers=admin).json()["scope"] == "database"
    with TestClient(
        create_app(client_keys={"alice": "client-test-key"}, admin_key="admin-test-key")
    ) as client:
        result = client.post("/v1/solve", headers=headers, json=request().model_dump(mode="json"))
        assert result.json()["reason_code"] == "SYSTEM_STOPPED"
        health = client.get("/v1/providers/mock_primary/health", headers=headers).json()
        assert health["requests_used"] == 1
        assert client.get(f"/v1/requests/{request_id}", headers=headers).status_code == 200
        assert client.post("/v1/system/resume", headers=admin).json()["stopped"] is False
