import asyncio
import json

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from apps.api.main import create_app
from fair.providers.mock import MockAdapter
from fair.router.scheduler import FairScheduler, SchedulerSettings, SchedulingRejected
from fair.schemas.api import SolveRequest
from fair.schemas.db import AuditEvent, ModelTaskPerformance, RoutingAttempt, TaskRequest


def request(client="alice", **values):
    return SolveRequest(
        client_id=client,
        task="Calculate the expression",
        validation={"kind": "arithmetic", "expression": "2 + 2"},
        **values,
    )


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class HeldAdapter(MockAdapter):
    def __init__(self):
        super().__init__("a", text="4")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.order = []

    async def complete(self, value):
        self.order.append(value.client_id)
        if len(self.order) == 1:
            self.started.set()
            await self.release.wait()
        return await super().complete(value)


async def hold(make_router, **settings):
    adapter = HeldAdapter()
    router = make_router([(provider(), adapter)], **settings)
    active = asyncio.create_task(router.solve(request()))
    await asyncio.wait_for(adapter.started.wait(), 2)
    return router, adapter, active


async def test_round_robin_fifo_and_continuous_arrivals_cannot_monopolize():
    scheduler = FairScheduler(SchedulerSettings())
    active = scheduler.submit("a", "P2", 1)
    a = [scheduler.submit("a", "P2", 1) for _ in range(5)]
    b = [scheduler.submit("b", "P2", 1) for _ in range(2)]
    c = scheduler.submit("c", "P2", 1)
    order = [b[0], c, a[0], b[1], a[1], *a[2:]]
    for expected in order:
        scheduler.release(active)
        assert scheduler.active is expected
        active = expected
    scheduler.release(active)
    assert scheduler.snapshot()["queued"] == 0
    assert all(not queue for queue in scheduler.queues)
    # Newly arriving work for the current client also goes behind waiting clients.
    active = scheduler.submit("a", "P2", 1)
    waiting = scheduler.submit("b", "P2", 1)
    for _ in range(8):
        more = scheduler.submit(active.client_id, "P2", 1)
        scheduler.release(active)
        assert scheduler.active is waiting
        active, waiting = waiting, more
    scheduler.release(active)
    scheduler.release(waiting)


async def test_strict_priority_is_non_preemptive_and_preserves_class_rotation():
    scheduler = FairScheduler(SchedulerSettings(client_priority_ceiling={"a": "P0", "b": "P0"}))
    active = scheduler.submit("a", "P4", 1)
    low = scheduler.submit("a", "P4", 1)
    other = scheduler.submit("b", "P4", 1)
    priorities = [scheduler.submit("a", f"P{p}", 1) for p in [3, 2, 1, 0]]
    assert scheduler.active is active
    for expected in [*reversed(priorities), other, low]:
        scheduler.release(active)
        assert scheduler.active is expected
        active = expected
    scheduler.release(active)


@pytest.mark.parametrize(
    "settings,client,priority,size,reason",
    [
        ({"max_queued": 1}, "c", "P2", 1, "QUEUE_FULL"),
        ({"max_queued_per_client": 1}, "b", "P4", 1, "CLIENT_QUEUE_FULL"),
        ({"max_queued_bytes": 2}, "c", "P2", 2, "QUEUE_BYTES_FULL"),
        ({"max_request_bytes": 2}, "c", "P2", 3, "REQUEST_TOO_LARGE"),
        ({}, "c", "P1", 1, "PRIORITY_NOT_ALLOWED"),
        ({}, "c", "P0", 1, "PRIORITY_NOT_ALLOWED"),
    ],
)
async def test_admission_limits_do_not_change_queue(settings, client, priority, size, reason):
    scheduler = FairScheduler(SchedulerSettings(**settings))
    active = scheduler.submit("a", "P2", 1)
    waiting = scheduler.submit("b", "P2", 1)
    before = scheduler.snapshot()
    with pytest.raises(SchedulingRejected, match=reason):
        scheduler.submit(client, priority, size)
    assert scheduler.snapshot() == before
    scheduler.release(waiting)
    assert scheduler.queued_bytes == 0 and not scheduler.client_counts
    scheduler.release(active)


