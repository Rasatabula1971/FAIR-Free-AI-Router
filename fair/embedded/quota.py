"""Quota governance with optional cross-process shared accounting."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from time import time
from zoneinfo import ZoneInfo

_WINDOW_ZONES = {"DAILY_UTC": UTC, "DAILY_PACIFIC": ZoneInfo("America/Los_Angeles")}


def next_window_reset(window, now):
    """Unix time of the next local midnight for a daily quota window."""
    zone = _WINDOW_ZONES[window]
    local = datetime.fromtimestamp(now, zone)
    tomorrow = (local + timedelta(days=1)).date()
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).timestamp()


def _identity(value, label, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string up to {maximum} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value.strip()


class SharedQuotaLedger:
    """SQLite-backed request accounting shared by independent FAIR processes.

    Only quota consumption/exhaustion is shared. Authentication failures,
    billing/security blocks, throttles and circuit-breaker state remain local
    to the FAIR instance because separate applications may use separate keys.
    """

    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_pool_state (
                    pool_id TEXT PRIMARY KEY,
                    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                    exhausted INTEGER NOT NULL DEFAULT 0 CHECK (exhausted IN (0, 1)),
                    reset_at REAL
                )
                """
            )
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_application_usage (
                    pool_id TEXT NOT NULL,
                    application_id TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                    PRIMARY KEY (pool_id, application_id),
                    FOREIGN KEY (pool_id) REFERENCES quota_pool_state(pool_id)
                        ON DELETE CASCADE
                )
                """
            )

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=5)
        database.execute("PRAGMA busy_timeout=5000")
        database.execute("PRAGMA foreign_keys=ON")
        return database

    def _recover(self, database, pool_id, now):
        row = database.execute(
            "SELECT used, exhausted, reset_at FROM quota_pool_state WHERE pool_id = ?",
            (pool_id,),
        ).fetchone()
        if row is None:
            database.execute("INSERT INTO quota_pool_state(pool_id) VALUES (?)", (pool_id,))
            return 0, False, None
        used, exhausted, reset_at = row
        if reset_at is not None and now >= reset_at:
            database.execute(
                "UPDATE quota_pool_state SET used = 0, exhausted = 0, reset_at = NULL "
                "WHERE pool_id = ?",
                (pool_id,),
            )
            database.execute(
                "DELETE FROM quota_application_usage WHERE pool_id = ?",
                (pool_id,),
            )
            return 0, False, None
        return int(used), bool(exhausted), reset_at

    def remaining(self, pool_id, limit, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        if limit is None:
            return None
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            used, _, _ = self._recover(database, pool_id, now)
            return max(0, limit - used)

    def available(self, pool_id, limit, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            used, exhausted, _ = self._recover(database, pool_id, now)
            return not exhausted and (limit is None or used < limit)

    def reserve(self, pool_id, application_id, limit, window, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        application_id = _identity(application_id, "application id")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            used, exhausted, reset_at = self._recover(database, pool_id, now)
            if exhausted or (limit is not None and used >= limit):
                return False
            if reset_at is None and limit is not None and window is not None:
                reset_at = next_window_reset(window, now)
            database.execute(
                "UPDATE quota_pool_state SET used = ?, reset_at = ? WHERE pool_id = ?",
                (used + 1, reset_at, pool_id),
            )
            database.execute(
                """
                INSERT INTO quota_application_usage(pool_id, application_id, used)
                VALUES (?, ?, 1)
                ON CONFLICT(pool_id, application_id)
                DO UPDATE SET used = used + 1
                """,
                (pool_id, application_id),
            )
            return True

    def observe(self, pool_id, observed_limit, remaining, reset_at, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        if remaining is None or observed_limit is None or remaining > observed_limit:
            return
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            used, _, current_reset = self._recover(database, pool_id, now)
            used = max(used, observed_limit - remaining)
            if reset_at is not None and now < reset_at <= now + 86400:
                current_reset = reset_at
            database.execute(
                "UPDATE quota_pool_state SET used = ?, exhausted = ?, reset_at = ? "
                "WHERE pool_id = ?",
                (used, int(remaining == 0), current_reset, pool_id),
            )

    def exhaust(self, pool_id, reset_at, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        if reset_at is None or not isfinite(reset_at) or reset_at <= now:
            return
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._recover(database, pool_id, now)
            database.execute(
                "UPDATE quota_pool_state SET exhausted = 1, reset_at = ? WHERE pool_id = ?",
                (reset_at, pool_id),
            )

    def report(self, pool_ids, now):
        pool_ids = sorted({_identity(value, "quota pool id", 256) for value in pool_ids})
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            for pool_id in pool_ids:
                self._recover(database, pool_id, now)
            rows = database.execute(
                "SELECT pool_id, used, exhausted, reset_at FROM quota_pool_state"
            ).fetchall()
            usage = database.execute(
                "SELECT pool_id, application_id, used FROM quota_application_usage"
            ).fetchall()
        per_application = {}
        for pool_id, application_id, used in usage:
            per_application.setdefault(pool_id, {})[application_id] = int(used)
        selected = set(pool_ids)
        return [
            {
                "pool_id": pool_id,
                "used": int(used),
                "exhausted": bool(exhausted),
                "reset_at": reset_at,
                "applications": per_application.get(pool_id, {}),
            }
            for pool_id, used, exhausted, reset_at in rows
            if pool_id in selected
        ]


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
    def __init__(
        self,
        settings,
        clock=time,
        *,
        shared_ledger=None,
        application_id="embedded",
        quota_pool_ids=None,
    ):
        self.settings = settings
        self.clock = clock
        self.shared_ledger = shared_ledger
        self.application_id = _identity(application_id, "application id")
        self.quota_pool_ids = dict(quota_pool_ids or {})
        for provider_id, pool_id in self.quota_pool_ids.items():
            _identity(provider_id, "provider id")
            _identity(pool_id, "quota pool id", 256)
        self._states: dict[str, QuotaState] = {}

    def pool_id(self, provider_id):
        return self.quota_pool_ids.get(provider_id, provider_id)

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
        if self.shared_ledger is not None:
            return self.shared_ledger.remaining(
                self.pool_id(spec.provider_id), spec.request_limit, self.clock()
            )
        state = self._state(spec.provider_id)
        return None if spec.request_limit is None else max(0, spec.request_limit - state.used)

    def _available_local(self, state, spec, *, include_limit=True):
        return (
            not state.exhausted
            and not state.security_blocked
            and state.circuit_state != "HALF_OPEN"
            and self.clock() >= state.blocked_until
            and (
                not include_limit
                or spec.request_limit is None
                or state.used < spec.request_limit
            )
        )

    def available(self, spec):
        state = self._state(spec.provider_id)
        if not self._available_local(state, spec, include_limit=self.shared_ledger is None):
            return False
        if self.shared_ledger is None:
            return True
        return self.shared_ledger.available(
            self.pool_id(spec.provider_id), spec.request_limit, self.clock()
        )

    def reserve(self, spec):
        state = self._state(spec.provider_id)
        if not self._available_local(state, spec, include_limit=self.shared_ledger is None):
            return False
        if self.shared_ledger is not None:
            if not self.shared_ledger.reserve(
                self.pool_id(spec.provider_id),
                self.application_id,
                spec.request_limit,
                spec.request_limit_window,
                self.clock(),
            ):
                return False
            state.used += 1
        else:
            state.used += 1
            if (
                state.reset_at is None
                and spec.request_limit is not None
                and spec.request_limit_window is not None
            ):
                state.reset_at = next_window_reset(spec.request_limit_window, self.clock())
        if state.circuit_state == "OPEN":
            state.circuit_state = "HALF_OPEN"
            state.probe_until = self.clock() + self.settings.timeout_seconds + 5
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
        observed_limit = observation.quota_limit
        if remaining is None or observed_limit is None or remaining > observed_limit:
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
        if self.shared_ledger is not None:
            self.shared_ledger.observe(
                self.pool_id(spec.provider_id),
                observed_limit,
                remaining,
                observation.reset_at,
                self.clock(),
            )

    def exhaust(self, provider, reset_at=None):
        spec = provider if hasattr(provider, "provider_id") else None
        provider_id = spec.provider_id if spec is not None else provider
        if reset_at is not None and (not isfinite(reset_at) or reset_at <= self.clock()):
            reset_at = None
        state = self._state(provider_id)
        state.exhausted = True
        state.reset_at = reset_at
        if state.circuit_state == "HALF_OPEN":
            self._open(state)
        if self.shared_ledger is not None and spec is not None:
            shared_reset = reset_at
            if shared_reset is None and spec.request_limit_window is not None:
                shared_reset = next_window_reset(spec.request_limit_window, self.clock())
            self.shared_ledger.exhaust(
                self.pool_id(provider_id), shared_reset, self.clock()
            )

    def block_security(self, provider_id):
        self._state(provider_id).security_blocked = True

    def failure(self, provider_id):
        state = self._state(provider_id)
        now = self.clock()
        state.failures = [
            t for t in state.failures if t >= now - self.settings.circuit_window_seconds
        ] + [now]
        if (
            state.circuit_state == "HALF_OPEN"
            or len(state.failures) >= self.settings.circuit_failures
        ):
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
        if state.exhausted:
            return "QUOTA_EXHAUSTED"
        if self.shared_ledger is not None and not self.shared_ledger.available(
            self.pool_id(spec.provider_id), spec.request_limit, self.clock()
        ):
            return "QUOTA_EXHAUSTED"
        if self.shared_ledger is None and (
            spec.request_limit is not None and state.used >= spec.request_limit
        ):
            return "QUOTA_EXHAUSTED"
        if state.circuit_state != "CLOSED":
            return "OUTAGE"
        if self.clock() < state.blocked_until:
            return "THROTTLED"
        return spec.status

    def usage_report(self, specs):
        specs = list(specs)
        provider_pools = {spec.provider_id: self.pool_id(spec.provider_id) for spec in specs}
        if self.shared_ledger is None:
            return {
                "shared": False,
                "application_id": self.application_id,
                "provider_pools": provider_pools,
                "pools": [],
            }
        pools = self.shared_ledger.report(provider_pools.values(), self.clock())
        limits = {}
        for spec in specs:
            pool_id = provider_pools[spec.provider_id]
            if spec.request_limit is not None:
                limits[pool_id] = min(limits.get(pool_id, spec.request_limit), spec.request_limit)
        for pool in pools:
            limit = limits.get(pool["pool_id"])
            pool["request_limit"] = limit
            pool["remaining"] = None if limit is None else max(0, limit - pool["used"])
        return {
            "shared": True,
            "application_id": self.application_id,
            "ledger_path": self.shared_ledger.path,
            "provider_pools": provider_pools,
            "pools": pools,
        }
