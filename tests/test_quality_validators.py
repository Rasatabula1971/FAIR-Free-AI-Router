"""Claim grounding, arithmetic and cross-check comparison.

These are the checks that decide whether an answer is accepted. A gap here is
an answer accepted on evidence that does not support it: claims.py was at 34%,
arithmetic.py 65%, consensus.py 62%.
"""

import pytest

from fair.quality.arithmetic import calculate, numeric_answer
from fair.quality.claims import (
    canonical,
    comparable_claims,
    fact_index,
    parse_claim_response,
    validate_claims,
)
from fair.quality.consensus import compare, independent
from fair.quality.contracts import ClaimsValidation, Evidence
from fair.schemas.domain import ModelDescriptor, ProviderSpec


def _facts(*entries):
    """entries: (subject, predicate, context, value)."""
    import json

    return json.dumps(
        {
            "facts": [
                {"subject": s, "predicate": p, "context": c, "value": v} for s, p, c, v in entries
            ]
        }
    )


def _evidence(source_id, facts_json):
    return Evidence.model_validate({"source_id": source_id, "text": facts_json})


def _contract(*keys):
    return ClaimsValidation.model_validate(
        {
            "kind": "grounded_claims",
            "claims": [
                {"claim_id": claim_id, "subject": s, "predicate": p, "context": c}
                for claim_id, s, p, c in keys
            ],
        }
    )


def _answer(claim_id, value, sources):
    import json

    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": claim_id,
                    "status": "answered",
                    "value": value,
                    "sources": [
                        {"source_id": source_id, "pointer": pointer}
                        for source_id, pointer in sources
                    ],
                }
            ]
        }
    )


FACTS = _facts(("acme", "revenue", "fy2025", 100))
CONTRACT = _contract(("c1", "acme", "revenue", "fy2025"))
EVIDENCE = [_evidence("s1", FACTS)]


# ── Arithmetic ──────────────────────────────────────────────────────────


class TestCalculate:
    @pytest.mark.parametrize(
        "expression,expected",
        [
            ("15*23", 345),
            ("1+2", 3),
            ("10-4", 6),
            ("7/2", "7/2"),
            ("(1+2)*3", 9),
            ("-4", -4),
            ("+4", 4),
            ("0.5+0.25", "3/4"),
            (".5*2", 1),
            ("2.*3", 6),
        ],
    )
    def test_exact_rational_results(self, expression, expected):
        from fractions import Fraction

        assert calculate(expression) == Fraction(expected)

    def test_decimals_are_exact_rather_than_binary_floats(self):
        from fractions import Fraction

        assert calculate("0.1+0.2") == Fraction(3, 10)

    @pytest.mark.parametrize(
        "expression",
        [
            "",
            "x" * 129,
            "2**8",
            "1//2",
            "1%2",
            "__import__('os')",
            "1+",
            "abs(-1)",
            "1 if 1 else 2",
            "0x10",
            "1e5",
            "1_000",
            "1/0",
        ],
    )
    def test_unsupported_expressions_are_refused(self, expression):
        with pytest.raises(ValueError):
            calculate(expression)

    def test_deeply_nested_arithmetic_is_refused(self):
        # Parentheses are not AST nodes, so depth has to come from real operators.
        expression = "1+(" * 17 + "1" + ")" * 17
        assert len(expression) <= 128
        with pytest.raises(ValueError, match="depth budget"):
            calculate(expression)

    def test_too_many_operations_are_refused(self):
        with pytest.raises(ValueError, match="operation budget"):
            calculate("+".join(["1"] * 40))


class TestNumericAnswer:
    @pytest.mark.parametrize(
        "text,expected",
        [("345", 345), (" 345 ", 345), ("-7", -7), ("+7", 7), ("1/2", "1/2"), ("2.50", "5/2")],
    )
    def test_accepted_answer_forms(self, text, expected):
        from fractions import Fraction

        assert numeric_answer(text) == Fraction(expected)

    @pytest.mark.parametrize(
        "text", ["", "the answer is 345", "345.", "1/0", "1e5", "0x10", "9" * 257, "1 2"]
    )
    def test_refused_answer_forms(self, text):
        with pytest.raises(ValueError):
            numeric_answer(text)


