import json
from pathlib import Path

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.main import create_app
from fair.operations.preflight import check
from fair.operations.status import SCHEMA_REVISION, readiness, snapshot
from fair.security.credentials import APIKeys


def stamp(router, revision=SCHEMA_REVISION):
    with router.sessions.begin() as session:
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision}
        )


def credentials():
    return APIKeys({"private-client": "private-client-key"}, "private-admin-key")


def test_schema_constant_matches_migrations():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_current_head() == SCHEMA_REVISION


def test_ready_live_and_admin_status_have_separate_meanings(make_router):
    router = make_router()
    stamp(router)
    app = create_app(router, {"private-client": "private-client-key"}, "private-admin-key")
    with TestClient(app) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/livez").json() == {"status": "alive"}
        assert client.get("/v1/system/status").status_code == 403
        assert (
            client.get("/v1/system/status", headers={"X-API-Key": "private-client-key"}).status_code
            == 403
        )
        response = client.get("/v1/system/status", headers={"X-API-Key": "private-admin-key"})
        assert response.status_code == 200 and response.json()["schema_current"]
        assert "private-" not in response.text
        router.stopped = True
        assert client.get("/readyz").status_code == 503
        assert client.get("/livez").status_code == 200
        router.stopped = False
        router.scheduler.closed = True
        assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize("revision", [None, "0007", "future"])
def test_missing_stale_future_schema_fails_closed(make_router, revision):
    router = make_router()
    if revision:
        stamp(router, revision)
    assert readiness(router, credentials()) == {"status": "not_ready"}


@pytest.mark.parametrize("clients,admin", [({}, "admin"), ({"a": "client"}, "")])
def test_missing_operational_credentials_not_ready(make_router, clients, admin):
    router = make_router()
    stamp(router)
    assert readiness(router, APIKeys(clients, admin))["status"] == "not_ready"


def test_database_failure_does_not_leak_diagnostics(make_router, monkeypatch):
    router = make_router()

    def broken():
        raise RuntimeError("postgres://secret-user:secret-password@private-host")

    app = create_app(router, {"a": "client-key"}, "admin-key")
    with TestClient(app) as client:
        monkeypatch.setattr(router, "sessions", broken)
        response = client.get("/readyz")
        assert response.status_code == 503 and response.json() == {"status": "not_ready"}
        response = client.get("/v1/system/status", headers={"X-API-Key": "admin-key"})
        assert response.status_code == 503 and response.json() == {
            "detail": "OPERATIONAL_STATUS_UNAVAILABLE"
        }
        assert client.get("/livez").status_code == 200


async def test_snapshot_counts_owned_work_without_reading_payloads(make_router):
    from conftest import provider

    from fair.providers.mock import MockAdapter
    from fair.schemas.api import SolveRequest

    router = make_router([(provider(), MockAdapter("a", "4"))], cache_enabled=True)
    stamp(router)
    req = SolveRequest(
        client_id="private-client",
        task="private-task",
        validation={"kind": "arithmetic", "expression": "2 + 2"},
    )
    await router.solve(req)
    await router.solve(req)
    router.quota.block_security("a")
    router.quota.exhaust("a")
    result = snapshot(router, credentials())
    assert result["requests_last_24h"]["ACCEPTED"] == 2
    assert result["cache_hits_last_24h"] == result["active_cache_entries"] == 1
    assert result["security_blocked_providers"] == result["quota_exhausted_providers"] == 1
    assert "private" not in json.dumps(result)


@pytest.fixture
def preflight_config(tmp_path, monkeypatch):
    for name, value in {
        "FAIR_CLIENT_KEYS": '{"client":"secret-client-key"}',
        "FAIR_ADMIN_KEY": "secret-admin-key",
        "FAIR_DEMO_MODE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ["FAIR_CLIENT_KEYS_FILE", "FAIR_ADMIN_KEY_FILE", "FAIR_DATABASE_URL"]:
        monkeypatch.delenv(name, raising=False)
    values = {
        "routing.yaml": {},
        "quality_thresholds.yaml": {"standard": 82},
        "providers.yaml": {"providers": []},
        "live_adapters.yaml": {"enabled": False},
    }
    for name, value in values.items():
        (tmp_path / name).write_text(yaml.safe_dump(value), encoding="utf-8")
    return tmp_path


def test_preflight_reads_only_and_masks_paths_keys(preflight_config):
    result = check(preflight_config)
    assert result["status"] == "PASS" and result["provider_calls"] == 0
    assert result["database_checked"] is False
    assert "secret" not in json.dumps(result) and str(preflight_config) not in json.dumps(result)


@pytest.mark.parametrize(
    "filename,value",
    [
        ("routing.yaml", "invalid: true"),
        ("providers.yaml", "providers: [broken]"),
        ("quality_thresholds.yaml", "standard: -1"),
        ("live_adapters.yaml", "enabled: true\nollama_url: http://remote"),
    ],
)
def test_preflight_rejects_invalid_inputs(preflight_config, filename, value):
    (preflight_config / filename).write_text(value, encoding="utf-8")
    assert check(preflight_config)["status"] == "FAIL"


def test_demo_is_explicit_and_missing_database_is_not_created(preflight_config, monkeypatch):
    monkeypatch.setenv("FAIR_DEMO_MODE", "1")
    assert check(preflight_config)["status"] == "FAIL"
    assert check(preflight_config, allow_demo=True)["status"] == "PASS"
    path = preflight_config / "not-created.db"
    monkeypatch.setenv("FAIR_DATABASE_URL", "sqlite:///" + path.as_posix())
    assert check(preflight_config, allow_demo=True, check_database=True)["status"] == "FAIL"
    assert not path.exists()


def test_preflight_database_check_does_not_migrate(preflight_config, monkeypatch):
    from fair.schemas.db import database

    path = preflight_config / "existing.db"
    url = "sqlite:///" + path.as_posix()
    engine, _ = database(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0007')"))
    monkeypatch.setenv("FAIR_DATABASE_URL", url)
    assert check(preflight_config, check_database=True)["status"] == "FAIL"
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        connection.execute(text("UPDATE alembic_version SET version_num='0008'"))
    assert check(preflight_config, check_database=True)["status"] == "PASS"
    engine.dispose()


def test_build_context_excludes_private_files():
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".dockerignore").read_text().splitlines()
    assert {"secrets/", "backups/", "*.dump", "*.backup", ".env", ".env.*"} <= set(ignored)
