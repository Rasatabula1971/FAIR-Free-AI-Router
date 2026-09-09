"""Bounded, non-preemptive scheduling for one router on one asyncio event loop."""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from pydantic import Field

from fair.schemas.domain import DTO

Priority = Literal["P0", "P1", "P2", "P3", "P4"]


class SchedulerSettings(DTO):
    max_queued: int = Field(default=128, ge=1, le=10000)
    max_queued_per_client: int = Field(default=16, ge=1, le=10000)
    max_queued_bytes: int = Field(default=8_000_000, ge=1, le=100_000_000)
    max_request_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)
    queue_timeout_seconds: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    # Most urgent class each authenticated client can request. Others default to P2.
    client_priority_ceiling: dict[str, Priority] = Field(default_factory=dict)


class SchedulingRejected(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(eq=False)
class Ticket:
    client_id: str
    priority: int
    size: int
    deadline: float
    ready: asyncio.Future
    owner: asyncio.Task
    queued: bool = True


class FairScheduler:
    def __init__(self, settings):
        self.settings = settings
        self.queues = [OrderedDict() for _ in range(5)]
        self.active = None
        self.queued = self.queued_bytes = 0
        self.client_counts = {}
        self.closed = False

    def submit(self, client_id, priority, size):
        if self.closed:
            raise SchedulingRejected("SCHEDULER_CLOSED")
        ceiling = self.settings.client_priority_ceiling.get(client_id, "P2")
        if priority < ceiling:
            raise SchedulingRejected("PRIORITY_NOT_ALLOWED")
        if size > self.settings.max_request_bytes:
            raise SchedulingRejected("REQUEST_TOO_LARGE")
        if self.active is not None:
            if self.client_counts.get(client_id, 0) >= self.settings.max_queued_per_client:
                raise SchedulingRejected("CLIENT_QUEUE_FULL")
            if self.queued >= self.settings.max_queued:
                raise SchedulingRejected("QUEUE_FULL")
            if self.queued_bytes + size > self.settings.max_queued_bytes:
                raise SchedulingRejected("QUEUE_BYTES_FULL")
        ticket = Ticket(
            client_id,
            int(priority[1]),
            size,
            monotonic() + self.settings.queue_timeout_seconds,
            asyncio.get_running_loop().create_future(),
            asyncio.current_task(),
        )
        self.queues[ticket.priority].setdefault(client_id, deque()).append(ticket)
        self.queued += 1
        self.queued_bytes += size
        self.client_counts[client_id] = self.client_counts.get(client_id, 0) + 1
        self._dispatch()
        return ticket

    def _remove(self, ticket):
        if not ticket.queued:
            return
        queue = self.queues[ticket.priority]
        queue[ticket.client_id].remove(ticket)
        self.queued -= 1
        self.queued_bytes -= ticket.size
        self.client_counts[ticket.client_id] -= 1
        if not self.client_counts[ticket.client_id]:
            del self.client_counts[ticket.client_id]
        ticket.queued = False
        # Keep the active client's ring position until it gives up its turn.
        if not queue[ticket.client_id] and not (
            self.active is not None
            and (self.active.priority, self.active.client_id) == (ticket.priority, ticket.client_id)
        ):
            del queue[ticket.client_id]

    def _dispatch(self):
        if self.active is not None or self.closed:
            return
        for queue in self.queues:
            while queue:
                ticket = next(iter(queue.values()))[0]
                if monotonic() >= ticket.deadline:
                    self._remove(ticket)
                    ticket.ready.set_result("QUEUE_TIMEOUT")
                    continue
                self.active = ticket
                self._remove(ticket)
                ticket.ready.set_result(None)
                return

    async def wait(self, ticket):
        try:
            reason = await asyncio.wait_for(
                asyncio.shield(ticket.ready), max(0, ticket.deadline - monotonic())
            )
        except TimeoutError:
            raise SchedulingRejected("QUEUE_TIMEOUT") from None
        if reason:
            raise SchedulingRejected(reason)

    def release(self, ticket):
        self._remove(ticket)
        if self.active is ticket:
            queue = self.queues[ticket.priority]
            if queue.get(ticket.client_id):
                queue.move_to_end(ticket.client_id)
            else:
                queue.pop(ticket.client_id, None)
            self.active = None
        self._dispatch()

    def reject_pending(self, reason):
        tickets = [ticket for queue in self.queues for items in queue.values() for ticket in items]
        for ticket in tickets:
            self._remove(ticket)
            ticket.ready.set_result(reason)

    async def close(self):
        self.closed = True
        owners = {
            ticket.owner for queue in self.queues for items in queue.values() for ticket in items
        }
        self.reject_pending("SCHEDULER_CLOSED")
        if self.active is not None:
            owners.add(self.active.owner)
            if (
                self.active.owner is not asyncio.current_task()
                and not self.active.owner.cancelling()
            ):
                self.active.owner.cancel()
        owners.discard(asyncio.current_task())
        if owners:
            cleanup = asyncio.gather(*owners, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    def snapshot(self):
        return {
            "scope": "process",
            "closed": self.closed,
            "active": int(self.active is not None),
            "queued": self.queued,
            "queued_bytes": self.queued_bytes,
            "waiting_clients": len(self.client_counts),
            "queued_by_priority": {
                f"P{index}": sum(map(len, queue.values()))
                for index, queue in enumerate(self.queues)
            },
        }
