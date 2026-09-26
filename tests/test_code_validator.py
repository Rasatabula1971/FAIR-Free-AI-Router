"""Bounded-evaluator tests.

FAIR never exec()s a model's code: python_function contracts are checked by the
small interpreter in fair.quality.code_validator. Two things have to hold. An
answer must only be accepted when it genuinely passes its test cases, and no
input may escape the subset, spend unbounded time, or reach a real builtin.
The module was at 36% coverage.
"""

import pytest

from fair.quality.code_validator import (
    MAX_LIST,
    CodeRejected,
    bounded,
    parse_function,
    run_case,
    same_value,
    validate_function,
)
from fair.quality.contracts import FunctionValidation


def _contract(name="f", cases=((([2]), 4),)):
    return FunctionValidation.model_validate(
        {
            "kind": "python_function",
            "function_name": name,
            "cases": [{"arguments": list(args), "expected": expected} for args, expected in cases],
        }
    )


def _reject(source, contract=None):
    passed, reason = validate_function(source, contract or _contract())
    assert passed is False
    return reason


# ── Accepting correct answers ───────────────────────────────────────────


class TestAcceptedFunctions:
    def test_a_correct_function_passes_its_cases(self):
        source = "def f(n):\n    return n * 2\n"
        assert validate_function(source, _contract(cases=(([2], 4), ([0], 0), ([-3], -6)))) == (
            True,
            None,
        )

    def test_conditionals_and_comparisons(self):
        source = "def f(n):\n    if n > 0 and n != 5:\n        return 1\n    return 0\n"
        assert validate_function(source, _contract(cases=(([3], 1), ([5], 0), ([-1], 0)))) == (
            True,
            None,
        )

    def test_a_bounded_for_loop_over_range(self):
        source = "def f(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n"
        assert validate_function(source, _contract(cases=(([4], 6), ([1], 0)))) == (True, None)

    def test_a_while_loop_with_break(self):
        source = (
            "def f(n):\n"
            "    total = 0\n"
            "    while True:\n"
            "        total = total + 1\n"
            "        if total >= n:\n"
            "            break\n"
            "    return total\n"
        )
        assert validate_function(source, _contract(cases=(([3], 3),))) == (True, None)

    def test_list_construction_indexing_and_builtins(self):
        source = "def f(n):\n    values = [n, n + 1, n + 2]\n    return max(values) + len(values)\n"
        assert validate_function(source, _contract(cases=(([1], 6),))) == (True, None)

    def test_a_list_return_value(self):
        source = "def f(n):\n    return sorted([n + 1, n])\n"
        contract = _contract(cases=(([1], [1, 2]),))
        assert validate_function(source, contract) == (True, None)

    def test_a_boolean_return_value(self):
        source = "def f(n):\n    return not n\n"
        assert validate_function(source, _contract(cases=(([0], True), ([1], False)))) == (
            True,
            None,
        )

    def test_integer_division_and_modulo(self):
        source = "def f(n):\n    return n // 3 + n % 3\n"
        assert validate_function(source, _contract(cases=(([7], 3),))) == (True, None)

    def test_a_conditional_expression(self):
        source = "def f(n):\n    return 1 if n else 0\n"
        assert validate_function(source, _contract(cases=(([5], 1), ([0], 0)))) == (True, None)

    def test_multiple_arguments(self):
        source = "def f(a, b):\n    return a + b\n"
        assert validate_function(source, _contract(cases=(([2, 3], 5),))) == (True, None)


# ── Rejecting wrong answers ─────────────────────────────────────────────


class TestWrongAnswers:
    def test_a_failing_case_is_a_test_failure(self):
        assert _reject("def f(n):\n    return n * 3\n") == "CODE_TEST_FAILURE"

    def test_one_failing_case_among_several_rejects(self):
        source = "def f(n):\n    return n * 2\n"
        contract = _contract(cases=(([2], 4), ([3], 99)))
        assert validate_function(source, contract) == (False, "CODE_TEST_FAILURE")

    def test_a_true_value_does_not_satisfy_an_integer_expectation(self):
        """1 == True in Python; the contract is stricter than that."""
        assert not same_value(True, 1)
        assert not same_value(1, True)

    def test_a_function_that_never_returns(self):
        assert _reject("def f(n):\n    n = n + 1\n") == "CODE_MISSING_RETURN"

    def test_a_case_with_the_wrong_argument_count(self):
        source = "def f(a, b):\n    return a + b\n"
        assert _reject(source, _contract(cases=(([1], 1),))) == "CODE_ARGUMENT_MISMATCH"


# ── Rejecting anything outside the subset ───────────────────────────────


