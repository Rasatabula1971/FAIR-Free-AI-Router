"""A JSON answer inside a short wrapper is read, checked in full, and returned clean.

FAIR used to accept a JSON answer bare or inside one whole-answer code fence, and
call anything else malformed. "Here is your JSON: {...}" was a right answer thrown
away: it cost a free request and one of the three answered attempts.

The relaxation is narrow on purpose. One object or array, nothing outside it that
contains a bracket, and only a short wrapper. Whatever is lifted out is parsed as
strictly as before and still has to pass the whole contract.
"""

import json
import random
import re
from time import perf_counter

import pytest

from fair.config import RoutingSettings
from fair.embedded.router import EmbeddedRouter, _output
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.claims import validate_claims
from fair.quality.contracts import ClaimsValidation, Evidence
from fair.quality.engine import expects_json_document
from fair.quality.json_data import (
    MAX_WRAPPER_CHARS,
    _fenced,
    json_document,
    json_text,
    read_json,
)
from fair.schemas.api import SolveRequest
from fair.schemas.domain import NormalizedModelResponse, ProviderSpec

ITEMS = {
    "type": "object",
    "properties": {"items": {"type": "array"}},
    "required": ["items"],
    "additionalProperties": False,
}
DOCUMENT = '{"items": [1, 2]}'


