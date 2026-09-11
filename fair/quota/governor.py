import asyncio
import logging
from math import isfinite
from time import time

from sqlalchemy import select

from fair.config import RoutingSettings
from fair.constants import SECONDS_IN_DAY
from fair.schemas.db import ProviderHealthEvent, ProviderQuotaState, utcnow

logger = logging.getLogger(__name__)


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
            logger.info("Quota reset for provider %s", row.provider_id)
        if row.circuit_state == "HALF_OPEN" and now >= row.probe_until:
            row.circuit_state = "OPEN"
            row.blocked_until = now + self.settings.cooldown_seconds
            row.probe_until = 0
            self._event(session, row, "PROBE_EXPIRED")
            logger.info("Probe expired for provider %s, circuit reopened", row.provider_id)

    async def state(self, provider_id):
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, provider_id)
                self._recover(session, row)
                return row
        return await asyncio.to_thread(_do)

    async def remaining(self, spec):
        return self._remaining(await self.state(spec.provider_id), spec)

    def _available(self, row, spec):
        return (
            not row.exhausted
            and not row.security_blocked
            and row.circuit_state != "HALF_OPEN"
            and self.clock() >= row.blocked_until
            and (spec.request_limit is None or row.used < spec.request_limit)
        )

    async def available(self, spec):
        return self._available(await self.state(spec.provider_id), spec)

    def _remaining(self, row, spec):
        return None if spec.request_limit is None else max(0, spec.request_limit - row.used)

    async def available_with_remaining(self, spec):
        row = await self.state(spec.provider_id)
        return self._available(row, spec), self._remaining(row, spec)

    async def reserve_with_info(self, spec):
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, spec.provider_id)
                self._recover(session, row)
                remaining = self._remaining(row, spec)
                if not self._available(row, spec):
                    return False, remaining
                if row.circuit_state == "OPEN":
                    row.circuit_state = "HALF_OPEN"
                    row.probe_until = self.clock() + self.settings.timeout_seconds + 5
                    self._event(session, row, "PROBE_STARTED")
                row.used += 1
                row.last_reserved_at = self.clock()
                row.updated_at = utcnow()
                return True, remaining
        return await asyncio.to_thread(_do)

    async def reserve(self, spec):
        reserved, _ = await self.reserve_with_info(spec)
        return reserved

    def _open(self, row):
        row.circuit_state = "OPEN"
        row.blocked_until = self.clock() + self.settings.cooldown_seconds
        row.probe_until = 0
        logger.warning("Circuit opened for provider %s", row.provider_id)

    async def throttle(self, provider_id, retry_after=None):
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, provider_id)
                if row.circuit_state == "HALF_OPEN":
                    self._open(row)
                delay = (
                    retry_after
                    if isinstance(retry_after, (int, float))
                    and isfinite(retry_after)
                    and 0 < retry_after <= SECONDS_IN_DAY
                    else 0
                )
                row.blocked_until = max(
                    row.blocked_until, self.clock() + max(self.settings.cooldown_seconds, delay)
                )
                self._event(session, row, "RATE_LIMITED")
        await asyncio.to_thread(_do)

    async def observe(self, spec, observation):
        if observation.provider_id != spec.provider_id:
            raise ValueError("Quota observation identity mismatch")
        remaining = observation.quota_remaining_estimate
        if (
            remaining is None
            or observation.quota_limit is None
            or remaining > observation.quota_limit
        ):
            return
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, spec.provider_id)
                if spec.request_limit is not None:
                    row.used = max(row.used, spec.request_limit - min(remaining, spec.request_limit))
                if remaining == 0:
                    row.exhausted = True
                if (
                    observation.reset_at is not None
                    and self.clock() < observation.reset_at <= self.clock() + SECONDS_IN_DAY
                ):
                    row.reset_at = observation.reset_at
                self._event(session, row, "QUOTA_OBSERVED")
        await asyncio.to_thread(_do)

    async def exhaust(self, provider_id, reset_at=None):
        """reset_at comes only from a trusted adapter's observed quota reset, if supplied."""
        if reset_at is not None and (not isfinite(reset_at) or reset_at <= self.clock()):
            reset_at = None
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, provider_id)
                row.exhausted = True
                row.reset_at = reset_at
                if row.circuit_state == "HALF_OPEN":
                    self._open(row)
                self._event(session, row, "QUOTA_EXHAUSTED")
        await asyncio.to_thread(_do)

    async def block_security(self, provider_id):
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, provider_id)
                row.security_blocked = True
                self._event(session, row, "AUTHENTICATION_FAILED")
                logger.error("Provider %s security-blocked", provider_id)
        await asyncio.to_thread(_do)

    async def failure(self, provider_id):
        def _do():
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
        await asyncio.to_thread(_do)

    async def success(self, provider_id):
        def _do():
            with self.sessions.begin() as session:
                row = self._row(session, provider_id)
                row.failures = []
                row.circuit_state = "CLOSED"
                row.probe_until = 0
                row.blocked_until = 0
                self._event(session, row, "HEALTHY_RESPONSE")
        await asyncio.to_thread(_do)

    async def effective_status(self, spec):
        if spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            return spec.status
        row = await self.state(spec.provider_id)
        if row.security_blocked:
            return "SECURITY_BLOCKED"
        if row.exhausted or (spec.request_limit is not None and row.used >= spec.request_limit):
            return "QUOTA_EXHAUSTED"
        if row.circuit_state != "CLOSED":
            return "OUTAGE"
        if self.clock() < row.blocked_until:
            return "THROTTLED"
        return spec.status
