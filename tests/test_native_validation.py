import asyncio
import json

import pytest
from sqlalchemy import select
from test_code_validation import GOOD, coding

from fair.providers.mock import MockAdapter
from fair.quality.contracts import NativeFunctionValidation
from fair.quality.sandbox import DockerSandbox, SandboxUnavailable
from fair.schemas.api import SolveRequest
from fair.schemas.db import ModelTaskPerformance, RoutingAttempt, TaskRequest

IMAGE = "sha256:" + "a" * 64


def contract(expected=4):
    return NativeFunctionValidation(
        kind="native_python_function",
        function_name="solve",
        cases=[{"arguments": [-4], "expected": expected}],
    )


def request(**changes):
    return SolveRequest(
        **{
            "client_id": "alice",
            "task": "Implement absolute value",
            "validation": contract(),
            **changes,
        }
    )


class FakeDocker(DockerSandbox):
    def __init__(self, output=b'{"values":[4]}', fail=None):
        super().__init__(IMAGE)
        self.output, self.fail, self.commands = output, fail, []

    async def _command(self, args, payload=None, timeout=10):
        self.commands.append((args, payload))
        if args[0] == self.fail:
            raise TimeoutError("private-error-canary")
        return 0, self.output if args[0] == "start" else b""


async def test_native_opt_in_and_separate_verification(make_router):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    disabled = await router.solve(request())
    assert disabled.status == "ESCALATION_REQUIRED" and disabled.output is None
    assert disabled.attempts[0].disposition == "UNVERIFIED"
    router.sandbox = FakeDocker()
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.verification_state == "NATIVE_CODE_TESTS"
    assert result.quality.validator_results["sandbox_image_id"] == IMAGE
    with router.sessions() as session:
        stats = session.get(ModelTaskPerformance, ("a", "model", "native_python_function"))
        assert stats.unverified == 1 and stats.accepted == 1 and stats.quality_samples == 1


async def test_native_wrong_result_falls_back(make_router):
    router = make_router([(coding(n), MockAdapter(n, text=GOOD)) for n in ("a", "b")])
    sandbox = router.sandbox = FakeDocker()
    count = 0

    async def validate(source, checks):
        nonlocal count
        count += 1
        return (False, "CODE_TEST_FAILURE") if count == 1 else (True, None)

    sandbox.validate = validate
    result = await router.solve(request())
    assert result.provider_id == "b"
    assert [a.disposition for a in result.attempts] == ["QUALITY_FAILURE", "ACCEPTED"]


@pytest.mark.parametrize("fail", ["create", "start", "rm"])
async def test_executor_failures_do_not_penalize_model(make_router, fail):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    router.sandbox = FakeDocker(fail=fail)
    result = await router.solve(request())
    assert result.status == "FAILED" and result.output is None
    assert result.reason_code == "VALIDATION_SERVICE_FAILED"
    assert "private-error-canary" not in result.model_dump_json()
    with router.sessions() as session:
        stats = session.get(ModelTaskPerformance, ("a", "model", "native_python_function"))
        assert stats.quality_samples == stats.infra_failures == 0
    assert router.sandbox.commands[-1][0][0] == "rm"


async def test_expected_answers_never_enter_sandbox_or_command_line():
    sandbox = FakeDocker()
    assert await sandbox.validate(GOOD, contract(expected=719351)) == (False, "CODE_TEST_FAILURE")
    serialized = json.dumps(sandbox.commands, default=lambda item: item.decode())
    assert "719351" not in serialized and "expected" not in serialized
    assert all(GOOD not in json.dumps(args) for args, _ in sandbox.commands)


@pytest.mark.parametrize(
    "output",
    [
        b"{}",
        b'{"values":[4],"extra":true}',
        b'{"values":[]}',
        b'{"values":[4,4]}',
        b'{"values":["4"]}',
        b'{"values":[NaN]}',
        b'{"values":[4],"values":[4]}',
        b"not-json",
        b'{"values":null}',
    ],
)
async def test_malformed_sandbox_protocol_fails_closed(output):
    sandbox = FakeDocker(output=output)
    with pytest.raises(SandboxUnavailable):
        await sandbox.validate(GOOD, contract())
    assert sandbox.commands[-1][0][0] == "rm"


async def test_native_boolean_does_not_match_integer():
    assert await FakeDocker(output=b'{"values":[true]}').validate(GOOD, contract(1)) == (
        False,
        "CODE_TEST_FAILURE",
    )


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef solve(x):\n    return x",
        "def solve(x):\n    return x.__class__",
        "def solve(x):\n    while True:\n        x = x + 1\n    return x",
        "def solve(x):\n    return __import__('socket')",
        "def solve(x):\n    if False:\n        open('marker', 'w')\n    return x",
        "def solve(x):\n" + "    x = x * x\n" * 10 + "    return x",
    ],
)
async def test_native_admission_rejects_before_docker(source):
    sandbox = FakeDocker()
    passed, reason = await sandbox.validate(source, contract())
    assert not passed and reason.startswith("CODE_") and not sandbox.commands


async def test_cleanup_failure_disables_future_execution():
    sandbox = FakeDocker(fail="rm")
    with pytest.raises(SandboxUnavailable):
        await sandbox.validate(GOOD, contract())
    count = len(sandbox.commands)
    with pytest.raises(SandboxUnavailable):
        await sandbox.validate(GOOD, contract())
    assert len(sandbox.commands) == count


async def test_cancellation_cleans_container_and_persists_cancelled_attempt(make_router):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    sandbox = router.sandbox = FakeDocker()
    started = asyncio.Event()
    original = sandbox._command

    async def command(args, payload=None, timeout=10):
        if args[0] == "start":
            started.set()
            await asyncio.Event().wait()
        return await original(args, payload, timeout)

    sandbox._command = command
    task = asyncio.create_task(router.solve(request()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.commands[-1][0][0] == "rm"
    with router.sessions() as session:
        assert session.scalar(select(TaskRequest)).status == "CANCELLED"
        assert session.scalar(select(RoutingAttempt)).detail_json["disposition"] == "CANCELLED"


async def test_stop_during_native_validation_withholds_result(make_router):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    router.sandbox = FakeDocker()

    async def stop(source, checks):
        router.stopped = True
        return True, None

    router.sandbox.validate = stop
    result = await router.solve(request())
    assert result.reason_code == "SYSTEM_STOPPED" and result.output is None


async def test_native_cross_check_uses_separate_execution(make_router):
    specs = [coding(n) for n in ("a", "b")]
    specs[1].models[0].model_id = "independent"
    router = make_router([(s, MockAdapter(s.provider_id, text=GOOD)) for s in specs])
    router.sandbox = FakeDocker()
    result = await router.solve(request(cross_check_required=True))
    assert result.status == "ACCEPTED" and result.cross_check.state == "PASSED"
    assert result.cross_check.agreement_basis == "HOST_TEST_CASES"
    assert sum(args[0] == "start" for args, _ in router.sandbox.commands) == 2


@pytest.mark.parametrize("image", ["python:latest", "--privileged", "", "remote/image@sha256:123"])
def test_only_immutable_local_images_allowed(image):
    with pytest.raises(ValueError):
        DockerSandbox(image)
