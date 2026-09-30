"""Schemas that would run a caller-chosen regular expression are refused."""

import subprocess
import sys
import textwrap

import pytest
from pydantic import ValidationError

from fair.schemas.api import SolveRequest
from fair.schemas.schema_policy import find_regex_keyword

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
    assert find_regex_keyword(schema) == keyword


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
    assert find_regex_keyword(schema) is None


def test_deeply_nested_schema_does_not_exhaust_the_stack():
    schema = {"type": "string", "pattern": EVIL}
    for _ in range(50_000):
        schema = {"type": "object", "properties": {"x": schema}}
    assert find_regex_keyword(schema) == "pattern"


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
