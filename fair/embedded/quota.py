"""In-memory quota governor — same circuit breaker logic, zero SQL."""

from dataclasses import dataclass, field
from math import isfinite
from time import time


@dataclass
class QuotaState:
    used: int = 0
    exhausted: bool = False
    security_blocked: bool = False
    blocked_until: float = 0
    reset_at: float | None = None
    circuit_state: str = "CLOSED"
    probe_until: float = 0
    failures: list[float] = field(default_factory=list)


class MemoryQuotaGovernor:
    def __init__(self, settings, clock=time):
        self.settings = settings
        self.clock = clock
        self._states: dict[str, QuotaState] = {}

    def _state(self, provider_id):
        if provider_id not in self._states:
            self._states[provider_id] = QuotaState()
        state = self._states[provider_id]
        self._recover(state)
        return state

    def _recover(self, state):
        now = self.clock()
        if state.reset_at is not None and now >= state.reset_at:
            state.used = 0
            state.exhausted = False
            state.reset_at = None
        if state.circuit_state == "HALF_OPEN" and now >= state.probe_until:
            state.circuit_state = "OPEN"
            state.blocked_until = now + self.settings.cooldown_seconds
            state.probe_until = 0

    def state(self, provider_id):
        return self._state(provider_id)

    def remaining(self, spec):
        state = self._state(spec.provider_id)
        return None if spec.request_limit is None else max(0, spec.request_limit - state.used)

    def _available(self, state, spec):
        return (
            not state.exhausted
            and not state.security_blocked
            and state.circuit_state != "HALF_OPEN"
            and self.clock() >= state.blocked_until
            and (spec.request_limit is None or state.used < spec.request_limit)
        )

    def available(self, spec):
        return self._available(self._state(spec.provider_id), spec)

    def reserve(self, spec):
        state = self._state(spec.provider_id)
        if not self._available(state, spec):
            return False
        if state.circuit_state == "OPEN":
            state.circuit_state = "HALF_OPEN"
            state.probe_until = self.clock() + self.settings.timeout_seconds + 5
        state.used += 1
        return True

    def _open(self, state):
        state.circuit_state = "OPEN"
        state.blocked_until = self.clock() + self.settings.cooldown_seconds
        state.probe_until = 0

    def throttle(self, provider_id, retry_after=None):
        state = self._state(provider_id)
        if state.circuit_state == "HALF_OPEN":
            self._open(state)
        delay = (
            retry_after
            if isinstance(retry_after, (int, float))
            and isfinite(retry_after)
            and 0 < retry_after <= 86400
            else 0
        )
        state.blocked_until = max(
            state.blocked_until, self.clock() + max(self.settings.cooldown_seconds, delay)
        )

    def observe(self, spec, observation):
        if observation.provider_id != spec.provider_id:
            raise ValueError("Quota observation identity mismatch")
        remaining = observation.quota_remaining_estimate
        if remaining is None or observation.quota_limit is None or remaining > observation.quota_limit:
            return
        state = self._state(spec.provider_id)
        if spec.request_limit is not None:
            state.used = max(state.used, spec.request_limit - min(remaining, spec.request_limit))
        if remaining == 0:
            state.exhausted = True
        if (
            observation.reset_at is not None
            and self.clock() < observation.reset_at <= self.clock() + 86400
        ):
            state.reset_at = observation.reset_at

    def exhaust(self, provider_id, reset_at=None):
        if reset_at is not None and (not isfinite(reset_at) or reset_at <= self.clock()):
            reset_at = None
        state = self._state(provider_id)
        state.exhausted = True
        state.reset_at = reset_at
        if state.circuit_state == "HALF_OPEN":
            self._open(state)

    def block_security(self, provider_id):
        self._state(provider_id).security_blocked = True

    def failure(self, provider_id):
        state = self._state(provider_id)
        now = self.clock()
        state.failures = [
            t for t in state.failures if t >= now - self.settings.circuit_window_seconds
        ] + [now]
        if state.circuit_state == "HALF_OPEN" or len(state.failures) >= self.settings.circuit_failures:
            self._open(state)

    def success(self, provider_id):
        state = self._state(provider_id)
        state.failures = []
        state.circuit_state = "CLOSED"
        state.probe_until = 0
        state.blocked_until = 0

    def effective_status(self, spec):
        if spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            return spec.status
        state = self._state(spec.provider_id)
        if state.security_blocked:
            return "SECURITY_BLOCKED"
        if state.exhausted or (spec.request_limit is not None and state.used >= spec.request_limit):
            return "QUOTA_EXHAUSTED"
        if state.circuit_state != "CLOSED":
            return "OUTAGE"
        if self.clock() < state.blocked_until:
            return "THROTTLED"
        return spec.status
