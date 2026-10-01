"""Quota governance with optional cross-process shared accounting."""

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from time import time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_EXHAUSTION_RETRY_SECONDS = 3600
_WINDOW_ZONES = {"DAILY_UTC": UTC, "DAILY_PACIFIC": ZoneInfo("America/Los_Angeles")}


def next_window_reset(window, now):
    """Unix time when FAIR's conservative local quota window clears."""
    if window == "HOURLY":
        return now + 3600
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


def _ledger_failure(error):
    """A fixed code for a SQLite failure, since its message can name the path."""
    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error):
        return "LEDGER_LOCKED"
    return "LEDGER_UNAVAILABLE"


class QuotaLedgerUnavailable(Exception):
    """The shared ledger could not be read or written.

    FAIR then cannot know whether a free request is still available, and the
    two ways of guessing are not symmetrical: guessing low wastes a free
    request, guessing high exceeds a free tier, which is the one outcome this
    ledger exists to prevent. So routing stops and says which it was, rather
    than proceeding on an assumption. Carries a fixed code, never SQLite's
    message, which can name the database path.
    """


class SharedQuotaLedger:
    """SQLite-backed request accounting shared by independent FAIR processes.

    Only quota consumption/exhaustion is shared. Authentication failures,
    billing/security blocks, throttles and circuit-breaker state remain local
    to the FAIR instance because separate applications may use separate keys.
    """

    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as database:
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
        try:
            database.execute("PRAGMA busy_timeout=5000")
            database.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            database.close()
            raise
        return database

    @contextmanager
    def _transaction(self):
        """One transaction on a connection that is always closed afterwards.

        ``with connection:`` only commits or rolls back; it does not close. Leaving
        the connection to the garbage collector ties its file handle to object
        lifetime, so this owns it explicitly on success, early return and error.
        """
        database = self._connect()
        try:
            with database:
                yield database
        except sqlite3.Error as error:
            # A fixed code, never SQLite's message, which can name the database
            # path. Contention is a real answer about the ledger and has to be
            # distinguishable from a defect, which is what it used to look like.
            raise QuotaLedgerUnavailable(_ledger_failure(error)) from None
        finally:
            database.close()

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
        with self._transaction() as database:
            database.execute("BEGIN IMMEDIATE")
            used, _, _ = self._recover(database, pool_id, now)
            return max(0, limit - used)

    def available(self, pool_id, limit, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        with self._transaction() as database:
            database.execute("BEGIN IMMEDIATE")
            used, exhausted, _ = self._recover(database, pool_id, now)
            return not exhausted and (limit is None or used < limit)

    def reserve(self, pool_id, application_id, limit, window, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        application_id = _identity(application_id, "application id")
        with self._transaction() as database:
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

    def release(self, pool_id, application_id, now):
        """Give back a reservation whose request never reached the provider.

        Floored at zero on both counters: a window reset between the reserve and
        the release would otherwise drive a count negative and hand out free
        requests, which is the one error this ledger exists to prevent.
        """
        pool_id = _identity(pool_id, "quota pool id", 256)
        application_id = _identity(application_id, "application id")
        with self._transaction() as database:
            database.execute("BEGIN IMMEDIATE")
            used, _exhausted, reset_at = self._recover(database, pool_id, now)
            database.execute(
                "UPDATE quota_pool_state SET used = ? WHERE pool_id = ?",
                (max(used - 1, 0), pool_id),
            )
            database.execute(
                """
                UPDATE quota_application_usage SET used = MAX(used - 1, 0)
                WHERE pool_id = ? AND application_id = ?
                """,
                (pool_id, application_id),
            )
            return True

    def observe(self, pool_id, observed_limit, remaining, reset_at, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        if remaining is None or observed_limit is None or remaining > observed_limit:
            return
        with self._transaction() as database:
            database.execute("BEGIN IMMEDIATE")
            used, exhausted, current_reset = self._recover(database, pool_id, now)
            used = max(used, observed_limit - remaining)
            if reset_at is not None and now < reset_at <= now + 86400:
                if not exhausted:
                    current_reset = reset_at
                elif remaining == 0 and current_reset is not None:
                    # Another exhaustion signal: the block lasts until the later reset.
                    current_reset = max(current_reset, reset_at)
                # Otherwise an already-exhausted pool keeps its established
                # reset. A positive observation can arrive late (a request that
                # began before the exhaustion was seen) and must neither shorten
                # nor extend the block.
            # Persist exhaustion only when the ledger knows how it will
            # recover. A provider can report zero remaining without a reset
            # timestamp; storing that forever would strand every application
            # until the SQLite file was manually edited. An exhaustion that is
            # already recorded is never cleared here: it ends only at its reset,
            # which _recover applies.
            shared_exhausted = exhausted or (remaining == 0 and current_reset is not None)
            database.execute(
                "UPDATE quota_pool_state SET used = ?, exhausted = ?, reset_at = ? "
                "WHERE pool_id = ?",
                (used, int(shared_exhausted), current_reset, pool_id),
            )

    def exhaust(self, pool_id, reset_at, now):
        pool_id = _identity(pool_id, "quota pool id", 256)
        if reset_at is None or not isfinite(reset_at) or reset_at <= now:
            return
        with self._transaction() as database:
            database.execute("BEGIN IMMEDIATE")
            self._recover(database, pool_id, now)
            database.execute(
                "UPDATE quota_pool_state SET exhausted = 1, reset_at = ? WHERE pool_id = ?",
                (reset_at, pool_id),
            )

    def report(self, pool_ids, now):
        pool_ids = sorted({_identity(value, "quota pool id", 256) for value in pool_ids})
        with self._transaction() as database:
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
    # Retry-After throttling is independent from circuit-breaker cooldown.
    throttled_until: float = 0
    reset_at: float | None = None
    circuit_state: str = "CLOSED"
    probe_until: float = 0
    # Identifies who owns the half-open slot, so a stale attempt cannot release it
    # or close the circuit on behalf of a newer probe. 0 means no probe.
    probe_token: int = 0
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
        self._probe_sequence = 0
        # Monotonic count of ledger operations that failed. Read before and
        # after a solve to tell a busy ledger from an exhausted quota.
        self.ledger_failures = 0
        self._failure_lock = threading.Lock()

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
            state.probe_token = 0

    def state(self, provider_id):
        return self._state(provider_id)

    def _ledger(self, operation, *args, failed):
        """Run a ledger operation fail-closed; never leak sqlite errors to callers.

        Treating an unreadable ledger as an unavailable pool is the right
        direction -- the alternative is dispatching a request FAIR cannot
        account for -- but it is indistinguishable from a spent quota to
        everything upstream. So the failure is counted as well as logged, and
        the router reads that count rather than reporting a busy ledger as
        though no free model had been eligible.
        """
        try:
            return operation(*args)
        except (QuotaLedgerUnavailable, sqlite3.Error) as error:
            code = (
                str(error) if isinstance(error, QuotaLedgerUnavailable) else _ledger_failure(error)
            )
            with self._failure_lock:
                self.ledger_failures += 1
            logger.warning(
                "Shared quota ledger unavailable (%s); treating pool as unavailable", code
            )
            return failed

    async def _ledger_async(self, operation, *args, failed):
        """Move blocking sqlite work off the application event loop."""
        return await asyncio.to_thread(self._ledger, operation, *args, failed=failed)

    def remaining(self, spec):
        if self.shared_ledger is not None:
            return self._ledger(
                self.shared_ledger.remaining,
                self.pool_id(spec.provider_id),
                spec.request_limit,
                self.clock(),
                failed=None if spec.request_limit is None else 0,
            )
        state = self._state(spec.provider_id)
        return None if spec.request_limit is None else max(0, spec.request_limit - state.used)

    async def remaining_async(self, spec):
        if self.shared_ledger is not None:
            return await self._ledger_async(
                self.shared_ledger.remaining,
                self.pool_id(spec.provider_id),
                spec.request_limit,
                self.clock(),
                failed=None if spec.request_limit is None else 0,
            )
        return self.remaining(spec)

    def _available_local(self, state, spec, *, include_limit=True):
        return (
            not state.exhausted
            and not state.security_blocked
            and state.circuit_state != "HALF_OPEN"
            and self.clock() >= state.blocked_until
            and self.clock() >= state.throttled_until
            and (not include_limit or spec.request_limit is None or state.used < spec.request_limit)
        )

    def available(self, spec):
        state = self._state(spec.provider_id)
        if not self._available_local(state, spec, include_limit=self.shared_ledger is None):
            return False
        if self.shared_ledger is None:
            return True
        return self._ledger(
            self.shared_ledger.available,
            self.pool_id(spec.provider_id),
            spec.request_limit,
            self.clock(),
            failed=False,
        )

    async def available_async(self, spec):
        state = self._state(spec.provider_id)
        if not self._available_local(state, spec, include_limit=self.shared_ledger is None):
            return False
        if self.shared_ledger is None:
            return True
        return await self._ledger_async(
            self.shared_ledger.available,
            self.pool_id(spec.provider_id),
            spec.request_limit,
            self.clock(),
            failed=False,
        )

    def reserve(self, spec, application_id=None, probe_timeout=None):
        return self.reserve_probe(spec, application_id, probe_timeout)[0]

    def reserve_probe(self, spec, application_id=None, probe_timeout=None):
        """Reserve one request; also returns the half-open probe token, or 0.

        ``probe_timeout`` is how long the caller's attempt may legitimately run. A
        half-open probe is owned for that long, not for a fixed default, so a large
        completion is not declared abandoned while it is still generating.
        """
        state = self._state(spec.provider_id)
        if not self._available_local(state, spec, include_limit=self.shared_ledger is None):
            return False, 0
        token = self._claim_probe(state, probe_timeout)
        if self.shared_ledger is not None:
            if not self._ledger(
                self.shared_ledger.reserve,
                self.pool_id(spec.provider_id),
                application_id or self.application_id,
                spec.request_limit,
                spec.request_limit_window,
                self.clock(),
                failed=False,
            ):
                self._release_probe(state, token)
                return False, 0
            state.used += 1
        else:
            state.used += 1
            if (
                state.reset_at is None
                and spec.request_limit is not None
                and spec.request_limit_window is not None
            ):
                state.reset_at = next_window_reset(spec.request_limit_window, self.clock())
        return True, token

    async def reserve_async(self, spec, application_id=None, probe_timeout=None):
        return (await self.reserve_probe_async(spec, application_id, probe_timeout))[0]

    async def reserve_probe_async(self, spec, application_id=None, probe_timeout=None):
        state = self._state(spec.provider_id)
        if not self._available_local(state, spec, include_limit=self.shared_ledger is None):
            return False, 0
        # Claim the half-open slot before awaiting SQLite. Otherwise several
        # concurrent coroutines can all observe OPEN and dispatch probe calls.
        token = self._claim_probe(state, probe_timeout)
        if self.shared_ledger is not None:
            if not await self._ledger_async(
                self.shared_ledger.reserve,
                self.pool_id(spec.provider_id),
                application_id or self.application_id,
                spec.request_limit,
                spec.request_limit_window,
                self.clock(),
                failed=False,
            ):
                self._release_probe(state, token)
                return False, 0
            state.used += 1
        else:
            state.used += 1
            if (
                state.reset_at is None
                and spec.request_limit is not None
                and spec.request_limit_window is not None
            ):
                state.reset_at = next_window_reset(spec.request_limit_window, self.clock())
        return True, token

    def release(self, spec, application_id=None, *, refund=True, probe_token=0):
        """Undo what a reservation claimed when no request reached the provider.

        Two things are given back, and they are separable because FAIR is certain
        of one more often than the other. The half-open probe is always released:
        it was claimed to test a provider, and an attempt that never called one
        tested nothing, so leaving it held sidelines a healthy provider for the
        probe window and then a fresh cooldown on top. ``probe_token`` names which
        probe this attempt owns; an attempt that owns none (0) releases nothing, and
        a stale token cannot release a newer probe.

        The request itself is refunded only when FAIR raised the failure before
        dispatch and so knows the provider was never asked. Where that is
        uncertain -- a cancellation, which may have been torn down with the
        request already in flight -- the count stands. Over-counting costs a free
        request; under-counting exceeds a free tier, which is the thing this
        governor exists to prevent.
        """
        state = self._state(spec.provider_id)
        self._release_probe(state, probe_token)
        if not refund:
            return
        if self.shared_ledger is not None:
            self._ledger(
                self.shared_ledger.release,
                self.pool_id(spec.provider_id),
                application_id or self.application_id,
                self.clock(),
                failed=None,
            )
        state.used = max(state.used - 1, 0)

    async def release_async(self, spec, application_id=None, *, refund=True, probe_token=0):
        state = self._state(spec.provider_id)
        self._release_probe(state, probe_token)
        if not refund:
            return
        if self.shared_ledger is not None:
            await self._ledger_async(
                self.shared_ledger.release,
                self.pool_id(spec.provider_id),
                application_id or self.application_id,
                self.clock(),
                failed=None,
            )
        state.used = max(state.used - 1, 0)

    def _claim_probe(self, state, probe_timeout=None):
        """Take the half-open slot; returns its ownership token, or 0 if not a probe."""
        if state.circuit_state != "OPEN":
            return 0
        self._probe_sequence += 1
        state.circuit_state = "HALF_OPEN"
        state.probe_token = self._probe_sequence
        lease = self.settings.timeout_seconds if probe_timeout is None else probe_timeout
        state.probe_until = self.clock() + lease + 5
        return state.probe_token

    def _release_probe(self, state, token):
        """Give the slot back, only if ``token`` still owns it."""
        if token and state.circuit_state == "HALF_OPEN" and state.probe_token == token:
            state.circuit_state = "OPEN"
            state.probe_until = 0
            state.probe_token = 0

    def release_probe(self, provider_id, token):
        """Release a probe whose outcome is not a verdict on the provider's health.

        The circuit returns to OPEN with no new cooldown, so the next request may
        probe at once. A token that no longer owns the slot is ignored, so a stale
        attempt cannot release a newer probe.
        """
        self._release_probe(self._state(provider_id), token)

    def _open(self, state):
        state.circuit_state = "OPEN"
        state.blocked_until = self.clock() + self.settings.cooldown_seconds
        state.probe_until = 0
        state.probe_token = 0

    def _stale_probe_outcome(self, state, probe_token):
        """A completion from a probe that no longer owns the half-open slot."""
        return bool(probe_token) and (
            state.circuit_state == "HALF_OPEN" and state.probe_token != probe_token
        )

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
        state.throttled_until = max(
            state.throttled_until, self.clock() + max(self.settings.cooldown_seconds, delay)
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
            if state.reset_at is None:
                state.reset_at = (
                    next_window_reset(spec.request_limit_window, self.clock())
                    if spec.request_limit_window is not None
                    else self.clock() + _EXHAUSTION_RETRY_SECONDS
                )
        if (
            observation.reset_at is not None
            and self.clock() < observation.reset_at <= self.clock() + 86400
        ):
            state.reset_at = observation.reset_at
        if self.shared_ledger is not None:
            self._ledger(
                self.shared_ledger.observe,
                self.pool_id(spec.provider_id),
                observed_limit,
                remaining,
                state.reset_at if remaining == 0 else observation.reset_at,
                self.clock(),
                failed=None,
            )

    async def observe_async(self, spec, observation):
        if observation.provider_id != spec.provider_id:
            raise ValueError("Quota observation identity mismatch")
        remaining = observation.quota_remaining_estimate
        observed_limit = observation.quota_limit
        if remaining is None or observed_limit is None or remaining > observed_limit:
            return
        state = self._state(spec.provider_id)
        if spec.request_limit is not None:
            state.used = max(state.used, spec.request_limit - min(remaining, spec.request_limit))
        if (
            observation.reset_at is not None
            and self.clock() < observation.reset_at <= self.clock() + 86400
        ):
            state.reset_at = observation.reset_at
        if remaining == 0:
            state.exhausted = True
            if state.reset_at is None:
                state.reset_at = (
                    next_window_reset(spec.request_limit_window, self.clock())
                    if spec.request_limit_window is not None
                    else self.clock() + _EXHAUSTION_RETRY_SECONDS
                )
        if self.shared_ledger is not None:
            await self._ledger_async(
                self.shared_ledger.observe,
                self.pool_id(spec.provider_id),
                observed_limit,
                remaining,
                state.reset_at if remaining == 0 else observation.reset_at,
                self.clock(),
                failed=None,
            )

    def exhaust(self, provider, reset_at=None):
        spec = provider if hasattr(provider, "provider_id") else None
        provider_id = spec.provider_id if spec is not None else provider
        if reset_at is not None and (not isfinite(reset_at) or reset_at <= self.clock()):
            reset_at = None
        if reset_at is None:
            window = spec.request_limit_window if spec is not None else None
            reset_at = (
                next_window_reset(window, self.clock())
                if window is not None
                else self.clock() + _EXHAUSTION_RETRY_SECONDS
            )
        state = self._state(provider_id)
        state.exhausted = True
        state.reset_at = reset_at
        if state.circuit_state == "HALF_OPEN":
            self._open(state)
        if self.shared_ledger is not None and spec is not None:
            self._ledger(
                self.shared_ledger.exhaust,
                self.pool_id(provider_id),
                reset_at,
                self.clock(),
                failed=None,
            )

    async def exhaust_async(self, provider, reset_at=None):
        spec = provider if hasattr(provider, "provider_id") else None
        provider_id = spec.provider_id if spec is not None else provider
        if reset_at is not None and (not isfinite(reset_at) or reset_at <= self.clock()):
            reset_at = None
        if reset_at is None:
            window = spec.request_limit_window if spec is not None else None
            reset_at = (
                next_window_reset(window, self.clock())
                if window is not None
                else self.clock() + _EXHAUSTION_RETRY_SECONDS
            )
        state = self._state(provider_id)
        state.exhausted = True
        state.reset_at = reset_at
        if state.circuit_state == "HALF_OPEN":
            self._open(state)
        if self.shared_ledger is not None and spec is not None:
            await self._ledger_async(
                self.shared_ledger.exhaust,
                self.pool_id(provider_id),
                reset_at,
                self.clock(),
                failed=None,
            )

    def block_security(self, provider_id):
        self._state(provider_id).security_blocked = True

    def unblock_security(self, provider_id):
        state = self._state(provider_id)
        state.security_blocked = False
        state.failures = []
        state.circuit_state = "CLOSED"
        state.blocked_until = 0
        state.throttled_until = 0

    def failure(self, provider_id, probe_token=0):
        state = self._state(provider_id)
        if self._stale_probe_outcome(state, probe_token):
            return
        now = self.clock()
        state.failures = [
            t for t in state.failures if t >= now - self.settings.circuit_window_seconds
        ] + [now]
        if (
            state.circuit_state == "HALF_OPEN"
            or len(state.failures) >= self.settings.circuit_failures
        ):
            self._open(state)

    def success(self, provider_id, probe_token=0):
        state = self._state(provider_id)
        if self._stale_probe_outcome(state, probe_token):
            return
        state.failures = []
        state.circuit_state = "CLOSED"
        state.probe_until = 0
        state.probe_token = 0
        state.blocked_until = 0

    def effective_status(self, spec):
        if spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            return spec.status
        from fair.governor.policy import AdmissionDenied, admit_provider

        try:
            admit_provider(spec)
        except AdmissionDenied:
            return "REVIEW_EXPIRED"
        state = self._state(spec.provider_id)
        if state.security_blocked:
            return "SECURITY_BLOCKED"
        if state.exhausted:
            return "QUOTA_EXHAUSTED"
        if self.shared_ledger is not None and not self._ledger(
            self.shared_ledger.available,
            self.pool_id(spec.provider_id),
            spec.request_limit,
            self.clock(),
            failed=False,
        ):
            return "QUOTA_EXHAUSTED"
        if self.shared_ledger is None and (
            spec.request_limit is not None and state.used >= spec.request_limit
        ):
            return "QUOTA_EXHAUSTED"
        if state.circuit_state != "CLOSED":
            return "OUTAGE"
        if self.clock() < max(state.blocked_until, state.throttled_until):
            return "THROTTLED"
        return spec.status

    async def effective_status_async(self, spec):
        if spec.status not in {"ACTIVE", "QUOTA_PRESSURE"}:
            return spec.status
        from fair.governor.policy import AdmissionDenied, admit_provider

        try:
            admit_provider(spec)
        except AdmissionDenied:
            return "REVIEW_EXPIRED"
        state = self._state(spec.provider_id)
        if state.security_blocked:
            return "SECURITY_BLOCKED"
        if state.exhausted:
            return "QUOTA_EXHAUSTED"
        if self.shared_ledger is not None and not await self._ledger_async(
            self.shared_ledger.available,
            self.pool_id(spec.provider_id),
            spec.request_limit,
            self.clock(),
            failed=False,
        ):
            return "QUOTA_EXHAUSTED"
        if self.shared_ledger is None and (
            spec.request_limit is not None and state.used >= spec.request_limit
        ):
            return "QUOTA_EXHAUSTED"
        if state.circuit_state != "CLOSED":
            return "OUTAGE"
        if self.clock() < max(state.blocked_until, state.throttled_until):
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
        pools = self._ledger(
            self.shared_ledger.report,
            provider_pools.values(),
            self.clock(),
            failed=[],
        )
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
            "ledger_available": bool(pools) or not provider_pools,
            "pools": pools,
        }

    async def usage_report_async(self, specs):
        specs = list(specs)
        provider_pools = {spec.provider_id: self.pool_id(spec.provider_id) for spec in specs}
        if self.shared_ledger is None:
            return {
                "shared": False,
                "application_id": self.application_id,
                "provider_pools": provider_pools,
                "pools": [],
            }
        pools = await self._ledger_async(
            self.shared_ledger.report,
            provider_pools.values(),
            self.clock(),
            failed=[],
        )
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
            "ledger_available": bool(pools) or not provider_pools,
            "pools": pools,
        }
