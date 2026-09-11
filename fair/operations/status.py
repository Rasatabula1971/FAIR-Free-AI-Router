import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from fair.schemas.db import AuditEvent, CacheEntry, ProviderQuotaState, SystemState, TaskRequest

logger = logging.getLogger(__name__)

SCHEMA_REVISION = "0009"


def schema_current(session):
    return set(session.scalars(text("SELECT version_num FROM alembic_version"))) == {
        SCHEMA_REVISION
    }


def readiness(router, credentials):
    try:
        with router.sessions() as session:
            current = schema_current(session)
            state = session.get(SystemState, "global")
            ready = (
                current
                and state is not None
                and not state.stopped
                and not router.scheduler.closed
                and credentials.configured
            )
        return {"status": "ready" if ready else "not_ready"}
    except Exception:
        logger.warning("Readiness check failed", exc_info=True)
        return {"status": "not_ready"}


def snapshot(router, credentials, now=None):
    now = now or datetime.now(UTC)
    with router.sessions() as session:
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
