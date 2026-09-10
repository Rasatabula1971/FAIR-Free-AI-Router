from math import isfinite
from time import time

from sqlalchemy import select

from fair.config import RoutingSettings
from fair.schemas.db import ProviderHealthEvent, ProviderQuotaState, utcnow


class QuotaGovernor:
    """Durable conservative reservations behind the single-process scheduler."""

    def __init__(self, settings: RoutingSettings, sessions, clock=time):
        self.settings = settings
        self.sessions = sessions
        self.clock = clock

    def _row(self, session, provider_id):
        row = session.scalar(
            select(ProviderQuotaState)
            .where(ProviderQuotaState.provider_id == provider_id)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("Provider quota state not initialized")
        return row

    def _event(self, session, row, event_type):
        row.updated_at = utcnow()
        session.add(
            ProviderHealthEvent(
                provider_id=row.provider_id, event_type=event_type, circuit_state=row.circuit_state
            )
        )

    def _recover(self, session, row):
        now = self.clock()
        if row.reset_at is not None and now >= row.reset_at:
            row.last_quota_reset_at = max(row.last_quota_reset_at or 0, row.reset_at)
            row.used = 0
            row.exhausted = False
            row.reset_at = None
            self._event(session, row, "QUOTA_RESET")
        if row.circuit_state == "HALF_OPEN" and now >= row.probe_until:
            # An abandoned probe is not success, including after a process crash.
            row.circuit_state = "OPEN"
            row.blocked_until = now + self.settings.cooldown_seconds
            row.probe_until = 0
            self._event(session, row, "PROBE_EXPIRED")

    def state(self, provider_id):
        with self.sessions.begin() as session:
            row = self._row(session, provider_id)
            self._recover(session, row)
            return row

    def remaining(self, spec):
        state = self.state(spec.provider_id)
        return None if spec.request_limit is None else max(0, spec.request_limit - state.used)

    def _available(self, row, spec):
        return (
            not row.exhausted
            and not row.security_blocked
            and row.circuit_state != "HALF_OPEN"
            and self.clock() >= row.blocked_until
            and (spec.request_limit is None or row.used < spec.request_limit)
        )

    def available(self, spec):
        return self._available(self.state(spec.provider_id), spec)

    def reserve(self, spec):
        with self.sessions.begin() as session:
            row = self._row(session, spec.provider_id)
            self._recover(session, row)
            if not self._available(row, spec):
                return False
            if row.circuit_state == "OPEN":
                row.circuit_state = "HALF_OPEN"
                row.probe_until = self.clock() + self.settings.timeout_seconds + 5
                self._event(session, row, "PROBE_STARTED")
            row.used += 1
            row.last_reserved_at = self.clock()
            row.updated_at = utcnow()
            return True

    def _open(self, row):
        row.circuit_state = "OPEN"
        row.blocked_until = self.clock() + self.settings.cooldown_seconds
        row.probe_until = 0

    def throttle(self, provider_id):
        with self.sessions.begin() as session:
            row = self._row(session, provider_id)
            row.blocked_until = self.clock() + self.settings.cooldown_seconds
            if row.circuit_state == "HALF_OPEN":
                self._open(row)
            self._event(session, row, "RATE_LIMITED")

    def exhaust(self, provider_id, reset_at=None):
        """reset_at comes only from a trusted adapter's observed quota reset, if supplied."""
        if reset_at is not None and (not isfinite(reset_at) or reset_at <= self.clock()):
            reset_at = None  # A stale or invalid reset is not permission to clear exhaustion.
        with self.sessions.begin() as session:
            row = self._row(session, provider_id)
            row.exhausted = True
            row.reset_at = reset_at
            if row.circuit_state == "HALF_OPEN":
                self._open(row)
            self._event(session, row, "QUOTA_EXHAUSTED")

    def block_security(self, provider_id):
        with self.sessions.begin() as session:
            row = self._row(session, provider_id)
            row.security_blocked = True
            self._event(session, row, "AUTHENTICATION_FAILED")

    def failure(self, provider_id):
        with self.sessions.begin() as session:
            row = self._row(session, provider_id)
            now = self.clock()
            row.failures = [
                t for t in row.failures if t >= now - self.settings.circuit_window_seconds
            ] + [now]
            if (
                row.circuit_state == "HALF_OPEN"
                or len(row.failures) >= self.settings.circuit_failures
            ):
                self._open(row)
            self._event(session, row, "INFRA_FAILURE")

    def success(self, provider_id):
        with self.sessions.begin() as session:
            row = self._row(session, provider_id)
            row.failures = []
            row.circuit_state = "CLOSED"
            row.probe_until = 0
            row.blocked_until = 0
            self._event(session, row, "HEALTHY_RESPONSE")

    def effective_status(self, spec):
        if spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            return spec.status
        row = self.state(spec.provider_id)
        if row.security_blocked:
            return "SECURITY_BLOCKED"
        if row.exhausted or (spec.request_limit is not None and row.used >= spec.request_limit):
            return "QUOTA_EXHAUSTED"
        if row.circuit_state != "CLOSED":
            return "OUTAGE"
        if self.clock() < row.blocked_until:
            return "THROTTLED"
        return spec.status
