import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from fair.providers.mock import MockAdapter
from fair.quality.code_validator import validate_function
from fair.quality.contracts import FunctionValidation
from fair.schemas.api import SolveRequest
from fair.schemas.db import ModelTaskPerformance


def contract(cases=None):
    return FunctionValidation(
        kind="python_function",
        function_name="solve",
        cases=cases
        or [
            {"arguments": [-4], "expected": 4},
            {"arguments": [3], "expected": 3},
            {"arguments": [0], "expected": 0},
        ],
    )


def coding(name):
    return provider(
        name, models=[{"model_id": "model", "context_window": 32768, "capabilities": ["coding"]}]
    )


def request(**changes):
    return SolveRequest(
        **(
            {"client_id": "alice", "task": "Implement absolute value", "validation": contract()}
            | changes
        )
    )


GOOD = "def solve(x):\n    if x < 0:\n        return -x\n    return x\n"


async def test_function_acceptance_and_failed_test_fallback(make_router):
    bad, good = MockAdapter("a", text="def solve(x):\n    return x\n"), MockAdapter("b", text=GOOD)
    router = make_router([(coding("a"), bad), (coding("b"), good)])
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.provider_id == "b"
    assert result.verification_state == "BOUNDED_CODE_TESTS"
    assert result.output == GOOD
    assert result.attempts[0].quality.reject_reasons == ["CODE_TEST_FAILURE"]
    assert result.quality.validator_results["code_test_count"] == "3"
    with router.sessions() as session:
        row = session.get(ModelTaskPerformance, ("a", "model", "python_function"))
        assert row.quality_failures == 1 and row.quality_sum == 0


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef solve(x):\n    return x",
        "def solve(x):\n    return __import__('os').system('echo unsafe')",
        "def solve(x):\n    while True:\n        x = x + 1\n    return x",
        "def solve(x):\n    for y in range(3):\n        x += y\n    return x",
        "def solve(x):\n    return x.__class__",
        "def solve(x):\n    return solve(x)",
        "def solve(x):\n    return [x for x in [1]]",
        "def solve(x):\n    return lambda: x",
        "def solve(x):\n    return 2 ** 1000000000",
        "def solve(x):\n    return 'x' * 1000000000",
        "def solve(x):\n    if False:\n        import os\n    return x",
        "@decorator\ndef solve(x):\n    return x",
        "def solve(x=1):\n    return x",
        "def solve(x: int):\n    return x",
        "def solve(*args):\n    return 1",
        "def solve(x):\n    def hidden():\n        return 1\n    return x",
        "def solve(x):\n    global data\n    return x",
        "def solve(x):\n    yield x",
        "def solve(x):\n    return x / 2",
        "def solve(x):\n    return [x]",
        "def solve(x):\n    a, b = x\n    return a",
        "def solve(x):\n    return x\nopen('marker','w')",
    ],
)
def test_unsupported_code_rejected_even_in_unused_branches(source):
    matched, reason = validate_function(source, contract())
    assert not matched and reason.startswith("CODE_")


@pytest.mark.parametrize(
    "source,reason",
    [
        ("def solve(: broken", "CODE_SYNTAX_FAILURE"),
        ("def wrong(x):\n    return x", "CODE_SIGNATURE_FAILURE"),
        ("def solve():\n    return 4", "CODE_ARGUMENT_MISMATCH"),
        ("def solve(x):\n    x = 1", "CODE_MISSING_RETURN"),
        ("def solve(x):\n    return unknown", "CODE_UNBOUND_NAME"),
        ("def solve(x):\n    return x // 0", "CODE_RUNTIME_FAILURE"),
        ("def solve(x):\n    return x % 0", "CODE_RUNTIME_FAILURE"),
        ("def solve(x):\n    return " + str(2**300), "CODE_VALUE_LIMIT"),
        ("#" * 8193, "CODE_SIZE_LIMIT"),
    ],
)
def test_code_failures_are_normalized(source, reason):
    assert validate_function(source, contract()) == (False, reason)