async def test_expired_ticket_cannot_dispatch_even_if_timeout_callback_has_not_run(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("fair.router.scheduler.monotonic", lambda: clock[0])
    scheduler = FairScheduler(SchedulerSettings(queue_timeout_seconds=10))
    active = scheduler.submit("a", "P2", 1)
    expired = scheduler.submit("b", "P2", 1)
    clock[0] = 111
    valid = scheduler.submit("c", "P2", 1)
    scheduler.release(active)
    assert scheduler.active is valid
    assert expired.ready.result() == "QUEUE_TIMEOUT"
    scheduler.release(valid)


async def test_router_fairness_and_durable_queue_audit(make_router):
    router, adapter, active = await hold(make_router)
    jobs = [
        asyncio.create_task(router.solve(request(name)))
        for name in ["alice", "alice", "bob", "bob", "carol"]
    ]
    await until(lambda: router.scheduler.queued == 5)

    def _queued_count():
        with router.sessions() as session:
            return len(list(session.scalars(select(TaskRequest).where(TaskRequest.status == "QUEUED"))))

    await until(lambda: _queued_count() == 5)
    adapter.release.set()
    results = await asyncio.gather(active, *jobs)
    order = adapter.order
    assert order[0] == "alice"
    assert set(order[1:3]) == {"bob", "carol"}
    assert sorted(order) == sorted(["alice", "alice", "alice", "bob", "bob", "carol"])
    assert all(result.status == "ACCEPTED" for result in results)
    with router.sessions() as session:
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.request_id == results[-1].request_id)
                .order_by(AuditEvent.created_at)
            )
        )
        assert [event.event_type for event in events[:3]] == ["QUEUED", "SCHEDULED", "PROFILED"]
        assert events[1].payload_json["wait_ms"] >= 0
        assert events[-1].event_type == "ACCEPTED"
        assert "2 + 2" not in json.dumps([event.payload_json for event in events])


@pytest.mark.parametrize("action", ["cancel", "timeout", "stop", "close"])
async def test_pending_work_ends_without_provider_or_quality_charge(make_router, action):
    settings = {"queue_timeout_seconds": 0.02} if action == "timeout" else {}
    router, adapter, active = await hold(make_router, scheduler=settings)
    pending = asyncio.create_task(router.solve(request("bob")))
    await until(lambda: router.scheduler.queued == 1)
    if action == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        if action == "stop":
            router.stopped = True
            router.stopped = False  # Already stopped queued work must not resume.
        if action == "close":
            await router.scheduler.close()
        result = await pending
        assert (
            result.reason_code
            == {"timeout": "QUEUE_TIMEOUT", "stop": "SYSTEM_STOPPED", "close": "SCHEDULER_CLOSED"}[
                action
            ]
        )
        assert result.attempts == [] and result.output is None
    assert router.scheduler.queued == router.scheduler.queued_bytes == 0
    with router.sessions() as session:
        row = session.scalar(select(TaskRequest).where(TaskRequest.client_id == "bob"))
        assert row.status == ("CANCELLED" if action == "cancel" else "FAILED")
        assert not list(
            session.scalars(select(RoutingAttempt).where(RoutingAttempt.request_id == row.id))
        )
        assert not list(session.scalars(select(ModelTaskPerformance)))
    adapter.release.set()
    await asyncio.gather(active, return_exceptions=True)
    assert adapter.order == ["alice"]
    if action == "close":
        assert (await router.solve(request())).reason_code == "SCHEDULER_CLOSED"