class TestSubsetEnforcement:
    def test_an_import_is_refused(self):
        assert _reject("import os\ndef f(n):\n    return 4\n") == "CODE_SUBSET_VIOLATION"

    def test_an_attribute_access_is_refused(self):
        assert _reject("def f(n):\n    return n.real\n") == "CODE_SUBSET_VIOLATION"

    def test_a_call_to_a_name_outside_the_safe_builtins(self):
        assert _reject("def f(n):\n    return eval('4')\n") == "CODE_SUBSET_VIOLATION"

    def test_a_dunder_lookup_is_refused(self):
        assert _reject("def f(n):\n    return open.__doc__\n") == "CODE_SUBSET_VIOLATION"

    def test_a_lambda_is_refused(self):
        assert _reject("def f(n):\n    g = lambda x: x\n    return 4\n") == "CODE_SUBSET_VIOLATION"

    def test_a_comprehension_is_refused(self):
        assert _reject("def f(n):\n    return [x for x in [1]]\n") == "CODE_SUBSET_VIOLATION"

    def test_a_dict_literal_is_refused(self):
        assert _reject("def f(n):\n    return {1: 2}\n") == "CODE_SUBSET_VIOLATION"

    def test_a_string_constant_is_refused(self):
        assert _reject('def f(n):\n    return "text"\n') == "CODE_VALUE_LIMIT"

    def test_a_float_constant_is_refused(self):
        assert _reject("def f(n):\n    return 1.5\n") == "CODE_VALUE_LIMIT"

    def test_true_division_is_refused(self):
        assert _reject("def f(n):\n    return n / 2\n") == "CODE_SUBSET_VIOLATION"

    def test_exponentiation_is_refused(self):
        """A short expression must not be able to build an enormous integer."""
        assert _reject("def f(n):\n    return n ** n\n") == "CODE_SUBSET_VIOLATION"

    def test_a_try_block_is_refused(self):
        source = "def f(n):\n    try:\n        return 4\n    except Exception:\n        return 0\n"
        assert _reject(source) == "CODE_SUBSET_VIOLATION"

    def test_a_tuple_assignment_target_is_refused(self):
        assert _reject("def f(n):\n    a = b = n\n    return a\n") == "CODE_SUBSET_VIOLATION"

    def test_subscript_assignment_is_refused(self):
        source = "def f(n):\n    values = [1]\n    values[0] = n\n    return values[0]\n"
        assert _reject(source) == "CODE_SUBSET_VIOLATION"

    def test_break_outside_a_loop_is_refused(self):
        """ast.parse accepts this; the subset check is what rejects it."""
        assert _reject("def f(n):\n    break\n    return 4\n") == "CODE_SUBSET_VIOLATION"

    def test_continue_outside_a_loop_is_refused(self):
        assert _reject("def f(n):\n    continue\n    return 4\n") == "CODE_SUBSET_VIOLATION"

    def test_break_inside_a_loop_body_is_allowed(self):
        source = (
            "def f(n):\n"
            "    total = 0\n"
            "    for i in range(10):\n"
            "        if i > n:\n"
            "            break\n"
            "        total = total + 1\n"
            "    return total\n"
        )
        assert validate_function(source, _contract(cases=(([2], 3),))) == (True, None)

    def test_a_nested_function_is_refused(self):
        source = "def f(n):\n    def g():\n        return 1\n    return 4\n"
        assert _reject(source) == "CODE_SIGNATURE_FAILURE"

    def test_range_outside_a_for_loop_is_refused(self):
        assert _reject("def f(n):\n    return len(range(n))\n") == "CODE_SUBSET_VIOLATION"


