from collections import deque
from dataclasses import dataclass, field
from time import monotonic

from fair.config import RoutingSettings


@dataclass
class QuotaState:
    used: int = 0
    blocked_until: float = 0
    exhausted: bool = False
    failures: deque = field(default_factory=deque)


class QuotaGovernor:
    """Single-process conservative reservations. Unknown exhaustion needs operator reset."""

    def __init__(self, settings: RoutingSettings, clock=monotonic):
        self.settings = settings
        self.clock = clock
        self.states: dict[str, QuotaState] = {}

    def state(self, provider_id):
        return self.states.setdefault(provider_id, QuotaState())

    def remaining(self, spec):
        return (
            None
            if spec.request_limit is None
            else max(0, spec.request_limit - self.state(spec.provider_id).used)
        )

    def available(self, spec):
        state = self.state(spec.provider_id)
        return (
            not state.exhausted
            and self.clock() >= state.blocked_until
            and (self.remaining(spec) is None or self.remaining(spec) > 0)
        )

    def reserve(self, spec):
        if not self.available(spec):
            return False
        self.state(spec.provider_id).used += 1
        return True

    def throttle(self, provider_id):
        self.state(provider_id).blocked_until = self.clock() + self.settings.cooldown_seconds

    def failure(self, provider_id):
        state = self.state(provider_id)
        now = self.clock()
        state.failures.append(now)
        while state.failures and state.failures[0] < now - self.settings.circuit_window_seconds:
            state.failures.popleft()
        if len(state.failures) >= self.settings.circuit_failures:
            self.throttle(provider_id)

    def success(self, provider_id):
        self.state(provider_id).failures.clear()