async def test_cancel_after_grant_releases_slot_and_queue_capacity(make_router):
    router, adapter, active = await hold(make_router, scheduler={"max_queued": 1})
    pending = asyncio.create_task(router.solve(request("bob")))
    await until(lambda: router.scheduler.queued == 1)
    # Cancel precisely between granting a turn and resuming its waiting coroutine.
    ticket = router.scheduler.active
    original = router.scheduler.release

    def release(value):
        original(value)
        if value is ticket:
            pending.cancel()

    router.scheduler.release = release
    adapter.release.set()
    await active
    try:
        result = await pending
        assert result.status in ("ACCEPTED", "CANCELLED", "FAILED")
    except asyncio.CancelledError:
        pass
    assert (await router.solve(request("carol"))).status == "ACCEPTED"
    assert router.scheduler.active is None


async def test_active_cancellation_waits_for_adapter_cleanup_before_next_dispatch(make_router):
    cleaning, cleaned = asyncio.Event(), asyncio.Event()

    class Cleanup(HeldAdapter):
        async def complete(self, value):
            try:
                return await super().complete(value)
            except asyncio.CancelledError:
                cleaning.set()
                await cleaned.wait()
                raise

    adapter = Cleanup()
    router = make_router([(provider(), adapter)])
    active = asyncio.create_task(router.solve(request()))
    await adapter.started.wait()
    pending = asyncio.create_task(router.solve(request("bob")))
    await until(lambda: router.scheduler.queued == 1)
    active.cancel()
    await cleaning.wait()
    assert adapter.order == ["alice"] and router.scheduler.queued == 1
    cleaned.set()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert (await pending).status == "ACCEPTED"
    assert adapter.order == ["alice", "bob"]


async def test_unexpected_execution_failure_releases_turn(make_router, monkeypatch):
    router, adapter, active = await hold(make_router)
    pending = asyncio.create_task(router.solve(request("bob")))
    await until(lambda: router.scheduler.queued == 1)
    original = router._solve

    async def fail(request, request_id, profile, profile_dict=None, **kwargs):
        if request.client_id == "bob":
            raise RuntimeError("private failure")
        return await original(request, request_id, profile, profile_dict, **kwargs)

    monkeypatch.setattr(router, "_solve", fail)
    adapter.release.set()
    await active
    with pytest.raises(RuntimeError):
        await pending
    assert (await router.solve(request("carol"))).status == "ACCEPTED"
    with router.sessions() as session:
        assert (
            session.scalar(select(TaskRequest).where(TaskRequest.client_id == "bob")).status
            == "FAILED"
        )


async def test_queued_request_is_snapshot_and_byte_limit_counts_utf8(make_router):
    router, adapter, active = await hold(make_router)
    value = request("bob")
    pending = asyncio.create_task(router.solve(value))
    await until(lambda: router.scheduler.queued == 1)
    value.client_id = "alice"
    value.task = "changed"
    adapter.release.set()
    await asyncio.gather(active, pending)
    assert adapter.order == ["alice", "bob"]
    value = SolveRequest(client_id="alice", task="界" * 100)
    router = make_router(scheduler={"max_request_bytes": len(value.model_dump_json())})
    result = await router.solve(value)
    assert result.reason_code == "REQUEST_TOO_LARGE"
    assert router.registry.adapters["a"].calls == 0