def _spec(name="a", model="model"):
    return ProviderSpec(
        provider_id=name,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        models=[
            {
                "model_id": model,
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
        ],
    )


def _router(*texts):
    registry = Registry()
    adapters = []
    for number, text in enumerate(texts):
        name = f"p{number}"
        adapters.append(MockAdapter(name, text=text))
        registry.register(_spec(name, f"m{number}"), adapters[-1])
    thresholds = {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}
    return EmbeddedRouter(registry, RoutingSettings(), thresholds), adapters


def _request(**overrides):
    fields = {"client_id": "c", "task": "list them", "task_type": "extraction"}
    return SolveRequest.model_validate(fields | overrides)


class TestReadingAnAnswer:
    @pytest.mark.parametrize(
        "text, wrapper",
        [
            (DOCUMENT, None),
            (f"  {DOCUMENT}\n", None),
            (f"```json\n{DOCUMENT}\n```", "FENCE"),
            (f"```\n{DOCUMENT}\n```", "FENCE"),
            (f"Here is your JSON: {DOCUMENT}", "PROSE"),
            (f"Here you go:\n{DOCUMENT}\nEnjoy!", "PROSE"),
            (f"{DOCUMENT}\n\nLet me know if you need anything else.", "PROSE"),
            (f"Sure!\n```json\n{DOCUMENT}\n```\nHope that helps.", "PROSE"),
            # A quoted word in the sentence is not a key: the colon does not follow it.
            (f'The "items" list you asked for: {DOCUMENT}', "PROSE"),
            (f"{DOCUMENT}, as requested.", "PROSE"),
        ],
    )
    def test_the_document_is_found_and_what_was_removed_is_named(self, text, wrapper):
        value, document, removed = read_json(text)
        assert value == {"items": [1, 2]}
        assert removed == wrapper
        assert json.loads(document) == value
        assert json_document(text) == value

    def test_a_bare_answer_comes_back_untouched(self):
        text = f"  {DOCUMENT}\n"
        assert json_text(text) is text

    @pytest.mark.parametrize(
        "text",
        [f"```json\n{DOCUMENT}\n```", f"Here you go:\n{DOCUMENT}\nEnjoy!"],
    )
    def test_a_wrapped_answer_comes_back_as_the_json_alone(self, text):
        assert json_text(text) == DOCUMENT

    def test_the_document_is_the_models_own_text_not_a_rewrite(self):
        """Re-serialising would reorder keys and reformat numbers nobody asked to change."""
        written = '{"b": 1.50, "a": [ 1,2 ]}'
        assert json_text(f"Result: {written} Done.") == written

    def test_an_array_is_lifted_out_too(self):
        assert read_json("The list is [1, 2, 3] as requested.") == ([1, 2, 3], "[1, 2, 3]", "PROSE")

    def test_brackets_inside_the_document_are_part_of_it(self):
        written = '{"note": "use {name} and [index]", "items": [[1], {"a": {}}]}'
        assert json_text(f"Here: {written}") == written

    @pytest.mark.parametrize(
        "text",
        [
            # Two documents: an example and an answer. FAIR does not choose.
            'Example: {"items": [1]} Final: {"items": [2]}',
            '{"items": [1]}\n{"items": [2]}',
            "[1][2]",
            # Prose that uses brackets itself, on either side.
            'Use {name} here: {"items": []}',
            'See [1]: {"items": []}',
            '{"items": []} (see note [a])',
            'Closed early } then {"items": []}',
            '{"items": []} and then an opening [',
            # A value inside a larger structure that lost its outer braces. Lifting it
            # out would be choosing one part of a broken object and calling it the answer.
            '"status": "error", "data": {"items": [1]}',
            '"items": [1, 2]',
            '"data" : \n {"items": []}',
            '{"items": [1]}, "other": 2',
            '{"items": [1]} ,\n "other": 2',
            # No object or array at all: a value in a sentence is not a wrapped answer.
            "The answer is 42",
            'The answer is "yes"',
            "no json here",
            "",
            # The span is parsed as strictly as a bare answer is.
            "Here: {'items': []}",
            'Here: {"items": [1,]}',
            'Here: {"items": [1], "items": [2]}',
            'Here: {"items": NaN}',
            'Here: {"items": [1, 2',
            "Here: ]{",
        ],
    )
    def test_anything_ambiguous_or_malformed_is_still_refused(self, text):
        with pytest.raises(ValueError):
            read_json(text)

    def test_a_duplicate_key_is_not_rescued_by_treating_the_answer_as_wrapped(self):
        with pytest.raises(ValueError, match="Duplicate JSON key"):
            read_json('{"a": 1, "a": 2}')

    def test_the_wrapper_has_to_be_short(self):
        """A paragraph is where a model hedges, and that is not thrown away."""
        at_limit = "x" * MAX_WRAPPER_CHARS
        assert read_json(f"{at_limit} {DOCUMENT}")[2] == "PROSE"
        with pytest.raises(ValueError, match="Too much text"):
            read_json(f"{at_limit}x {DOCUMENT}")
        # Split across both sides it is still one wrapper.
        half = "x" * (MAX_WRAPPER_CHARS // 2)
        assert read_json(f"{half} {DOCUMENT} {half}")[2] == "PROSE"
        with pytest.raises(ValueError, match="Too much text"):
            read_json(f"{half}x {DOCUMENT} {half}")

    def test_whitespace_does_not_count_toward_the_wrapper(self):
        padded = "Here:" + "\n" * (MAX_WRAPPER_CHARS * 2) + DOCUMENT
        assert read_json(padded)[2] == "PROSE"


# The single pattern the fence used to be read with. Kept here only to prove the
# linear reader gives the same body; it must never be run on untrusted text again.
_OLD_FENCE = re.compile(r"^\s*```(?:[A-Za-z0-9_-]+)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


class TestTheFenceIsReadInLinearTime:
    @pytest.mark.parametrize(
        "text",
        [
            "```json" + "\n" * 20000,  # opened, then ran away in newlines: was cubic
            "```json" + "\n" * 20000 + DOCUMENT,
            "```json\n" + DOCUMENT + " " * 90000 + "x",  # was quadratic
            "```" * 30000,
            " " * 100000,
        ],
    )
    def test_a_runaway_answer_is_read_or_refused_at_once(self, text):
        """1,600 newlines after an opening fence used to hold the event loop for
        seven seconds, and 3,000 for most of a minute. Nothing times the gate out."""
        started = perf_counter()
        try:
            read_json(text)
        except ValueError:
            pass
        assert perf_counter() - started < 1.0

    def test_it_reads_exactly_what_the_old_pattern_read(self):
        rng = random.Random(20261007)
        pieces = ["```", "``", "`", "json", "x-1_", "{", "}", "[", "]", '"', "1", "é"]
        pieces += [
            " ",
            "\n",
            "\t",
            "\r",
            "\x0b",
            "\x0c",
            "\x1c",
            "\x85",
            "\xa0",
            "\u2028",
            "\ufeff",
        ]
        for _ in range(20000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
            match = _OLD_FENCE.match(text)
            assert _fenced(text) == (match.group(1) if match else None), repr(text)

    @pytest.mark.parametrize(
        "text, body",
        [
            ("```json\n{}\n```", "{}"),
            ("  ```\n\n{}  \n\n```  \n", "{}"),
            ("```json{}```", "{}"),
            ("``````", ""),
            ("```json```", ""),
            ("```", None),
            ("`````", None),
            ("```json\n{}", None),
            ("{}\n```", None),
            ("x```json\n{}\n```", None),
            ("```json\n{}\n```x", None),
        ],
    )
    def test_only_a_fence_around_the_whole_answer_counts(self, text, body):
        assert _fenced(text) == body


class TestTheQualityGate:
    async def test_a_wrapped_answer_is_accepted_and_returned_as_json(self):
        router, _ = _router(f"Here is your JSON:\n{DOCUMENT}\nEnjoy!")
        result = await router.solve(_request(expected_schema=ITEMS))
        assert result.status == "ACCEPTED"
        assert result.output == DOCUMENT
        checks = result.attempts[0].quality.validator_results
        assert (checks["schema"], checks["json_wrapper"]) == ("PASS", "PROSE_REMOVED")

    async def test_a_fenced_answer_is_returned_as_json_too(self):
        """It was accepted before and handed back with the fence still on it."""
        router, _ = _router(f"```json\n{DOCUMENT}\n```")
        result = await router.solve(_request(expected_schema=ITEMS))
        assert (result.status, result.output) == ("ACCEPTED", DOCUMENT)
        assert "json_wrapper" not in result.attempts[0].quality.validator_results

    async def test_a_bare_answer_is_returned_exactly_as_written(self):
        router, _ = _router(f"{DOCUMENT}\n")
        result = await router.solve(_request(expected_schema=ITEMS))
        assert (result.status, result.output) == ("ACCEPTED", f"{DOCUMENT}\n")
        assert "json_wrapper" not in result.attempts[0].quality.validator_results

    @pytest.mark.parametrize(
        "text",
        [
            'Here you go: {"other": 1}',  # wrong shape
            'Here you go: {"items": [], "extra": true}',  # a property the schema forbids
            'Example: {"items": [1]} Final: {"items": [2]}',  # two documents
        ],
    )
    async def test_the_whole_schema_still_has_to_pass(self, text):
        router, _ = _router(text)
        result = await router.solve(_request(expected_schema=ITEMS))
        assert result.status == "ESCALATION_REQUIRED"
        checks = result.attempts[0].quality.validator_results
        assert checks["schema"] == "FAIL"
        assert "json_wrapper" not in checks
        assert result.output is None

    async def test_a_wrapped_answer_no_longer_costs_an_attempt(self):
        """Before, the first model's right answer was refused and a second was asked."""
        router, adapters = _router(f"Here you go: {DOCUMENT}", DOCUMENT)
        result = await router.solve(_request(expected_schema=ITEMS))
        assert result.status == "ACCEPTED"
        assert len(result.attempts) == 1
        assert sum(adapter.calls for adapter in adapters) == 1

    async def test_a_reference_match_reads_through_the_wrapper(self):
        router, _ = _router('Result: {"x": 1}')
        result = await router.solve(
            _request(validation={"kind": "reference_json", "expected": {"x": 1}})
        )
        assert (result.status, result.output) == ("ACCEPTED", '{"x": 1}')
        assert result.verification_state == "HOST_REFERENCE_MATCH"
        assert result.attempts[0].quality.validator_results["json_wrapper"] == "PROSE_REMOVED"

    async def test_a_wrapped_answer_with_the_wrong_value_still_fails_its_reference(self):
        router, _ = _router('Result: {"x": 2}')
        result = await router.solve(
            _request(validation={"kind": "reference_json", "expected": {"x": 1}})
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert "json_wrapper" not in result.attempts[0].quality.validator_results

    async def test_a_grounded_match_reads_through_the_wrapper(self):
        document = json.dumps(
            {
                "answer": {"answer": "x"},
                "sources": {"answer": {"source_id": "s1", "pointer": "/answer"}},
            }
        )
        router, _ = _router(f"Extracted: {document}")
        result = await router.solve(
            _request(
                validation={
                    "kind": "grounded_json",
                    "fields": [{"output_key": "answer", "source_id": "s1", "pointer": "/answer"}],
                },
                evidence=[{"source_id": "s1", "text": '{"answer": "x"}'}],
            )
        )
        assert (result.status, result.output) == ("ACCEPTED", document)
        assert result.attempts[0].quality.validator_results["json_wrapper"] == "PROSE_REMOVED"

    async def test_prose_that_mentions_json_is_not_reduced_to_it(self):
        """Only a request that asked for JSON gets JSON back. An explanation that
        contains an example is the answer, example and all."""
        written = 'A config file looks like this: {"debug": true}'
        router, _ = _router(written)
        result = await router.solve(
            SolveRequest.model_validate(
                {"client_id": "c", "task": "explain config files", "accept_unverified": True}
            )
        )
        assert (result.status, result.output) == ("ACCEPTED_UNVERIFIED", written)

    async def test_two_models_agree_whatever_each_wrapped_its_answer_in(self):
        answer = {"type": "object", "required": ["answer"]}
        router, adapters = _router('Here it is: {"answer": 345}', '```json\n{"answer":345}\n```')
        result = await router.solve(_request(expected_schema=answer, cross_check_required=True))
        assert result.status == "ACCEPTED"
        assert result.cross_check.state == "PASSED"
        assert sum(adapter.calls for adapter in adapters) == 2

    async def test_code_is_not_read_as_a_wrapper_around_its_own_list(self):
        """A function contract owns the answer. With a schema as well, the code was
        accepted as the array literal inside it and returned as that fragment."""
        code = "def f(a):\n    return [1, 2, 3]"
        ask = _request(
            task="write f",
            task_type="coding",
            expected_schema={"type": "array"},
            validation={
                "kind": "python_function",
                "function_name": "f",
                "cases": [{"arguments": [0], "expected": [1, 2, 3]}],
            },
        )
        router, _ = _router(code)
        result = await router.solve(ask)
        assert result.status == "ESCALATION_REQUIRED"
        quality = result.attempts[0].quality
        assert quality.reject_reasons == ["SCHEMA_FAILURE"]
        assert "json_wrapper" not in quality.validator_results
        # And were such an answer ever returned, it would be the code, not a slice.
        response = NormalizedModelResponse(provider_id="p", model_id="m", text=code)
        assert _output(ask, response) == code

    @pytest.mark.parametrize(
        "fields, expected",
        [
            ({"expected_schema": ITEMS}, True),
            ({"validation": {"kind": "reference_json", "expected": {"x": 1}}}, True),
            (
                {
                    "expected_schema": ITEMS,
                    "validation": {"kind": "reference_json", "expected": {"x": 1}},
                },
                True,
            ),
            ({}, False),
            ({"validation": {"kind": "arithmetic", "expression": "1+1"}}, False),
            (
                {
                    "expected_schema": {"type": "integer"},
                    "validation": {"kind": "arithmetic", "expression": "1+1"},
                },
                False,
            ),
        ],
    )
    def test_only_a_json_contract_makes_the_answer_a_json_document(self, fields, expected):
        assert expects_json_document(_request(**fields)) is expected

    async def test_a_number_contract_with_a_schema_is_not_read_through_prose(self):
        router, _ = _router("The answer is [2].")
        result = await router.solve(
            _request(
                task="1+1",
                expected_schema={"type": "array"},
                validation={"kind": "arithmetic", "expression": "1+1"},
            )
        )
        assert result.status == "ESCALATION_REQUIRED"
        assert "SCHEMA_FAILURE" in result.attempts[0].quality.reject_reasons

    def test_an_answer_that_cannot_be_read_as_json_is_handed_back_as_written(self):
        """The gate refuses these before they are returned; if one ever got through,
        the caller is owed the text rather than an exception from tidying it."""
        response = NormalizedModelResponse(provider_id="p", model_id="m", text="not json at all")
        assert _output(_request(expected_schema=ITEMS), response) == "not json at all"


class TestWhatStaysStrict:
    def test_a_claims_answer_wrapped_in_prose_is_still_a_format_failure(self):
        """Only the JSON document checks were relaxed."""
        contract = ClaimsValidation.model_validate(
            {
                "kind": "grounded_claims",
                "claims": [
                    {"claim_id": "c1", "subject": "acme", "predicate": "revenue", "context": "fy"}
                ],
            }
        )
        facts = json.dumps(
            {"facts": [{"subject": "acme", "predicate": "revenue", "context": "fy", "value": 100}]}
        )
        evidence = [Evidence.model_validate({"source_id": "s1", "text": facts})]
        answer = json.dumps(
            {
                "claims": [
                    {
                        "claim_id": "c1",
                        "status": "answered",
                        "value": 100,
                        "sources": [{"source_id": "s1", "pointer": "/facts/0"}],
                    }
                ]
            }
        )
        assert validate_claims(answer, contract, evidence)[0] is True
        matched, reasons, _ = validate_claims(f"Here you go: {answer}", contract, evidence)
        assert (matched, reasons) == (False, ["CLAIM_FORMAT_FAILURE"])
