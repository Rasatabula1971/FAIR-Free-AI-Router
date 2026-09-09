import json

import pytest
from pydantic import ValidationError
from test_code_validation import coding, request
from test_native_validation import FakeDocker

from fair.providers.mock import MockAdapter
from fair.quality.code_validator import validate_function
from fair.quality.contracts import FunctionValidation


def checks(arguments, expected, kind="python_function"):
    return FunctionValidation(
        kind=kind, function_name="solve", cases=[{"arguments": arguments, "expected": expected}]
    )


# Handwritten programs and independent expected values; no generated code executes on the host.
PROGRAMS = [
    (
        "def solve(n):\n    result = 1\n    for i in range(1, n + 1):\n        result *= i\n    return result",
        [6],
        720,
    ),
    (
        "def solve(a, b):\n    while b:\n        r = a % b\n        a = b\n        b = r\n    return a",
        [48, 18],
        6,
    ),
    (
        "def solve(xs):\n    total = 0\n    for x in xs:\n        if x < 0:\n            continue\n        total += x\n    return total",
        [[-5, 3, 0, 7]],
        10,
    ),
    (
        "def solve(xs):\n    for x in xs:\n        if x > 5:\n            return x\n    else:\n        return -1",
        [[1, 4, 7]],
        7,
    ),
    (
        "def solve(xs):\n    for x in xs:\n        if x > 5:\n            break\n    else:\n        return -1\n    return x",
        [[1, 4]],
        -1,
    ),
    ("def solve(n):\n    while n > 0:\n        n -= 1\n    else:\n        return n", [3], 0),
    (
        "def solve(n):\n    total = 0\n    for x in range(n, 0, -2):\n        total += x\n    return total",
        [5],
        9,
    ),
    ("def solve(xs):\n    return sorted(xs)", [[5, -1, 2, 0]], [-1, 0, 2, 5]),
    (
        "def solve(xs):\n    return [min(xs), max(xs), sum(xs), len(xs)]",
        [[3, -2, 5]],
        [-2, 5, 6, 3],
    ),
    ("def solve(xs):\n    return [xs[-1], xs[0], abs(xs[1])]", [[4, -5, 6]], [6, 4, 5]),
    ("def solve(xs):\n    return xs + [True, 0]", [[1, False]], [1, False, True, 0]),
    (
        "def solve(n):\n    total = 0\n    for i in range(n):\n        for j in range(i):\n            total += j\n    return total",
        [5],
        10,
    ),
    ("def solve(xs):\n    for x in xs:\n        xs = []\n    return x", [[1, 2, 3]], 3),
    (
        "def solve(n):\n    x = 0\n    while n:\n        n -= 1\n        if n == 2:\n            continue\n        x += n\n        if n == 1:\n            break\n    else:\n        x = -1\n    return x",
        [5],
        8,
    ),
    ("def solve(xs):\n    return min(5, xs[0], 9) + max(3, xs[-1])", [[2, 7]], 9),
    ("def solve(xs):\n    return xs or [0]", [[]], [0]),
    ("def solve(xs):\n    return [sum(xs), len(xs)]", [[]], [0, 0]),
]


@pytest.mark.parametrize("source,arguments,expected", PROGRAMS)
def test_expanded_language_semantics(source, arguments, expected):
    assert validate_function(source, checks(arguments, expected)) == (True, None)


@pytest.mark.parametrize(
    "body",
    [
        "return xs[0]",
        "return min(xs)",
        "return xs // 2",
        "return xs * 1000000000",
        "xs += [1]\n    return xs",
        "return -xs",
        "return range(3)",
        "return sorted(xs, reverse=True)",
        "xs.append(1)\n    return xs",
        "xs[0] = 1\n    return xs",
        "return xs[:1]",
        "return [[1]]",
        "return sum(xs, 1)",
        "break\n    return 1",
        "continue\n    return 1",
        "return [x for x in xs]",
        "if False:\n        open('marker', 'w')\n    return 1",
        "len = 2\n    return len",
        "return __import__('os')",
        "return solve(xs)",
    ],
)
def test_unsafe_unsupported_or_invalid_operations_fail_closed(body):
    result = validate_function("def solve(xs):\n    " + body, checks([[]], 0))
    assert not result[0] and result[1].startswith("CODE_")


@pytest.mark.parametrize(
    "source",
    [
        "def solve(n):\n    while True:\n        n += 1\n    return n",
        "def solve(n):\n    for x in range(n):\n        n = x\n    return n",
        "def solve(n):\n    for x in range(100):\n        for y in range(100):\n            n += 1\n    return n",
        "def solve(n):\n    xs = []\n    while n:\n        xs = xs + [n]\n        n -= 1\n    return xs",
    ],
)
async def test_limits_reject_before_native_execution(source):
    sandbox = FakeDocker()
    result = await sandbox.validate(source, checks([1000000000000000000000000000000], 0))
    assert not result[0] and result[1].startswith("CODE_") and not sandbox.commands


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ([[[1]]], 0),
        ([[1] * 65], 0),
        ([[2**257]], 0),
        ([[]], [[1]]),
        ([[]], [True, 2**257]),
        ([[1.0]], 0),
        ([[]], ["1"]),
    ],
)
def test_contract_rejects_nested_oversized_or_coerced_values(arguments, expected):
    with pytest.raises(ValidationError):
        checks(arguments, expected)


def test_aggregate_test_budget_and_reserved_function_names():
    with pytest.raises(ValidationError):
        FunctionValidation(
            kind="python_function",
            function_name="solve",
            cases=[{"arguments": [[2**255] * 64] * 8, "expected": 0}],
        )
    with pytest.raises(ValidationError):
        FunctionValidation(
            kind="python_function", function_name="sum", cases=[{"arguments": [1], "expected": 1}]
        )


@pytest.mark.parametrize("actual,expected", [([True], [1]), ([1], [True]), ([], [0])])
async def test_list_results_preserve_strict_element_types_in_both_executors(actual, expected):
    source = "def solve(xs):\n    return xs"
    assert validate_function(source, checks([actual], expected)) == (False, "CODE_TEST_FAILURE")
    native = FakeDocker(output=json.dumps({"values": [actual]}).encode())
    assert await native.validate(source, checks([actual], expected)) == (False, "CODE_TEST_FAILURE")


async def test_expanded_function_routes_and_keeps_tests_private(make_router):
    source, arguments, expected = PROGRAMS[0]

    class Capture(MockAdapter):
        async def complete(self, req):
            assert "720" not in req.task and "range" in req.task
            return await super().complete(req)

    router = make_router([(coding("a"), Capture("a", text=source))])
    result = await router.solve(request(validation=checks(arguments, expected)))
    assert result.status == "ACCEPTED" and result.quality.engine_version == "deterministic-v8"


async def test_native_serialization_budget_rejects_before_docker():
    source = "#" + "\U0001f600" * 8000 + "\ndef solve(x):\n    return x"
    sandbox = FakeDocker()
    assert await sandbox.validate(source, checks([1], 1)) == (False, "CODE_INPUT_LIMIT")
    assert not sandbox.commands
