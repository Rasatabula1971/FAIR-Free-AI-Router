"""Real-container checks; intentionally skipped unless an explicit test image is supplied."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
from test_code_validation import GOOD, coding
from test_expanded_code import PROGRAMS
from test_native_validation import contract, request

from fair.providers.mock import MockAdapter
from fair.quality.contracts import NativeFunctionValidation
from fair.quality.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(
    not os.environ.get("FAIR_TEST_SANDBOX_IMAGE"), reason="Native Docker test image not configured"
)


def sandbox():
    return DockerSandbox(os.environ["FAIR_TEST_SANDBOX_IMAGE"])


@pytest.mark.parametrize("expected,matched", [(4, True), (5, False), (True, False)])
async def test_real_native_answer_comparison(expected, matched):
    executor = sandbox()
    result = await executor.validate(GOOD, contract(expected))
    assert result == ((True, None) if matched else (False, "CODE_TEST_FAILURE"))


async def test_real_native_router_accepts_and_records_scope(make_router):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    router.sandbox = sandbox()
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.verification_state == "NATIVE_CODE_TESTS"
    assert result.quality.validator_results["sandbox_image_id"] == router.sandbox.image_id


async def test_native_semantics_for_multiple_hidden_cases():
    checks = NativeFunctionValidation(
        kind="native_python_function",
        function_name="solve",
        cases=[
            {"arguments": [x, y], "expected": x // y + x % y}
            for x, y in [(-7, 3), (7, -3), (0, 3), (12345, 67)]
        ],
    )
    assert await sandbox().validate("def solve(x, y):\n    return x // y + x % y", checks) == (
        True,
        None,
    )


async def test_container_has_enforced_isolation(monkeypatch):
    executor = sandbox()
    monkeypatch.setenv("FAIR_HOST_SECRET_CANARY", "must-not-enter-container")
    name = "fair-sandbox-probe-" + uuid4().hex
    # This trusted probe tests the OS boundary, independently of AST admission.
    probe = """
import json, os, resource, socket
assert os.geteuid() == 65534
assert 'FAIR_HOST_SECRET_CANARY' not in os.environ
assert not os.path.exists('/var/run/docker.sock')
with open('/proc/self/status') as f:
    status = dict(line.split(':', 1) for line in f if ':' in line)
assert status['NoNewPrivs'].strip() == '1'
assert status['Seccomp'].strip() == '2'
assert int(status['CapEff'].strip(), 16) == 0
assert resource.getrlimit(resource.RLIMIT_CPU) == (2, 2)
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (0, 0)
assert os.statvfs('/').f_flag & os.ST_RDONLY
with socket.socket() as sock:
    sock.settimeout(0.5)
    try:
        sock.connect(('192.0.2.1', 443))
    except OSError:
        pass
    else:
        raise AssertionError('Network unexpectedly reachable')
print(json.dumps({'isolated': True}))
"""
    args = executor.create_args(name)
    args[-1:] = ["-c", probe]
    try:
        code, _ = await executor._command(args)
        assert code == 0
        code, details = await executor._command(["inspect", name])
        assert code == 0
        detail = json.loads(details)[0]
        host = detail["HostConfig"]
        assert not detail["Mounts"] and not host["Privileged"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
        assert host["Memory"] == host["MemorySwap"] == 128 * 1024 * 1024
        assert host["PidsLimit"] == 32 and host["NanoCpus"] == 500000000
        code, output = await executor._command(["start", "--attach", name])
        assert code == 0 and json.loads(output) == {"isolated": True}
    finally:
        await executor._remove(name)


@pytest.mark.parametrize("mode", ["timeout", "cancel", "output_limit"])
async def test_real_interrupted_execution_removes_container(mode):
    executor = sandbox()
    original = executor.create_args
    names = []

    def delayed_args(name):
        names.append(name)
        args = original(name)
        # Trusted deliberate fault: never submitted through the generated-code interface.
        probe = "print('x' * 40000)" if mode == "output_limit" else "import time; time.sleep(60)"
        args[-1:] = ["-c", probe]
        return args

    executor.create_args = delayed_args
    started = asyncio.Event()
    command = executor._command

    async def short_start(args, payload=None, timeout=10):
        if args[0] == "start":
            started.set()
            timeout = 1 if mode == "timeout" else 10
        return await command(args, payload, timeout)

    executor._command = short_start
    task = asyncio.create_task(executor.validate(GOOD, contract()))
    await asyncio.wait_for(started.wait(), 20)
    if mode == "cancel":
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        from fair.quality.sandbox import SandboxUnavailable

        with pytest.raises(SandboxUnavailable):
            await task
    code, _ = await command(["inspect", names[0]])
    assert code != 0
    assert executor.healthy


@pytest.mark.parametrize("source,arguments,expected", PROGRAMS)
async def test_real_native_expanded_language_matches_expected(source, arguments, expected):
    checks = NativeFunctionValidation(
        kind="native_python_function",
        function_name="solve",
        cases=[{"arguments": arguments, "expected": expected}],
    )
    assert await sandbox().validate(source, checks) == (True, None)


async def test_real_owner_recovery_after_executor_restart_preserves_other_work():
    from time import time

    image = os.environ["FAIR_TEST_SANDBOX_IMAGE"]
    owner = "test-" + uuid4().hex
    abandoned = DockerSandbox(image, owner=owner, clock=lambda: time() - 130)
    active = DockerSandbox(image, owner=owner)
    foreign = DockerSandbox(image, owner="foreign-" + uuid4().hex, clock=lambda: time() - 130)
    names = ["fair-sandbox-" + uuid4().hex for _ in range(3)]
    try:
        for executor, name in zip((abandoned, active, foreign), names, strict=True):
            args = executor.create_args(name)
            args[-1:] = ["-c", "import time; time.sleep(60)"]
            code, _ = await executor._command(args)
            assert code == 0
        code, _ = await abandoned._command(["start", names[0]])
        assert code == 0
        restarted = DockerSandbox(image, owner=owner)
        assert await restarted.recover() == {"removed": 1, "preserved": 1, "healthy": True}
        assert (await restarted._command(["inspect", names[0]]))[0] != 0
        for name in names[1:]:
            assert (await restarted._command(["inspect", name]))[0] == 0
        assert (await restarted.recover())["removed"] == 0
        assert await restarted.validate(GOOD, contract()) == (True, None)
    finally:
        for executor, name in zip((abandoned, active, foreign), names, strict=True):
            await executor._remove(name)
