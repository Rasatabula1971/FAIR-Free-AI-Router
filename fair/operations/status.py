import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text

from fair.schemas.db import AuditEvent, CacheEntry, ProviderQuotaState, SystemState, TaskRequest

logger = logging.getLogger(__name__)


def _discover_schema_revision():
    versions_dir = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    if not versions_dir.is_dir():
        return "0010"
    revisions = sorted(
        p.stem.split("_", 1)[0]
        for p in versions_dir.iterdir()
        if p.suffix == ".py" and not p.name.startswith("_")
    )
    return revisions[-1] if revisions else "0010"


SCHEMA_REVISION = _discover_schema_revision()


def schema_current(session):
    return set(session.scalars(text("SELECT version_num FROM alembic_version"))) == {
        SCHEMA_REVISION
    }


_schema_cache = {}
_schema_lock = threading.Lock()
_SCHEMA_TTL = 30.0


def readiness(router, credentials):
    if router.scheduler.closed or not credentials.configured:
        return {"status": "not_ready"}
    now = time.monotonic()
    key = id(router)
    with _schema_lock:
        cached = _schema_cache.get(key)
        schema_ok = cached[0] if cached is not None and now < cached[1] else None
    try:
        with router.sessions() as session:
            if schema_ok is None:
                schema_ok = schema_current(session)
                with _schema_lock:
                    _schema_cache[key] = (schema_ok, now + _SCHEMA_TTL)
            state = session.get(SystemState, "global")
            ready = schema_ok and state is not None and not state.stopped
        return {"status": "ready" if ready else "not_ready"}
    except Exception:
        logger.warning("Readiness check failed", exc_info=True)
        return {"status": "not_ready"}


def snapshot(router, credentials, now=None):
    now = now or datetime.now(UTC)
    with router.sessions.begin() as session:
        current = schema_current(session)
        state = session.get(SystemState, "global")
        rows = session.execute(
            select(TaskRequest.status, func.count())
            .where(TaskRequest.created_at >= now - timedelta(days=1), TaskRequest.created_at <= now)
            .group_by(TaskRequest.status)
        )
        counts = {
            name: 0
            for name in [
                "RECEIVED",
                "QUEUED",
                "PROFILED",
                "ROUTING",
                "EXECUTING",
                "VALIDATING",
                "ACCEPTED",
                "UNVERIFIED_RESULT",
                "ESCALATION_REQUIRED",
                "FAILED",
                "CANCELLED",
            ]
        }
        for name, count in rows:
            if name in counts:
                counts[name] = count
        cache_hits = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.event_type == "CACHE_HIT",
                AuditEvent.created_at >= now - timedelta(days=1),
                AuditEvent.created_at <= now,
            )
        )
        active_cache = session.scalar(
            select(func.count())
            .select_from(CacheEntry)
            .where(CacheEntry.expires_at > now.timestamp())
        )
        blocked = session.scalar(
            select(func.count())
            .select_from(ProviderQuotaState)
            .where(ProviderQuotaState.security_blocked.is_(True))
        )
        exhausted = session.scalar(
            select(func.count())
            .select_from(ProviderQuotaState)
            .where(ProviderQuotaState.exhausted.is_(True))
        )
        stopped = state is None or state.stopped
    return {
        "scope": "single_process",
        "observed_at": now.isoformat(),
        "schema_current": current,
        "credentials_configured": credentials.configured,
        "stopped": stopped,
        "scheduler": router.scheduler.snapshot() | {"inflight": len(router.inflight)},
        "requests_last_24h": counts,
        "cache_hits_last_24h": cache_hits,
        "active_cache_entries": active_cache,
        "security_blocked_providers": blocked,
        "quota_exhausted_providers": exhausted,
        "configured_live_adapters": sum(
            bool(getattr(adapter, "live_inference", False))
            for adapter in router.registry.adapters.values()
        ),
    }
