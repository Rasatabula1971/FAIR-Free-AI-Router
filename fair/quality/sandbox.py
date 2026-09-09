"""Opt-in native execution through a local Docker daemon, never the router process."""

import asyncio
import json
import os
import re
from uuid import uuid4

from fair.quality.code_validator import CodeRejected, bounded, parse_function, run_case
from fair.quality.json_data import strict_json


class SandboxUnavailable(RuntimeError):
    """An executor failure, not evidence of poor model quality."""


class DockerSandbox:
    def __init__(self, image_id):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("Sandbox requires a locally built immutable Docker image ID")
        self.image_id = image_id
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
            self.image_id,
            "-I",
            "-B",
            "/sandbox/runner.py",
        ]

    async def _remove(self, name):
        try:
            code, _ = await self._command(["rm", "--force", name])
            if code:
                raise SandboxUnavailable("SANDBOX_CLEANUP_FAILED")
        except Exception as error:
            self.healthy = False
            raise SandboxUnavailable("SANDBOX_CLEANUP_FAILED") from error

    async def validate(self, source, contract):
        # A bounded host interpreter is also the admission check for native execution.
        # It never execs code and does not compare or send the expected answers.
        try:
            function = parse_function(source, contract.function_name)
            for case in contract.cases:
                run_case(function, case.arguments)
        except CodeRejected as error:
            return False, str(error)
        async with self.lock:
            if not self.healthy:
                raise SandboxUnavailable("SANDBOX_CLEANUP_FAILED")
            name = "fair-sandbox-" + uuid4().hex
            created = False
            try:
                code, _ = await self._command(self.create_args(name))
                if code:
                    raise SandboxUnavailable("SANDBOX_CREATE_FAILED")
                created = True
                payload = json.dumps(
                    {
                        "source": source,
                        "function_name": contract.function_name,
                        "arguments": [case.arguments for case in contract.cases],
                    }
                ).encode()
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
                    if type(actual) is not type(case.expected) or actual != case.expected:
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
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
                except SandboxUnavailable:
                    if created:
                        raise
