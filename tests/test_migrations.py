import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, bindparam, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]


def recovery_roundtrip(connection):
    from conftest import provider

    from fair.config import RoutingSettings
    from fair.governor.recovery import ProviderRecovery, Recovery, RequestRecovery
    from fair.providers.registry import Registry
    from fair.router.orchestrator import Router
    from fair.schemas.db import TaskRequest

    sessions = sessionmaker(bind=connection, expire_on_commit=False)
    registry = Registry()
    registry.register(provider())
    router = Router(registry, RoutingSettings(), {"standard": 82}, sessions)
    router.stopped = True
    router.quota.block_security("a")
    recovery = Recovery(router)
    request = ProviderRecovery(
        action="clear_authentication",
        expected_state=recovery.inspect_provider("a")["expected_state"],
        review_reference="migration-test",
    )
    recovery.provider("a", request)
    assert not router.quota.state("a").security_blocked
    with sessions.begin() as session:
        session.add(
            TaskRequest(
                id="interrupted-json-null",
                client_id="alice",
                status="QUEUED",
                profile_json={},
                result_json=None,
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
    result = recovery.requests(
        RequestRecovery(created_before=datetime.now(UTC), review_reference="migration-test")
    )
    assert result["recovered"] == 1


def config(connection):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def migration_roundtrip(connection):
    cfg = config(connection)
    command.upgrade(cfg, "0001")
    connection.execute(
        text("""INSERT INTO audit_events
        (id, request_id, actor_id, event_type, payload_json, created_at)
        VALUES (:id, NULL, 'admin', 'SYSTEM_STOP', '{}', :created_at)""").bindparams(
            bindparam("created_at", type_=DateTime(timezone=True))
        ),
        {"id": str(uuid4()), "created_at": datetime.now(UTC)},
    )
    command.upgrade(cfg, "head")
    assert connection.execute(text("SELECT stopped FROM system_state WHERE id = 'global'")).scalar()
    assert "provider_quota_states" in inspect(connection).get_table_names()
    command.check(cfg)
    command.downgrade(cfg, "0001")
    assert "provider_quota_states" not in inspect(connection).get_table_names()
    assert connection.execute(text("SELECT count(*) FROM audit_events")).scalar() == 1
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_sqlite_migration_preserves_history_and_stop(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "migration.db").as_posix())
    try:
        with engine.begin() as connection:
            migration_roundtrip(connection)
            recovery_roundtrip(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("FAIR_TEST_POSTGRES_URL"), reason="PostgreSQL test URL not configured"
)
def test_postgres_migrations_and_audit_trigger():
    # Each run creates a unique test schema and rolls the entire transaction back.
    engine = create_engine(os.environ["FAIR_TEST_POSTGRES_URL"])
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                schema = "fair_test_" + uuid4().hex
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                migration_roundtrip(connection)
                recovery_roundtrip(connection)
                for statement in (
                    "UPDATE audit_events SET event_type = 'tampered'",
                    "DELETE FROM audit_events",
                    "TRUNCATE audit_events",
                ):
                    with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                        connection.execute(text(statement))
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