def test_priority_authorization_and_scheduler_status_are_client_isolated(make_router):
    router = make_router(scheduler={"client_priority_ceiling": {"alice": "P1"}})
    with TestClient(create_app(router, {"alice": "a", "bob": "b"}, "admin")) as client:
        assert client.get("/v1/system/scheduler", headers={"X-API-Key": "a"}).status_code == 403
        status = client.get("/v1/system/scheduler", headers={"X-API-Key": "admin"}).json()
        assert status["queued"] == status["active"] == 0
        assert "alice" not in json.dumps(status)
        for identity, priority, allowed in [
            ("alice", "P1", True),
            ("alice", "P0", False),
            ("bob", "P1", False),
            ("bob", "P4", True),
        ]:
            result = client.post(
                "/v1/solve",
                headers={"X-API-Key": identity[0]},
                json=request(identity, priority=priority).model_dump(mode="json"),
            ).json()
            assert (result["reason_code"] != "PRIORITY_NOT_ALLOWED") == allowed
            other = "b" if identity == "alice" else "a"
            assert (
                client.get(
                    f"/v1/requests/{result['request_id']}", headers={"X-API-Key": other}
                ).status_code
                == 404
            )
        assert (
            client.post(
                "/v1/solve",
                headers={"X-API-Key": "b"},
                json=request("alice", priority="P1").model_dump(mode="json"),
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/solve",
                headers={"X-API-Key": "a"},
                json={"client_id": "alice", "task": "x", "priority": "P9"},
            ).status_code
            == 422
        )


async def test_http_disconnect_removes_queued_request(make_router):
    router, adapter, active = await hold(make_router)
    app = create_app(router, {"bob": "b"}, "admin")
    incoming = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": request("bob").model_dump_json().encode(),
            "more_body": False,
        }
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/solve",
        "raw_path": b"/v1/solve",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"x-api-key", b"b")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }

    async def send(message):
        pass

    async with app.router.lifespan_context(app):
        connection = asyncio.create_task(app(scope, incoming.get, send))
        await until(lambda: router.scheduler.queued == 1)
        await incoming.put({"type": "http.disconnect"})
        with pytest.raises(asyncio.CancelledError):
            await connection
        assert router.scheduler.queued == 0
        adapter.release.set()
        await active
    assert adapter.order == ["alice"]


@pytest.mark.parametrize("cross_check", [False, True])
async def test_retry_or_verification_finishes_before_next_client_turn(make_router, cross_check):
    order = []
    first = HeldAdapter()
    first.text = "4" if cross_check else "wrong answer"

    class Second(MockAdapter):
        async def complete(self, value):
            order.append(value.client_id)
            return await super().complete(value)

    router = make_router(
        [
            (provider(), first),
            (
                provider("b", models=[{"model_id": "other", "context_window": 32768}]),
                Second("b", text="4"),
            ),
        ]
    )
    active = asyncio.create_task(router.solve(request(cross_check_required=cross_check)))
    await first.started.wait()
    waiting = asyncio.create_task(router.solve(request("bob")))
    await until(lambda: router.scheduler.queued == 1)
    first.release.set()
    result, next_result = await asyncio.gather(active, waiting)
    assert result.status == next_result.status == "ACCEPTED"
    assert order[0] == "alice"
    assert len(result.attempts) == 2
    assert result.attempts[1].role == ("CROSS_CHECK" if cross_check else "PRIMARY")


async def test_shutdown_cancellation_cannot_interrupt_provider_cleanup(make_router):
    cleaning, cleaned = asyncio.Event(), asyncio.Event()

    class Cleanup(HeldAdapter):
        async def complete(self, value):
            try:
                return await super().complete(value)
            except asyncio.CancelledError:
                cleaning.set()
                await cleaned.wait()
                raise

    adapter = Cleanup()
    router = make_router([(provider(), adapter)])
    active = asyncio.create_task(router.solve(request()))
    await adapter.started.wait()
    closing = asyncio.create_task(router.scheduler.close())
    await cleaning.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not active.done() and not closing.done()
    cleaned.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.CancelledError):
        await active
    assert router.scheduler.active is None


@pytest.mark.parametrize(
    "settings",
    [
        {"max_queued": 0},
        {"max_queued_per_client": 0},
        {"max_queued_bytes": 0},
        {"max_request_bytes": 0},
        {"queue_timeout_seconds": float("nan")},
        {"queue_timeout_seconds": float("inf")},
        {"client_priority_ceiling": {"alice": "P9"}},
    ],
)
def test_invalid_scheduler_configuration_fails_closed(settings):
    with pytest.raises(ValidationError):
        SchedulerSettings(**settings)
