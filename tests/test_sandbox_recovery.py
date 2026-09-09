import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_code_validation import GOOD
from test_native_validation import IMAGE, FakeDocker, contract

from apps.api.main import create_app
from fair.quality.sandbox import DockerSandbox, SandboxUnavailable
from fair.schemas.db import AuditEvent

NOW = 1800000000


def record(number=1, lease=NOW - 1, owner="test-owner"):
    return {
        "id": f"{number:064x}",
        "name": "/fair-sandbox-" + f"{number:032x}",
        "labels": {
            "io.fair.sandbox": "1",
            "io.fair.owner": owner,
            "io.fair.lease-until": str(lease),
        },
    }


class RecoveryDocker(DockerSandbox):
    def __init__(self, records=(), fail=None):
        super().__init__(IMAGE, owner="test-owner", clock=lambda: NOW)
        self.records = {row["id"]: row for row in records}
        self.commands, self.fail = [], fail

    async def _command(self, args, payload=None, timeout=10):
        self.commands.append(args)
        if args[0] == self.fail:
            raise TimeoutError("private-docker-details")
        if args[0] == "ps":
            return 0, "\n".join(self.records).encode()
        if args[0] == "inspect":
            return 0, "\n".join(json.dumps(row) for row in self.records.values()).encode()
        if args[0] == "rm":
            self.records.pop(args[-1], None)
        return 0, b'{"values":[4]}' if args[0] == "start" else b""


async def test_recovery_removes_only_expired_owned_containers_and_is_idempotent():
    expired, active = record(), record(2, lease=NOW + 60)
    executor = RecoveryDocker([expired, active])
    result = await executor.recover()
    assert result == {"removed": 1, "preserved": 1, "healthy": True}
    assert (
        executor.commands[0][0] == "ps" and "label=io.fair.owner=test-owner" in executor.commands[0]
    )
    assert [args[-1] for args in executor.commands if args[0] == "rm"] == [expired["id"]]
    assert (await executor.recover())["removed"] == 0


@pytest.mark.parametrize("mode", ["foreign", "name", "lease", "label", "id", "over_budget"])
async def test_invalid_inventory_never_deletes_anything(mode):
    rows = [record(), record(2)]
    if mode == "foreign":
        rows[-1]["labels"]["io.fair.owner"] = "someone-else"
    if mode == "name":
        rows[-1]["name"] = "/unrelated-service"
    if mode == "lease":
        rows[-1]["labels"]["io.fair.lease-until"] = "invalid"
    if mode == "label":
        rows[-1]["labels"]["io.fair.sandbox"] = "0"
    if mode == "id":
        rows[-1]["id"] = "--all"
    if mode == "over_budget":
        rows = [record(i + 1) for i in range(65)]
    executor = RecoveryDocker(rows)
    with pytest.raises(SandboxUnavailable):
        await executor.recover()
    assert not executor.healthy and not any(args[0] == "rm" for args in executor.commands)


@pytest.mark.parametrize("failure", ["ps", "inspect", "rm"])
async def test_recovery_failure_is_private_and_blocks_execution(failure):
    executor = RecoveryDocker([record()], fail=failure)
    with pytest.raises(SandboxUnavailable, match="SANDBOX_RECOVERY_FAILED"):
        await executor.validate(GOOD, contract())
    assert not any(args[0] == "create" for args in executor.commands)
    assert not executor.healthy


async def test_failed_cleanup_can_recover_after_lease_expires():
    leftover = record(lease=NOW + 60)
    executor = RecoveryDocker([leftover], fail="rm")
    with pytest.raises(SandboxUnavailable):
        await executor._remove(leftover["name"].lstrip("/"))
    executor.fail = None
    assert not (await executor.recover())["healthy"]
    executor.clock = lambda: NOW + 61
    assert (await executor.recover()) == {"removed": 1, "preserved": 0, "healthy": True}
    assert await executor.validate(GOOD, contract()) == (True, None)


async def test_already_absent_cleanup_is_success_only_after_successful_inventory():
    executor = FakeDocker()

    async def command(args, payload=None, timeout=10):
        return (1, b"missing") if args[0] == "rm" else (0, b"")

    executor._command = command
    await executor._remove("fair-sandbox-" + "a" * 32)
    assert executor.healthy


async def test_repeated_cancellation_cannot_cancel_cleanup():
    executor = FakeDocker()
    start, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    command = executor._command

    async def delayed(args, payload=None, timeout=10):
        if args[0] == "start":
            start.set()
            await asyncio.Event().wait()
        if args[0] == "rm":
            cleanup.set()
            await release.wait()
        return await command(args, payload, timeout)

    executor._command = delayed
    task = asyncio.create_task(executor.validate(GOOD, contract()))
    await asyncio.wait_for(start.wait(), 2)
    task.cancel()
    await asyncio.wait_for(cleanup.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert executor.commands[-1][0][0] == "rm" and executor.healthy


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, True])
async def test_invalid_clock_cannot_expire_live_leases(value):
    executor = RecoveryDocker([record(lease=NOW + 60)])
    executor.clock = lambda: value
    with pytest.raises(SandboxUnavailable):
        await executor.recover()
    assert not any(args[0] == "rm" for args in executor.commands)


def test_admin_recovery_is_authenticated_and_audited(make_router):
    router = make_router()
    router.sandbox = executor = RecoveryDocker()
    with TestClient(create_app(router, {"alice": "key"}, "admin")) as client:
        executor.records = {record()["id"]: record()}
        path = "/v1/system/sandbox/recover"
        assert client.post(path, headers={"X-API-Key": "key"}).status_code == 403
        response = client.post(path, headers={"X-API-Key": "admin"})
        assert response.status_code == 200 and response.json()["removed"] == 1
        executor.fail = "ps"
        assert client.post(path, headers={"X-API-Key": "admin"}).status_code == 503
    with router.sessions() as session:
        events = list(
            session.scalars(select(AuditEvent).where(AuditEvent.event_type == "SANDBOX_RECOVERY"))
        )
    assert len(events) == 2 and "private-docker-details" not in json.dumps(
        [row.payload_json for row in events]
    )


def test_disabled_recovery_requires_owner(make_router):
    with TestClient(create_app(make_router(), {"alice": "key"}, "admin")) as client:
        assert (
            client.post("/v1/system/sandbox/recover", headers={"X-API-Key": "admin"}).status_code
            == 409
        )
    with pytest.raises(ValueError):
        DockerSandbox(IMAGE, owner="--all bad")


def test_failed_startup_reconciliation_does_not_serve(make_router):
    router = make_router()
    router.sandbox = RecoveryDocker(fail="ps")
    with (
        pytest.raises(SandboxUnavailable),
        TestClient(create_app(router, {"alice": "key"}, "admin")),
    ):
        pass