# ── Fact indexing ───────────────────────────────────────────────────────


class TestFactIndex:
    def test_facts_are_indexed_by_their_key(self):
        index = fact_index(EVIDENCE)
        assert index[("acme", "revenue", "fy2025")] == [("100", ("s1", "/facts/0"))]

    def test_two_sources_carrying_the_same_fact_both_appear(self):
        index = fact_index([_evidence("s1", FACTS), _evidence("s2", FACTS)])
        assert len(index[("acme", "revenue", "fy2025")]) == 2

    def test_evidence_that_is_not_a_fact_document_is_refused(self):
        with pytest.raises(ValueError, match="bounded structured facts"):
            fact_index([_evidence("s1", '{"not": "facts"}')])

    def test_evidence_that_is_not_json_is_refused(self):
        with pytest.raises(ValueError, match="bounded structured facts"):
            fact_index([_evidence("s1", "just prose")])

    def test_a_non_scalar_fact_value_is_refused(self):
        with pytest.raises(ValueError, match="bounded structured facts"):
            fact_index([_evidence("s1", _facts(("a", "b", "c", {"nested": 1})))])

    def test_too_many_facts_across_sources_are_refused(self):
        big = _facts(*[(f"s{index}", "p", "c", index) for index in range(50)])
        with pytest.raises(ValueError, match="200 facts"):
            fact_index([_evidence(f"s{index}", big) for index in range(5)])

    def test_canonical_distinguishes_json_types(self):
        assert canonical(1) != canonical(True)
        assert canonical(1) != canonical(1.0)
        assert canonical(None) == "null"


# ── Claim validation ────────────────────────────────────────────────────


