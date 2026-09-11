"""Explicit maintenance operations; no inferred quota resets or provider activation."""

import hashlib
import json
from datetime import UTC
from math import isfinite
from time import time
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import String, cast, or_, select

from fair.constants import SECONDS_IN_DAY
from fair.schemas.db import (
    AuditEvent,
    ProviderHealthEvent,
    ProviderQuotaState,
    SystemState,
    TaskRequest,
    utcnow,
)
from fair.schemas.domain import DTO


class ProviderRecovery(DTO):
    action: Literal["clear_authentication", "rearm_circuit", "confirm_quota_reset"]
    expected_state: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_reference: str = Field(min_length=1, max_length=128, pattern=r"\S")
    observed_reset_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def reset_evidence(self):
        if (self.action == "confirm_quota_reset") != (self.observed_reset_at is not None):
            raise ValueError("Quota reset evidence must match the recovery action")
        return self


class RequestRecovery(DTO):
    created_before: AwareDatetime
    review_reference: str = Field(min_length=1, max_length=128, pattern=r"\S")
    limit: int = Field(default=100, ge=1, le=1000)


class RecoveryDenied(Exception):
    def __init__(self, reason, status=409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def snapshot(row):
    result = {
        name: getattr(row, name)
        for name in (
            "provider_id",
            "used",
            "exhausted",
            "security_blocked",
            "blocked_until",
            "reset_at",
            "circuit_state",
            "probe_until",
            "failures",
            "last_reserved_at",
            "last_quota_reset_at",
        )
    }
    updated = row.updated_at
    result["updated_at"] = (
        updated.astimezone(UTC) if updated.tzinfo else updated.replace(tzinfo=UTC)
    ).isoformat()
    return result


def fingerprint(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()


def review_hash(reference):
    return hashlib.sha256(reference.encode()).hexdigest()


class Recovery:
    def __init__(self, router, clock=time):
        self.router, self.clock = router, clock

    def _now(self):
        now = self.clock()
        if not isfinite(now):
            raise RecoveryDenied("RECOVERY_CLOCK_UNAVAILABLE", 503)
        return now

    def _maintenance(self, session):
        state = session.scalar(
            select(SystemState).where(SystemState.id == "global").with_for_update()
        )
        if state is None or not state.stopped:
            raise RecoveryDenied("SYSTEM_MUST_BE_STOPPED")
        if self.router.inflight or self.router.scheduler.active is not None:
            raise RecoveryDenied("REQUESTS_STILL_IN_FLIGHT")

    def inspect_provider(self, provider_id):
        if provider_id not in self.router.registry.providers:
            raise RecoveryDenied("PROVIDER_NOT_FOUND", 404)
        with self.router.sessions() as session:
            row = session.get(ProviderQuotaState, provider_id)
            state = snapshot(row)
            return {"state": state, "expected_state": fingerprint(state)}

    def provider(self, provider_id, request):
        if provider_id not in self.router.registry.providers:
            raise RecoveryDenied("PROVIDER_NOT_FOUND", 404)
        now = self._now()
        with self.router.sessions.begin() as session:
            self._maintenance(session)
            row = session.scalar(
                select(ProviderQuotaState)
                .where(ProviderQuotaState.provider_id == provider_id)
                .with_for_update()
            )
            before = snapshot(row)
            if fingerprint(before) != request.expected_state:
                raise RecoveryDenied("RECOVERY_STATE_CHANGED")
            if request.action == "clear_authentication":
                if not row.security_blocked:
                    raise RecoveryDenied("NO_RECOVERY_NEEDED")
                row.security_blocked = False
            elif request.action == "rearm_circuit":
                if row.circuit_state == "CLOSED" and not row.failures and row.blocked_until <= now:
                    raise RecoveryDenied("NO_RECOVERY_NEEDED")
                # Permit one existing half-open probe after resume; never assert health.
                row.circuit_state, row.blocked_until, row.probe_until, row.failures = (
                    "OPEN",
                    now,
                    0,
                    [],
                )
            else:
                reset = request.observed_reset_at.timestamp()
                if reset > now or reset < now - SECONDS_IN_DAY:
                    raise RecoveryDenied("INVALID_RESET_OBSERVATION")
                if (row.last_quota_reset_at is not None and reset <= row.last_quota_reset_at) or (
                    row.last_reserved_at is not None and reset <= row.last_reserved_at
                ):
                    raise RecoveryDenied("RESET_OBSERVATION_ALREADY_ACCOUNTED_FOR")
                if not row.exhausted and not row.used:
                    raise RecoveryDenied("NO_RECOVERY_NEEDED")
                row.used, row.exhausted, row.reset_at = 0, False, None
                row.last_quota_reset_at = reset
            row.updated_at = utcnow()
            after = snapshot(row)
            report = {
                "provider_id": provider_id,
                "action": request.action,
                "review_reference_hash": review_hash(request.review_reference),
                "before": before,
                "after": after,
                "expected_state": fingerprint(after),
            }
            session.add(
                AuditEvent(actor_id="admin", event_type="PROVIDER_RECOVERY", payload_json=report)
            )
            session.add(
                ProviderHealthEvent(
                    provider_id=provider_id,
                    event_type="OPERATOR_RECOVERY",
                    circuit_state=row.circuit_state,
                )
            )
        return report

    def requests(self, request):
        if request.created_before.timestamp() > self._now():
            raise RecoveryDenied("INVALID_RECOVERY_CUTOFF")
        with self.router.sessions.begin() as session:
            self._maintenance(session)
            rows = list(
                session.scalars(
                    select(TaskRequest)
                    .where(
                        TaskRequest.status.in_(
                            ["RECEIVED", "QUEUED", "PROFILED", "ROUTING", "EXECUTING", "VALIDATING"]
                        ),
                        TaskRequest.created_at < request.created_before.astimezone(UTC),
                        or_(
                            TaskRequest.result_json.is_(None),
                            cast(TaskRequest.result_json, String) == "null",
                        ),
                    )
                    .order_by(TaskRequest.created_at, TaskRequest.id)
                    .limit(request.limit)
                    .with_for_update()
                )
            )
            for row in rows:
                row.status = "FAILED"
                row.result_json = {
                    "request_id": row.id,
                    "status": "FAILED",
                    "reason_code": "PROCESS_INTERRUPTED",
                    "output": None,
                    "paid_inference_executed": False,
                }
                session.add(
                    AuditEvent(
                        request_id=row.id,
                        actor_id="admin",
                        event_type="REQUEST_RECOVERED",
                        payload_json={
                            "reason_code": "PROCESS_INTERRUPTED",
                            "review_reference_hash": review_hash(request.review_reference),
                        },
                    )
                )
            result = {
                "recovered": len(rows),
                "limit": request.limit,
                "created_before": request.created_before.isoformat(),
                "review_reference_hash": review_hash(request.review_reference),
            }
            session.add(
                AuditEvent(actor_id="admin", event_type="REQUEST_RECOVERY", payload_json=result)
            )
        return result
