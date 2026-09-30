"""Schema transport: what each dialect carries, and what the prompt says instead."""

import json

import pytest

from fair.providers.schema_dialects import (
    GEMINI,
    JSON_SCHEMA,
    OPENAI_STRICT,
    constraint_notes,
    transport_schema,
)
from fair.tools import schema_compat

# The shape that failed in production: strict-mode-correct structure, 53 constraint
# keywords no provider accepts, and two const schemas carrying no type.
CONCEPT_SCHEMA = {
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
                "required": ["concept_id", "drama", "passes", "elements"],
                "properties": {
                    "concept_id": {"type": "string", "minLength": 1},
                    "drama": {"type": "integer", "minimum": 4, "maximum": 10},
                    "passes": {"const": True},
                    "elements": {"type": "array", "maxItems": 0, "items": {"type": "string"}},
                },
            },
        },
    },
}


def _paths(schema, key, path="result"):
    """Every path under ``schema`` where ``key`` appears."""
    found = []
    if isinstance(schema, dict):
        if key in schema:
            found.append(path)
        for name, item in (schema.get("properties") or {}).items():
            found += _paths(item, key, f"{path}.{name}")
        if "items" in schema:
            found += _paths(schema["items"], key, f"{path}[]")
    return found


class TestTransportSchema:
    def test_constraint_keywords_are_dropped_for_openai_strict(self):
        sent = transport_schema(CONCEPT_SCHEMA, OPENAI_STRICT)
        for keyword in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
            assert _paths(sent, keyword) == [], keyword

    def test_shape_survives_intact(self):
        sent = transport_schema(CONCEPT_SCHEMA, OPENAI_STRICT)
        concept = sent["properties"]["concepts"]["items"]
        assert sent["required"] == ["mechanism_id", "concepts"]
        assert sent["properties"]["concepts"]["type"] == "array"
        assert set(concept["properties"]) == {"concept_id", "drama", "passes", "elements"}
        assert concept["properties"]["drama"]["type"] == "integer"

    def test_const_becomes_a_single_value_enum(self):
        sent = transport_schema(CONCEPT_SCHEMA, OPENAI_STRICT)
        assert sent["properties"]["mechanism_id"] == {
            "type": "string",
            "enum": ["payoff_reveal"],
        }
        assert _paths(sent, "const") == []

    def test_const_without_a_type_gains_the_inferred_one(self):
        """A bare {"const": true} is rejected outright; strict mode requires a type."""
        sent = transport_schema(CONCEPT_SCHEMA, OPENAI_STRICT)
        assert sent["properties"]["concepts"]["items"]["properties"]["passes"] == {
            "type": "boolean",
            "enum": [True],
        }

    def test_a_boolean_const_is_not_typed_as_an_integer(self):
        sent = transport_schema({"const": False}, OPENAI_STRICT)
        assert sent == {"type": "boolean", "enum": [False]}

    def test_strict_mode_keeps_additional_properties_because_it_requires_it(self):
        sent = transport_schema(CONCEPT_SCHEMA, OPENAI_STRICT)
        assert len(_paths(sent, "additionalProperties")) == 2

    def test_gemini_drops_additional_properties(self):
        assert _paths(transport_schema(CONCEPT_SCHEMA, GEMINI), "additionalProperties") == []

    def test_json_schema_dialect_passes_everything_through(self):
        assert transport_schema(CONCEPT_SCHEMA, JSON_SCHEMA) == CONCEPT_SCHEMA

    def test_the_caller_schema_is_never_mutated(self):
        before = json.dumps(CONCEPT_SCHEMA, sort_keys=True)
        for dialect in (OPENAI_STRICT, GEMINI, JSON_SCHEMA):
            transport_schema(CONCEPT_SCHEMA, dialect)
        assert json.dumps(CONCEPT_SCHEMA, sort_keys=True) == before

    def test_the_transported_copy_is_not_shared_with_the_caller(self):
        sent = transport_schema(CONCEPT_SCHEMA, JSON_SCHEMA)
        sent["properties"]["mechanism_id"]["type"] = "number"
        assert CONCEPT_SCHEMA["properties"]["mechanism_id"]["type"] == "string"