class TestValidateClaims:
    def test_a_supported_claim_passes(self):
        matched, reasons, reports = validate_claims(
            _answer("c1", 100, [("s1", "/facts/0")]), CONTRACT, EVIDENCE
        )
        assert matched is True
        assert reasons == []
        assert [report.status for report in reports] == ["SUPPORTED"]

    def test_an_unparseable_response_is_a_format_failure(self):
        matched, reasons, reports = validate_claims("not json", CONTRACT, EVIDENCE)
        assert matched is False
        assert reasons == ["CLAIM_FORMAT_FAILURE"]
        assert reports == []

    def test_a_contradicted_value_is_rejected(self):
        matched, reasons, reports = validate_claims(
            _answer("c1", 999, [("s1", "/facts/0")]), CONTRACT, EVIDENCE
        )
        assert matched is False
        assert "CONTRADICTED_CLAIM" in reasons
        assert reports[0].status == "CONTRADICTED"

    def test_a_value_of_the_wrong_json_type_is_contradicted(self):
        """100 and 100.0 are different facts."""
        matched, _, reports = validate_claims(
            _answer("c1", 100.0, [("s1", "/facts/0")]), CONTRACT, EVIDENCE
        )
        assert matched is False
        assert reports[0].status == "CONTRADICTED"

    def test_a_claim_with_no_supporting_fact_is_unsupported(self):
        contract = _contract(("c1", "acme", "headcount", "fy2025"))
        matched, reasons, reports = validate_claims(
            _answer("c1", 100, [("s1", "/facts/0")]), contract, EVIDENCE
        )
        assert matched is False
        assert "UNSUPPORTED_CLAIM" in reasons
        assert reports[0].status == "UNSUPPORTED"

    def test_a_missing_claim_is_rejected(self):
        matched, reasons, reports = validate_claims('{"claims": []}', CONTRACT, EVIDENCE)
        assert matched is False
        assert "MISSING_CLAIM" in reasons
        assert reports[0].status == "MISSING"

    def test_an_unrequested_claim_is_rejected(self):
        matched, reasons, _ = validate_claims(
            _answer("other", 100, [("s1", "/facts/0")]), CONTRACT, EVIDENCE
        )
        assert matched is False
        assert "UNREQUESTED_CLAIM" in reasons

    def test_conflicting_evidence_cannot_be_answered(self):
        evidence = [
            _evidence("s1", _facts(("acme", "revenue", "fy2025", 100))),
            _evidence("s2", _facts(("acme", "revenue", "fy2025", 200))),
        ]
        matched, reasons, reports = validate_claims(
            _answer("c1", 100, [("s1", "/facts/0")]), CONTRACT, evidence
        )
        assert matched is False
        assert "CLAIM_OVER_CONFLICTING_EVIDENCE" in reasons
        assert reports[0].status == "CONFLICTING_EVIDENCE"

    def test_citations_must_name_every_supporting_fact(self):
        evidence = [_evidence("s1", FACTS), _evidence("s2", FACTS)]
        matched, reasons, reports = validate_claims(
            _answer("c1", 100, [("s1", "/facts/0")]), CONTRACT, evidence
        )
        assert matched is False
        assert "CLAIM_PROVENANCE_FAILURE" in reasons
        assert reports[0].status == "INVALID_PROVENANCE"

    def test_a_repeated_citation_is_invalid_provenance(self):
        matched, reasons, _ = validate_claims(
            _answer("c1", 100, [("s1", "/facts/0"), ("s1", "/facts/0")]), CONTRACT, EVIDENCE
        )
        assert matched is False
        assert "CLAIM_PROVENANCE_FAILURE" in reasons

    def test_a_citation_naming_the_wrong_fact_is_invalid_provenance(self):
        facts = _facts(("acme", "revenue", "fy2025", 100), ("acme", "headcount", "fy2025", 5))
        matched, reasons, _ = validate_claims(
            _answer("c1", 100, [("s1", "/facts/1")]), CONTRACT, [_evidence("s1", facts)]
        )
        assert matched is False
        assert "CLAIM_PROVENANCE_FAILURE" in reasons

    def test_abstaining_with_no_evidence_is_incomplete_not_wrong(self):
        contract = _contract(("c1", "acme", "headcount", "fy2025"))
        answer = '{"claims": [{"claim_id": "c1", "status": "abstained"}]}'
        matched, reasons, reports = validate_claims(answer, contract, EVIDENCE)
        assert matched is None
        assert reasons == []
        assert reports[0].status == "INSUFFICIENT_EVIDENCE"

    def test_abstaining_over_conflicting_evidence_is_recorded_as_such(self):
        evidence = [
            _evidence("s1", _facts(("acme", "revenue", "fy2025", 100))),
            _evidence("s2", _facts(("acme", "revenue", "fy2025", 200))),
        ]
        answer = '{"claims": [{"claim_id": "c1", "status": "abstained"}]}'
        matched, _, reports = validate_claims(answer, CONTRACT, evidence)
        assert matched is None
        assert reports[0].status == "CONFLICTING_EVIDENCE"

    def test_abstaining_when_evidence_exists_is_still_incomplete(self):
        answer = '{"claims": [{"claim_id": "c1", "status": "abstained"}]}'
        matched, _, reports = validate_claims(answer, CONTRACT, EVIDENCE)
        assert matched is None
        assert reports[0].status == "ABSTAINED"


class TestParseClaimResponse:
    def test_duplicate_claim_ids_are_refused(self):
        answer = (
            '{"claims": ['
            '{"claim_id": "c1", "status": "abstained"},'
            '{"claim_id": "c1", "status": "abstained"}]}'
        )
        with pytest.raises(ValueError, match="Duplicate response claims"):
            parse_claim_response(answer)

    def test_an_oversized_response_is_refused(self):
        with pytest.raises(ValueError, match="exceeds budget"):
            parse_claim_response("x" * 100001)

    def test_comparable_claims_ignores_citation_order(self):
        first = _answer("c1", 100, [("s1", "/facts/0"), ("s2", "/facts/0")])
        second = _answer("c1", 100, [("s2", "/facts/0"), ("s1", "/facts/0")])
        assert comparable_claims(first) == comparable_claims(second)

    def test_comparable_claims_still_separates_different_values(self):
        first = _answer("c1", 100, [("s1", "/facts/0")])
        second = _answer("c1", 101, [("s1", "/facts/0")])
        assert comparable_claims(first) != comparable_claims(second)