def test_resource_limits_during_interpretation():
    body = "\n".join("    x = x * x" for _ in range(10))
    result = validate_function("def solve(x):\n" + body + "\n    return x", contract())
    assert result == (False, "CODE_VALUE_LIMIT")
    expression = "not " * 22 + "x"
    assert validate_function("def solve(x):\n    return " + expression, contract()) == (
        False,
        "CODE_OPERATION_LIMIT",
    )


@pytest.mark.parametrize(
    "expression,args,expected",
    [
        ("x + y * 2", [3, 4], 11),
        ("x // y", [-7, 3], -3),
        ("x % y", [-7, 3], 2),
        ("x < y and y < 10", [3, 4], True),
        ("x < y < 10", [3, 4], True),
        ("x or y", [0, 4], 4),
        ("x and y", [0, 4], 0),
        ("x if x > y else y", [3, 4], 4),
        ("not x", [0, 4], True),
        ("x != y", [3, 4], True),
        ("x <= y", [4, 4], True),
    ],
)
def test_supported_numeric_boolean_semantics(expression, args, expected):
    checks = contract([{"arguments": args, "expected": expected}])
    assert validate_function("def solve(x, y):\n    return " + expression, checks) == (True, None)


def test_boolean_is_not_an_integer_test_result():
    checks = contract([{"arguments": [1], "expected": 1}])
    assert validate_function("def solve(x):\n    return True", checks) == (
        False,
        "CODE_TEST_FAILURE",
    )


async def test_rejected_code_cannot_write_to_host(make_router, tmp_path):
    marker = tmp_path / "never-created"
    source = f"def solve(x):\n    open({str(marker)!r}, 'w').write('unsafe')\n    return x"
    router = make_router([(coding("a"), MockAdapter("a", text=source))])
    result = await router.solve(request())
    assert result.status == "ESCALATION_REQUIRED" and result.output is None
    assert not marker.exists()


async def test_test_cases_withheld_from_model(make_router):
    class Capture(MockAdapter):
        async def complete(self, value):
            assert "cases" not in value.model_dump()
            assert "913579" not in value.model_dump_json()
            return await super().complete(value)

    checks = contract([{"arguments": [913579], "expected": 913579}])
    router = make_router([(coding("a"), Capture("a", text="def solve(x):\n    return x"))])
    assert (await router.solve(request(validation=checks))).status == "ACCEPTED"


@pytest.mark.parametrize(
    "changes",
    [
        {"quality_level": "high_impact_support"},
        {"freshness_required": True},
        {"required_capabilities": {"vision"}},
    ],
)
async def test_code_checks_do_not_cover_other_capabilities(make_router, changes):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    assert (await router.solve(request(**changes))).status == "ESCALATION_REQUIRED"


@pytest.mark.parametrize(
    "cases",
    [
        [],
        [{"arguments": ["1"], "expected": 1}],
        [{"arguments": [1], "expected": 1}, {"arguments": [1, 2], "expected": 3}],
        [{"arguments": [2**300], "expected": 1}],
    ],
)
def test_invalid_host_tests_rejected(cases):
    with pytest.raises(ValidationError):
        FunctionValidation(kind="python_function", function_name="solve", cases=cases)


def test_code_contract_http_acceptance(make_router):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    with TestClient(create_app(router, {"alice": "key"}, "admin")) as client:
        result = client.post(
            "/v1/solve", headers={"X-API-Key": "key"}, json=request().model_dump(mode="json")
        )
        assert (
            result.status_code == 200
            and result.json()["verification_state"] == "BOUNDED_CODE_TESTS"
        )


async def test_source_code_phrase_does_not_require_external_grounding(make_router):
    router = make_router([(coding("a"), MockAdapter("a", text=GOOD))])
    result = await router.solve(request(task="Write Python source code for absolute value"))
    assert result.status == "ACCEPTED"