class TestSignatureEnforcement:
    def test_the_function_must_have_the_contracted_name(self):
        assert _reject("def other(n):\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_prose_around_the_function_is_refused(self):
        assert _reject("Here you go:\ndef f(n):\n    return 4\n") == "CODE_SYNTAX_FAILURE"

    def test_a_second_statement_beside_the_function_is_refused(self):
        assert _reject("def f(n):\n    return 4\nx = 1\n") == "CODE_FUNCTION_REQUIRED"

    def test_an_empty_answer_is_refused(self):
        assert _reject("") == "CODE_FUNCTION_REQUIRED"

    def test_a_default_argument_is_refused(self):
        assert _reject("def f(n=1):\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_varargs_are_refused(self):
        assert _reject("def f(*args):\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_keyword_arguments_are_refused(self):
        assert _reject("def f(**kwargs):\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_an_annotation_is_refused(self):
        assert _reject("def f(n: int):\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_a_return_annotation_is_refused(self):
        assert _reject("def f(n) -> int:\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_too_many_parameters_are_refused(self):
        args = ", ".join(f"a{index}" for index in range(9))
        assert _reject(f"def f({args}):\n    return 4\n") == "CODE_SIGNATURE_FAILURE"

    def test_shadowing_a_safe_builtin_is_refused(self):
        assert _reject("def f(len):\n    return 4\n") == "CODE_RESERVED_NAME"

    def test_assigning_over_a_safe_builtin_is_refused(self):
        assert _reject("def f(n):\n    sum = n\n    return sum\n") == "CODE_RESERVED_NAME"

    def test_an_oversized_answer_is_refused(self):
        source = "def f(n):\n" + "    # padding\n" * 900 + "    return 4\n"
        assert len(source) > 8192
        assert _reject(source) == "CODE_SIZE_LIMIT"

    def test_an_answer_with_too_many_nodes_is_refused(self):
        body = "".join(f"    x{index} = {index}\n" for index in range(130))
        assert _reject("def f(n):\n" + body + "    return 4\n") == "CODE_SUBSET_VIOLATION"


# ── Resource bounds at run time ─────────────────────────────────────────


class TestRuntimeBounds:
    def test_an_unbounded_while_loop_is_stopped(self):
        source = "def f(n):\n    while True:\n        n = n + 1\n    return n\n"
        assert _reject(source) in {"CODE_OPERATION_LIMIT", "CODE_ITERATION_LIMIT"}

    def test_an_oversized_range_is_stopped(self):
        source = (
            "def f(n):\n"
            "    total = 0\n"
            "    for i in range(100000):\n"
            "        total = total + 1\n"
            "    return total\n"
        )
        assert _reject(source) in {"CODE_OPERATION_LIMIT", "CODE_ITERATION_LIMIT"}

    def test_an_integer_that_grows_past_the_value_limit(self):
        source = (
            "def f(n):\n"
            "    total = 2\n"
            "    for i in range(300):\n"
            "        total = total * total\n"
            "    return total\n"
        )
        assert _reject(source) in {"CODE_VALUE_LIMIT", "CODE_OPERATION_LIMIT"}

    def test_division_by_zero_is_a_runtime_failure(self):
        assert _reject("def f(n):\n    return n // 0\n") == "CODE_RUNTIME_FAILURE"

    def test_an_out_of_range_index_is_a_runtime_failure(self):
        source = "def f(n):\n    values = [1]\n    return values[n]\n"
        assert _reject(source, _contract(cases=(([5], 1),))) == "CODE_RUNTIME_FAILURE"

    def test_a_name_used_before_assignment_is_refused(self):
        source = "def f(n):\n    if n > 100:\n        total = 1\n    return total\n"
        assert _reject(source) == "CODE_UNBOUND_NAME"

    def test_iterating_something_that_is_not_a_range_is_refused(self):
        source = "def f(n):\n    total = 0\n    for i in n:\n        total = total + 1\n    return total\n"
        assert _reject(source) == "CODE_TYPE_FAILURE"


# ── The helpers themselves ──────────────────────────────────────────────


class TestBounded:
    @pytest.mark.parametrize("value", [0, 1, -1, True, False, 2**255, [1, 2, 3], []])
    def test_accepts_bounded_values(self, value):
        assert bounded(value) == value

    @pytest.mark.parametrize(
        "value", [1.5, "text", None, 2**256, [1.5], list(range(MAX_LIST + 1)), [[1]], {1: 2}]
    )
    def test_refuses_unbounded_values(self, value):
        with pytest.raises(CodeRejected, match="CODE_VALUE_LIMIT"):
            bounded(value)


class TestSameValue:
    @pytest.mark.parametrize(
        "actual,expected",
        [(1, 1), (True, True), ([1, 2], [1, 2]), ([], []), ([[1]], [[1]])],
    )
    def test_equal_values(self, actual, expected):
        assert same_value(actual, expected)

    @pytest.mark.parametrize(
        "actual,expected",
        [(1, 2), (1, True), ([1], [1, 2]), ([1], 1), ([1], [2])],
    )
    def test_unequal_values(self, actual, expected):
        assert not same_value(actual, expected)


class TestRunCase:
    def test_run_case_returns_the_computed_value(self):
        function = parse_function("def f(n):\n    return n + 1\n", "f")
        assert run_case(function, [41]) == 42

    def test_run_case_checks_its_arguments(self):
        function = parse_function("def f(n):\n    return n\n", "f")
        with pytest.raises(CodeRejected, match="CODE_ARGUMENT_MISMATCH"):
            run_case(function, [1, 2])

    def test_an_unbounded_argument_is_refused(self):
        function = parse_function("def f(n):\n    return n\n", "f")
        with pytest.raises(CodeRejected, match="CODE_VALUE_LIMIT"):
            run_case(function, [2**256])