# ── Cross-check comparison ──────────────────────────────────────────────


def _model(model_id, independence_group=None):
    return ModelDescriptor(
        model_id=model_id,
        context_window=32768,
        capabilities={"reasoning"},
        independence_group=independence_group,
    )


def _provider(provider_id):
    return ProviderSpec(
        provider_id=provider_id,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
    )


class TestIndependence:
    def test_a_different_provider_and_model_is_independent(self):
        primary = (1.0, _provider("a"), _model("one"))
        assert independent(primary, (1.0, _provider("b"), _model("two")))

    def test_the_same_provider_is_not_independent(self):
        primary = (1.0, _provider("a"), _model("one"))
        assert not independent(primary, (1.0, _provider("a"), _model("two")))

    def test_the_same_model_is_not_independent(self):
        primary = (1.0, _provider("a"), _model("one"))
        assert not independent(primary, (1.0, _provider("b"), _model("one")))

    def test_a_shared_independence_group_is_not_independent(self):
        """Two gateways reselling one upstream model are not two opinions."""
        primary = (1.0, _provider("a"), _model("one", independence_group="upstream"))
        candidate = (1.0, _provider("b"), _model("two", independence_group="upstream"))
        assert not independent(primary, candidate)

    def test_comparison_ignores_case_and_surrounding_space(self):
        primary = (1.0, _provider("a"), _model("one"))
        assert not independent(primary, (1.0, _provider("A"), _model("ONE")))


class _Response:
    def __init__(self, text):
        self.text = text


class _Request:
    def __init__(self, validation):
        self.validation = validation


class _Validation:
    def __init__(self, kind):
        self.kind = kind


class TestCompare:
    def test_arithmetic_agreement_is_exact_value(self):
        request = _Request(_Validation("arithmetic"))
        assert compare(request, _Response("345"), _Response("345.0"), False) == (
            True,
            "EXACT_VALUE",
        )

    def test_arithmetic_disagreement(self):
        request = _Request(_Validation("arithmetic"))
        assert compare(request, _Response("345"), _Response("346"), False) == (False, "EXACT_VALUE")

    def test_an_unparseable_arithmetic_answer_is_not_assessed(self):
        request = _Request(_Validation("arithmetic"))
        assert compare(request, _Response("345"), _Response("about 345"), False) == (
            None,
            "NOT_ASSESSED",
        )

    def test_json_agreement_ignores_key_order(self):
        request = _Request(_Validation("reference_json"))
        assert compare(
            request, _Response('{"a": 1, "b": 2}'), _Response('{"b": 2, "a": 1}'), False
        ) == (True, "EXACT_VALUE")

    def test_json_disagreement(self):
        request = _Request(_Validation("grounded_json"))
        assert compare(request, _Response('{"a": 1}'), _Response('{"a": 2}'), False) == (
            False,
            "EXACT_VALUE",
        )

    def test_claim_agreement_ignores_citation_order(self):
        request = _Request(_Validation("grounded_claims"))
        first = _answer("c1", 100, [("s1", "/facts/0"), ("s2", "/facts/0")])
        second = _answer("c1", 100, [("s2", "/facts/0"), ("s1", "/facts/0")])
        assert compare(request, _Response(first), _Response(second), False) == (True, "EXACT_VALUE")

    def test_validated_code_answers_agree_on_the_host_cases(self):
        request = _Request(_Validation("python_function"))
        assert compare(request, _Response("def f(): pass"), _Response("other"), True) == (
            True,
            "HOST_TEST_CASES",
        )

    def test_unvalidated_code_answers_are_not_compared_as_text(self):
        request = _Request(_Validation("python_function"))
        assert compare(request, _Response("a"), _Response("b"), False) == (None, "NOT_ASSESSED")

    def test_no_contract_is_not_assessed(self):
        assert compare(_Request(None), _Response("a"), _Response("a"), False) == (
            None,
            "NOT_ASSESSED",
        )
