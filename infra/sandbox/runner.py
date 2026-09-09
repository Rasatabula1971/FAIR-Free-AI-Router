"""Trusted image entrypoint. Native execution is permitted only inside the sandbox image."""

import ast
import json
import os
import resource
import sys

# -I excludes application paths; load only the trusted validator copied into this image.
sys.path.insert(0, "/sandbox")
from code_validator import SAFE_BUILTINS, bounded, parse_function, run_case  # noqa: E402


def main():
    if os.geteuid() != 65534:
        raise RuntimeError("Sandbox user required")
    with open("/proc/self/status") as status_file:
        status = dict(line.split(":", 1) for line in status_file if ":" in line)
    if status["NoNewPrivs"].strip() != "1" or status["Seccomp"].strip() != "2":
        raise RuntimeError("Sandbox restrictions required")
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    payload = sys.stdin.buffer.read(65537)
    if len(payload) > 65536:
        raise ValueError("Input budget exceeded")
    data = json.loads(payload)
    if set(data) != {"source", "function_name", "arguments"}:
        raise ValueError("Invalid protocol")
    arguments = data["arguments"]
    if not isinstance(arguments, list) or not 1 <= len(arguments) <= 32:
        raise ValueError("Invalid case count")
    function = parse_function(data["source"], data["function_name"])
    compiled = compile(ast.Module(body=[function], type_ignores=[]), "<candidate>", "exec")
    values = []
    for args in arguments:
        if not isinstance(args, list) or len(args) > 8:
            raise ValueError("Invalid arguments")
        for value in args:
            bounded(value)
        # Repeat the bounded admission check inside the image before native execution.
        run_case(function, args)
        scope = {"__builtins__": dict(SAFE_BUILTINS)}
        exec(compiled, scope)  # Native code executes only in this isolated container.
        values.append(bounded(scope[data["function_name"]](*args)))
    result = json.dumps({"values": values}, allow_nan=False)
    if len(result) > 25000:
        raise ValueError("Output budget exceeded")
    print(result)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never emit generated code, arguments, traceback or host test values on failure.
        sys.exit(1)
