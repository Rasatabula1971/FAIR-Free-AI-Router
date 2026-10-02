"""Schemas whose evaluation cost their own size does not bound are refused."""

import json
import subprocess
import sys
import textwrap

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from fair.schemas.api import SolveRequest
from fair.schemas.schema_policy import find_unbounded_keyword

EVIL = "^(a+)+$"


def _obj(**properties):
    return {"type": "object", "properties": properties, "required": list(properties)}


@pytest.mark.parametrize(
    "schema, keyword",
    [
        (_obj(value={"type": "string", "pattern": EVIL}), "pattern"),
        ({"type": "object", "patternProperties": {EVIL: {"type": "string"}}}, "patternProperties"),
        (_obj(a=_obj(b={"type": "string", "pattern": EVIL})), "pattern"),
        ({"type": "array", "items": {"type": "string", "pattern": EVIL}}, "pattern"),
        ({"type": "array", "items": [{"type": "string", "pattern": EVIL}]}, "pattern"),
        ({"type": "array", "prefixItems": [{"type": "string", "pattern": EVIL}]}, "pattern"),
        ({"anyOf": [{"type": "integer"}, {"type": "string", "pattern": EVIL}]}, "pattern"),
        ({"not": {"type": "string", "pattern": EVIL}}, "pattern"),
        ({"if": {"type": "string", "pattern": EVIL}, "then": {}}, "pattern"),
        ({"type": "object", "propertyNames": {"pattern": EVIL}}, "pattern"),
        (
            {"type": "object", "additionalProperties": {"type": "string", "pattern": EVIL}},
            "pattern",
        ),
        ({"$defs": {"x": {"type": "string", "pattern": EVIL}}, "type": "object"}, "pattern"),
    ],
)
def test_regex_keyword_is_found_in_every_subschema_position(schema, keyword):
    assert find_unbounded_keyword(schema) == keyword


@pytest.mark.parametrize(
    "schema",
    [
        _obj(title={"type": "string", "minLength": 1, "maxLength": 100}),
        # A property *named* "pattern" is data, not the keyword.
        _obj(pattern={"type": "string"}),
        {"type": "object", "properties": {"patternProperties": {"type": "integer"}}},
        # enum/const/default values are data.
        {"enum": [{"pattern": EVIL}]},
        {"const": {"patternProperties": 1}},
        {"type": "object", "default": {"pattern": EVIL}},
        {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "maxItems": 5},
    ],
)
def test_ordinary_schemas_are_not_refused(schema):
    assert find_unbounded_keyword(schema) is None


def test_deeply_nested_schema_does_not_exhaust_the_stack():
    schema = {"type": "string", "pattern": EVIL}
    for _ in range(50_000):
        schema = {"type": "object", "properties": {"x": schema}}
    assert find_unbounded_keyword(schema) == "pattern"


def test_solve_request_refuses_a_regex_schema():
    with pytest.raises(ValidationError, match="pattern"):
        SolveRequest(
            client_id="c",
            task="t",
            quality_level="standard",
            expected_schema=_obj(value={"type": "string", "pattern": EVIL}),
        )


def test_solve_request_accepts_an_ordinary_schema():
    request = SolveRequest(
        client_id="c",
        task="t",
        quality_level="standard",
        expected_schema=_obj(value={"type": "string"}),
    )
    assert request.expected_schema is not None


def test_a_refused_schema_never_reaches_the_validator():
    """End to end in a subprocess with a hard timeout: admission refuses fast."""
    script = textwrap.dedent(
        f"""
        from pydantic import ValidationError
        from fair.schemas.api import SolveRequest
        schema = {{"type": "object", "properties": {{"value": {{"type": "string", "pattern": {EVIL!r}}}}}}}
        try:
            SolveRequest(client_id="c", task="t", quality_level="standard", expected_schema=schema)
        except ValidationError:
            print("refused")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.stdout.strip() == "refused", result.stderr


# ── Annotation collection buys exponential time with linear text ────────


def _annotated(depth, width=1):
    """The audit's construction: unevaluatedProperties under nested combinators.

    Each level makes the validator re-evaluate everything beneath it to decide
    which properties were already evaluated, so the work multiplies with depth
    while the text only adds a constant per level.
    """
    schema = {}
    for _ in range(depth):
        schema = {"allOf": [schema] * width, "unevaluatedProperties": False}
    return schema


class TestCostThatSizeDoesNotBound:
    @pytest.mark.parametrize("keyword", ["unevaluatedProperties", "unevaluatedItems"])
    def test_an_annotation_collecting_keyword_is_refused(self, keyword):
        assert find_unbounded_keyword({"type": "object", keyword: False}) == keyword

    @pytest.mark.parametrize("depth", [12, 15])
    def test_the_measured_attack_is_refused(self, depth):
        """677 bytes took 10.4 seconds; 542 took 0.55. Both under the size cap."""
        schema = _annotated(depth)
        assert len(json.dumps(schema)) < 20_000
        assert find_unbounded_keyword(schema) == "unevaluatedProperties"

    def test_the_width_variant_is_refused_too(self):
        """A cap on nesting *depth* would have let this through: 12,497 bytes, 2.3s."""
        schema = _annotated(8, width=2)
        assert 12_000 < len(json.dumps(schema)) < 20_000
        assert find_unbounded_keyword(schema) == "unevaluatedProperties"

    def test_it_is_found_however_deeply_it_is_buried(self):
        schema = {"type": "object", "unevaluatedProperties": False}
        for _ in range(1_000):
            schema = {"allOf": [{"type": "object", "properties": {"x": schema}}]}
        assert find_unbounded_keyword(schema) == "unevaluatedProperties"

    def test_a_property_named_like_the_keyword_is_data(self):
        """Property names are the caller's, not JSON Schema's."""
        schema = _obj(unevaluatedProperties={"type": "string"}, pattern={"type": "integer"})
        assert find_unbounded_keyword(schema) is None

    def test_solve_request_refuses_it_and_says_why(self):
        with pytest.raises(ValidationError, match="unevaluatedProperties"):
            SolveRequest(
                client_id="c",
                task="t",
                quality_level="standard",
                expected_schema=_annotated(15),
            )

    def test_combinators_without_annotation_collection_are_still_allowed(self):
        """allOf alone is linear -- refusing it would cost real schemas for nothing."""
        schema = {"allOf": [{"type": "object"}, {"required": ["a"]}]}
        assert find_unbounded_keyword(schema) is None


class TestOrdinarySchemasStillPass:
    """The shape of a real caller's schema, which must keep working."""

    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["mechanism_id", "concepts"],
        "properties": {
            "mechanism_id": {"type": "string", "const": "payoff_reveal"},
            "concepts": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["concept_id", "score"],
                    "properties": {
                        "concept_id": {"type": "string", "minLength": 1},
                        "score": {"type": "integer", "minimum": 4, "maximum": 10},
                        "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
                    },
                },
            },
        },
    }

    def test_it_is_admitted(self):
        assert find_unbounded_keyword(self.SCHEMA) is None
        SolveRequest(client_id="c", task="t", quality_level="standard", expected_schema=self.SCHEMA)

    def test_it_still_validates_a_response(self):
        """Admission must not have cost the local enforcement it exists to protect."""
        good = {"mechanism_id": "payoff_reveal", "concepts": [{"concept_id": "x", "score": 7}]}
        bad = {"mechanism_id": "payoff_reveal", "concepts": [{"concept_id": "", "score": 99}]}
        Draft202012Validator(self.SCHEMA).validate(good)
        with pytest.raises(JsonSchemaValidationError):
            Draft202012Validator(self.SCHEMA).validate(bad)