class TestConstraintNotes:
    def _note(self, schema=None, dialect=OPENAI_STRICT):
        return constraint_notes(schema or CONCEPT_SCHEMA, dialect)

    def test_dropped_bounds_are_restated_for_the_model(self):
        note = self._note()
        assert "result.concepts: 1 to 5 items" in note
        assert "result.concepts[].drama: between 4 and 10" in note
        assert "result.concepts[].elements: must be an empty array" in note

    def test_repeated_non_empty_constraints_collapse_into_one_counted_line(self):
        assert "1 in all, must not be empty" in self._note()

    def test_a_schema_with_no_dropped_constraints_needs_no_note(self):
        assert self._note({"type": "object", "properties": {"a": {"type": "string"}}}) is None

    def test_a_pass_through_dialect_needs_no_note(self):
        assert self._note(dialect=JSON_SCHEMA) is None

    def test_the_note_is_bounded(self):
        wide = {
            "type": "object",
            "properties": {
                f"f{i}": {"type": "array", "minItems": i, "maxItems": i + 1} for i in range(200)
            },
        }
        assert len(constraint_notes(wide, OPENAI_STRICT)) <= 1400

    @pytest.mark.parametrize(
        ("node", "expected"),
        [
            ({"type": "string", "minLength": 5}, "at least 5 characters"),
            ({"type": "string", "pattern": "^a+$"}, "must match ^a+$"),
            ({"type": "array", "minItems": 2}, "at least 2 items"),
            ({"type": "array", "maxItems": 3}, "at most 3 items"),
            ({"type": "integer", "minimum": 1}, "at least 1"),
            ({"type": "array", "minItems": 1, "uniqueItems": True}, "items must be unique"),
        ],
    )
    def test_each_dropped_constraint_is_rendered(self, node, expected):
        schema = {"type": "object", "properties": {"field": node}}
        assert expected in constraint_notes(schema, OPENAI_STRICT)


class TestSchemaCompat:
    def test_a_strict_correct_schema_reports_nothing_to_fix(self):
        assert schema_compat.blocking_findings(CONCEPT_SCHEMA, OPENAI_STRICT) == []

    def test_an_object_without_additional_properties_is_an_author_fix(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        problems = [text for _, text in schema_compat.blocking_findings(schema, OPENAI_STRICT)]
        assert any("additionalProperties" in text for text in problems)

    def test_an_optional_property_is_an_author_fix_not_a_silent_rewrite(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
        }
        problems = [text for _, text in schema_compat.blocking_findings(schema, OPENAI_STRICT)]
        assert any("missing b" in text for text in problems)
        # The transform must not have quietly made b required.
        assert transport_schema(schema, OPENAI_STRICT)["required"] == ["a"]

    def test_combinators_are_reported_for_every_dialect(self):
        schema = {"type": "object", "properties": {"a": {"oneOf": [{"type": "string"}]}}}
        for dialect in (OPENAI_STRICT, GEMINI):
            problems = [text for _, text in schema_compat.blocking_findings(schema, dialect)]
            assert any("oneOf" in text for text in problems)

    def test_dropped_keyword_counts_match_the_schema(self):
        dropped = schema_compat.dropped_keywords(CONCEPT_SCHEMA, OPENAI_STRICT)
        assert len(dropped["minItems"]) == 1
        assert len(dropped["const (rewritten as enum)"]) == 2

    def test_every_provider_dialect_is_one_this_module_knows(self):
        from fair.providers.schema_dialects import DIALECTS

        assert set(schema_compat.provider_dialects()) <= set(DIALECTS)

    def test_a_request_envelope_is_unwrapped(self):
        document = {"mechanism_id": "x", "expected_schema": CONCEPT_SCHEMA}
        assert schema_compat.find_schema(document) is CONCEPT_SCHEMA
        assert schema_compat.find_schema(document, "expected_schema") is CONCEPT_SCHEMA

    def test_a_document_with_no_schema_is_refused(self):
        with pytest.raises(KeyError):
            schema_compat.find_schema({"mechanism_id": "x"})

    def test_the_exit_code_reports_whether_the_schema_needs_work(self, tmp_path, capsys):
        good = tmp_path / "good.json"
        good.write_text(json.dumps(CONCEPT_SCHEMA), encoding="utf-8")
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"type": "object", "properties": {}}), encoding="utf-8")
        assert schema_compat.main([str(good)]) == 0
        assert schema_compat.main([str(bad)]) == 1

    def test_emit_prints_the_transported_schema(self, tmp_path, capsys):
        path = tmp_path / "s.json"
        path.write_text(json.dumps(CONCEPT_SCHEMA), encoding="utf-8")
        assert schema_compat.main([str(path), "--emit", OPENAI_STRICT]) == 0
        assert json.loads(capsys.readouterr().out) == transport_schema(
            CONCEPT_SCHEMA, OPENAI_STRICT
        )
