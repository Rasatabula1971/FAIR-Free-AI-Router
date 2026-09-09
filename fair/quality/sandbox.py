"""Opt-in native execution through a local Docker daemon, never the router process."""

import asyncio
import json
import math
import os
import re
from time import time
from uuid import uuid4

from fair.quality.code_validator import CodeRejected, bounded, parse_function, run_case, same_value
from fair.quality.json_data import strict_json


class SandboxUnavailable(RuntimeError):
    """An executor failure, not evidence of poor model quality."""


class DockerSandbox:
    def __init__(self, image_id, owner=None, clock=None):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("Sandbox requires a locally built immutable Docker image ID")
        self.image_id = image_id
        if owner is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", owner):
            raise ValueError("Sandbox owner must be a bounded deployment identifier")
        self.owner = owner
        self.clock = clock or time
        self.pending_cleanup = set()
        # Never inherit a remote Docker context or DOCKER_HOST for private host test inputs.
        self.host = (
            "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
        )
        self.lock = asyncio.Lock()
        self.healthy = True

    async def _command(self, args, payload=None, timeout=10):
        process = await asyncio.create_subprocess_exec(
            "docker",
            "--host",
            self.host,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read(stream):
            data = bytearray()
            while chunk := await stream.read(4096):
                data.extend(chunk)
                if len(data) > 32768:
                    raise SandboxUnavailable("SANDBOX_OUTPUT_LIMIT")
            return bytes(data)

        async def write():
            try:
                if payload:
                    process.stdin.write(payload)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [
            asyncio.create_task(read(process.stdout)),
            asyncio.create_task(read(process.stderr)),
            asyncio.create_task(write()),
            asyncio.create_task(process.wait()),
        ]
        try:
            output, _, _, code = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=timeout,
            )
            return code, output
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def create_args(self, name):
        labels = []
        if self.owner is not None:
            labels = [
                "--label",
                "io.fair.sandbox=1",
                "--label",
                f"io.fair.owner={self.owner}",
                "--label",
                f"io.fair.lease-until={int(self._now()) + 120}",
            ]
        return [
            "create",
            "--name",
            name,
            "--pull",
            "never",
            "--interactive",
            "--network",
            "none",
            "--ipc",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "32",
            "--ulimit",
            "cpu=2:2",
            "--ulimit",
            "fsize=0:0",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            "nofile=64:64",
            "--log-driver",
            "none",
            "--entrypoint",
            "python",
            *labels,
            self.image_id,
            "-I",
            "-B",
            "/sandbox/runner.py",
        ]

    async def _remove(self, name):
        try:
            code, _ = await self._command(["rm", "--force", name])
            if code:
                # A successful list distinguishes an already absent target from daemon failure.
                code, output = await self._command(
                    [
                        "ps",
                        "--all",
                        "--no-trunc",
                        "--filter",
                        f"{'id' if re.fullmatch(r'[a-f0-9]{64}', name) else 'name'}={name}",
                        "--format",
                        "{{.ID}}",
                    ]
                )
                if code or output.strip():
                    raise SandboxUnavailable("SANDBOX_CLEANUP_FAILED")
            self.pending_cleanup.discard(name)
        except Exception as error:
            self.healthy = False
            self.pending_cleanup.add(name)
            raise SandboxUnavailable("SANDBOX_CLEANUP_FAILED") from error

    async def _recover(self):
        if self.owner is None:
            raise SandboxUnavailable("SANDBOX_RECOVERY_OWNER_REQUIRED")
        code, output = await self._command(
            [
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                "label=io.fair.sandbox=1",
                "--filter",
                f"label=io.fair.owner={self.owner}",
                "--format",
                "{{.ID}}",
            ]
        )
        if code:
            raise SandboxUnavailable("SANDBOX_RECOVERY_FAILED")
        ids = output.decode().splitlines()
        if (
            len(ids) > 64
            or len(set(ids)) != len(ids)
            or any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in ids)
        ):
            raise SandboxUnavailable("SANDBOX_RECOVERY_PROTOCOL_FAILED")
        records = []
        if ids:
            template = '{"id":"{{.Id}}","name":{{json .Name}},"labels":{{json .Config.Labels}}}'
            code, output = await self._command(
                ["inspect", "--type", "container", "--format", template, *ids]
            )
            if code:
                raise SandboxUnavailable("SANDBOX_RECOVERY_FAILED")
            records = [strict_json(line) for line in output.decode().splitlines()]
            if len(records) != len(ids) or {row["id"] for row in records} != set(ids):
                raise SandboxUnavailable("SANDBOX_RECOVERY_PROTOCOL_FAILED")
        # Validate the complete inventory before deleting anything. Never prune a daemon.
        for row in records:
            labels = row["labels"]
            if (
                not re.fullmatch(r"/fair-sandbox-[a-f0-9]{32}", row["name"])
                or labels.get("io.fair.sandbox") != "1"
                or labels.get("io.fair.owner") != self.owner
                or not re.fullmatch(r"[0-9]{1,12}", labels.get("io.fair.lease-until", ""))
            ):
                raise SandboxUnavailable("SANDBOX_RECOVERY_PROTOCOL_FAILED")
        removed, active = 0, set()
        now = self._now()
        for row in records:
            if int(row["labels"]["io.fair.lease-until"]) <= now:
                await self._remove(row["id"])
                removed += 1
            else:
                active.update((row["id"], row["name"].lstrip("/")))
        self.pending_cleanup.intersection_update(active)
        self.healthy = not self.pending_cleanup
        return {"removed": removed, "preserved": len(records) - removed, "healthy": self.healthy}

    def _now(self):
        value = self.clock()
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value < 1e12:
            raise SandboxUnavailable("SANDBOX_CLOCK_FAILED")
        return value

    async def _bounded_recovery(self):
        try:
            return await asyncio.wait_for(self._recover(), timeout=30)
        except asyncio.CancelledError:
            self.healthy = False
            raise
        except Exception as error:
            self.healthy = False
            raise SandboxUnavailable("SANDBOX_RECOVERY_FAILED") from error

    async def recover(self):
        async with self.lock:
            return await self._bounded_recovery()

    async def validate(self, source, contract):
        # A bounded host interpreter is also the admission check for native execution.
        # It never execs code and does not compare or send the expected answers.
        try:
            function = parse_function(source, contract.function_name)
            values = [run_case(function, case.arguments) for case in contract.cases]
            if len(json.dumps(values)) > 24000:
                raise CodeRejected("CODE_OUTPUT_LIMIT")
            payload = json.dumps(
                {
                    "source": source,
                    "function_name": contract.function_name,
                    "arguments": [case.arguments for case in contract.cases],
                }
            ).encode()
            if len(payload) > 65536:
                raise CodeRejected("CODE_INPUT_LIMIT")
        except CodeRejected as error:
            return False, str(error)
        async with self.lock:
            if self.owner is not None:
                await self._bounded_recovery()
            if not self.healthy:
                raise SandboxUnavailable("SANDBOX_CLEANUP_FAILED")
            name = "fair-sandbox-" + uuid4().hex
            created = False
            try:
                code, _ = await self._command(self.create_args(name))
                if code:
                    raise SandboxUnavailable("SANDBOX_CREATE_FAILED")
                created = True
                code, output = await self._command(
                    ["start", "--attach", "--interactive", name], payload
                )
                if code:
                    raise SandboxUnavailable("SANDBOX_EXECUTION_FAILED")
                result = strict_json(output.decode())
                if not isinstance(result, dict) or set(result) != {"values"}:
                    raise SandboxUnavailable("SANDBOX_PROTOCOL_FAILED")
                values = result["values"]
                if not isinstance(values, list) or len(values) != len(contract.cases):
                    raise SandboxUnavailable("SANDBOX_PROTOCOL_FAILED")
                for actual in values:
                    bounded(actual)
                for actual, case in zip(values, contract.cases, strict=True):
                    if not same_value(actual, case.expected):
                        return False, "CODE_TEST_FAILURE"
                return True, None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise SandboxUnavailable("SANDBOX_SERVICE_FAILED") from error
            finally:
                # A known name lets us clean up even if create's reply is interrupted.
                # Removal of an unconfirmed container may fail; poison the executor in that
                # case rather than permitting further native work with uncertain cleanup.
                cleanup = asyncio.create_task(self._remove(name))
                interrupted = False
                try:
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            interrupted = True
                    await cleanup
                except SandboxUnavailable:
                    if created:
                        raise
                finally:
                    if interrupted:
                        raise asyncio.CancelledError
