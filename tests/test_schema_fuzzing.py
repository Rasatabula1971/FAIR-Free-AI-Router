"""Randomised schemas through the transport transform.

The design rests on one claim: the copy a provider receives carries shape, the
caller's original enforces the constraints, and dropping a keyword in transit
weakens nothing. Three properties have to hold for that to be true, and the
hand-written cases in test_schema_dialects.py check them on schemas chosen by
whoever wrote the test:

  1. The transform never rejects what the original accepts. If transport were
     stricter anywhere, FAIR would be steering a model away from an answer its
     own validator would have taken.
  2. The caller's schema is never mutated. It is the thing used to validate the
     response afterwards, so a transform that edited it in place would weaken
     the guarantee invisibly and permanently.
  3. Nothing leaves in the payload that the dialect cannot parse -- which is the
     production failure this module was written for.

Generated with a seeded RNG rather than Hypothesis: reproducible in CI, and no
new dependency. Each case prints its seed on failure.
"""

import copy
import json
import random

import jsonschema
import pytest

from fair.providers.schema_dialects import (
    DIALECTS,
    GEMINI,
    JSON_SCHEMA,
    OPENAI_STRICT,
    constraint_notes,
    transport_schema,
)

CASES = 300
DIALECT_NAMES = [OPENAI_STRICT, GEMINI, JSON_SCHEMA]

# Everything the transform is allowed to emit: the shape keywords each dialect
# keeps, plus the 'enum'/'type' a rewritten 'const' becomes.
_EMITTABLE = frozenset({"enum", "type"})


# ── Generators ───────────────────────────────────────────────────────────


def _word(rng, length):
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(length))


def _paired(rng, depth=0):
    """A schema and a value that satisfies it.

    Generating the two together is what makes property 1 testable: a random
    instance checked against a random schema is almost always invalid under
    both, so the implication holds vacuously and proves nothing.
    """
    kinds = ["string", "integer", "number", "boolean", "enum", "const"]
    if depth < 3:
        kinds += ["object", "array", "anyOf"]
    kind = rng.choice(kinds)

    if kind == "string":
        low = rng.choice([None, 0, 1, 3])
        high = None if low is None else rng.choice([None, (low or 0) + 4])
        schema = {"type": "string"}
        if low is not None:
            schema["minLength"] = low
        if high is not None:
            schema["maxLength"] = high
        return schema, _word(rng, max(low or 1, 1))
    if kind in {"integer", "number"}:
        low = rng.choice([None, -10, 0, 5])
        high = None if low is None else low + rng.choice([0, 20])
        schema = {"type": kind}
        if low is not None:
            schema["minimum"] = low
        if high is not None:
            schema["maximum"] = high
        value = low if low is not None else rng.randint(-50, 50)
        return schema, float(value) + 0.5 if kind == "number" and low != high else value
    if kind == "boolean":
        return {"type": "boolean"}, rng.choice([True, False])
    if kind == "enum":
        values = rng.sample(["red", "green", "blue", "amber"], rng.randint(1, 4))
        return {"type": "string", "enum": values}, rng.choice(values)
    if kind == "const":
        value = rng.choice(["fixed", 7, True, 1.5, None])
        schema = {"const": value}
        # Sometimes stated, sometimes left for the transform to infer.
        if rng.random() < 0.5 and value is not None:
            schema["type"] = {str: "string", bool: "boolean", int: "integer", float: "number"}[
                type(value)
            ]
        return schema, value
    if kind == "array":
        item_schema, item_value = _paired(rng, depth + 1)
        low = rng.choice([None, 0, 1, 2])
        schema = {"type": "array", "items": item_schema}
        if low is not None:
            schema["minItems"] = low
            if rng.random() < 0.5:
                schema["maxItems"] = low + 3
        return schema, [copy.deepcopy(item_value) for _ in range(max(low or 1, 1))]
    if kind == "anyOf":
        first, value = _paired(rng, depth + 1)
        second, _ = _paired(rng, depth + 1)
        return {"anyOf": [first, second]}, value

    properties, example, names = {}, {}, []
    for index in range(rng.randint(1, 3)):
        name = f"f{index}_{_word(rng, 3)}"
        properties[name], example[name] = _paired(rng, depth + 1)
        names.append(name)
    schema = {"type": "object", "properties": properties}
    required = rng.sample(names, rng.randint(0, len(names)))
    if required:
        schema["required"] = sorted(required)
    if rng.random() < 0.5:
        schema["additionalProperties"] = False
    if rng.random() < 0.3:
        schema["description"] = "a generated object"
    return schema, example


def _with_defs(rng):
    """A root schema using $defs and $ref, as the production schema does."""
    target, value = _paired(rng, depth=1)
    return (
        {
            "type": "object",
            "properties": {"direct": target, "referenced": {"$ref": "#/$defs/Shared"}},
            "required": ["direct", "referenced"],
            "$defs": {"Shared": copy.deepcopy(target)},
        },
        {"direct": value, "referenced": copy.deepcopy(value)},
    )


_CONSTRAINTS = (
    ("minItems", 1),
    ("maxItems", 5),
    ("uniqueItems", True),
    ("minLength", 1),
    ("maxLength", 40),
    ("pattern", "^[a-z]+$"),
    ("format", "date-time"),
    ("minimum", 0),
    ("maximum", 99),
    ("exclusiveMinimum", 0),
    ("multipleOf", 2),
    ("default", "x"),
    ("examples", ["x"]),
    ("title", "T"),
    ("$comment", "c"),
    ("deprecated", True),
    ("readOnly", True),
    ("oneOf", [{"type": "string"}]),
    ("allOf", [{"type": "string"}]),
    ("not", {"type": "null"}),
    ("if", {"type": "string"}),
    ("propertyNames", {"pattern": "^[a-z]+$"}),
    ("patternProperties", {"^x": {"type": "string"}}),
    ("dependentRequired", {"a": ["b"]}),
    ("prefixItems", [{"type": "string"}]),
    ("contains", {"type": "string"}),
    ("unevaluatedProperties", False),
)


def _decorated(rng, depth=0):
    """A schema carrying the full constraint vocabulary, example not required.

    Soundness is checked by _paired; this one exists to push keywords the
    dialects cannot parse through the transform, including the combinators and
    annotations a real caller's schema carries.
    """
    schema, _ = _paired(rng, depth)
    for _ in range(rng.randint(0, 4)):
        key, value = rng.choice(_CONSTRAINTS)
        schema[key] = copy.deepcopy(value)
    return schema


# ── Helpers ──────────────────────────────────────────────────────────────


def _walk(node):
    """Every mutable container in a schema.

    Lists as well as dicts: 'required', 'enum' and 'examples' are lists, and a
    transform that copied the dicts but aliased those would still let an edit of
    one schema reach the other.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        yield node
        for item in node:
            yield from _walk(item)


def _schema_nodes(node, inside_properties=False):
    """Dict nodes that are schemas, not the maps that hold them."""
    if not isinstance(node, dict):
        return
    if not inside_properties:
        yield node
    for key, value in node.items():
        if key in {"properties", "$defs"} and isinstance(value, dict):
            for item in value.values():
                yield from _schema_nodes(item)
        elif key in {"items", "not", "if", "then", "else", "contains", "propertyNames"}:
            yield from _schema_nodes(value)
        elif key in {"anyOf", "allOf", "oneOf", "prefixItems"} and isinstance(value, list):
            for item in value:
                yield from _schema_nodes(item)


def _each(check, cases=CASES):
    """Run `check(seed)` over every case, naming the seed that failed.

    One test per property rather than one per (property, seed): the coverage is
    the same, the failure still reproduces from its seed, and a report of 25
    tests says more than one of 7,600.
    """
    for seed in range(cases):
        try:
            check(seed)
        except Exception as error:
            raise AssertionError(f"seed {seed}: {error}") from error


# Bounds _describe is expected to render. minLength:0 and minItems:0 are not
# here by accident: every string is at least 0 characters, so there is nothing
# to restate, and saying so would only crowd out the limits that do bite.
_BOUNDS = (
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)


def _restricts(schema, key):
    value = schema.get(key)
    if key in {"minLength", "minItems"}:
        return bool(value)
    return value is not None


# ── Property 1: transport never refuses what the original accepts ────────


@pytest.mark.parametrize("dialect", DIALECT_NAMES)
class TestTransportNeverNarrowsTheSchema:
    def test_a_value_the_original_accepts_the_transport_copy_accepts(self, dialect):
        def check(seed):
            schema, example = _paired(random.Random(seed))
            # If this fails the generator is wrong, not the transform.
            jsonschema.validate(example, schema)
            jsonschema.validate(example, transport_schema(schema, dialect))

        _each(check)

    def test_refs_and_defs_survive_the_transform(self, dialect):
        def check(seed):
            schema, example = _with_defs(random.Random(seed))
            jsonschema.validate(example, schema)
            transport = transport_schema(schema, dialect)
            jsonschema.validate(example, transport)
            # A dropped $defs leaves a $ref pointing at nothing, which jsonschema
            # reports as an unresolvable reference rather than a validation
            # failure -- so pin the pair explicitly.
            assert "$defs" in transport
            assert transport["properties"]["referenced"] == {"$ref": "#/$defs/Shared"}

        _each(check, cases=120)


# ── Property 2: the caller's own schema is untouched ─────────────────────


@pytest.mark.parametrize("dialect", DIALECT_NAMES)
class TestTheCallersSchemaIsNeverMutated:
    def test_the_original_is_unchanged_by_transport(self, dialect):
        def check(seed):
            schema = _decorated(random.Random(seed))
            before = copy.deepcopy(schema)
            transport_schema(schema, dialect)
            constraint_notes(schema, dialect)
            assert schema == before

        _each(check)

    def test_the_copy_shares_nothing_mutable_with_the_original(self, dialect):
        """A shared sub-dict would let a later edit of one reach the other."""

        def check(seed):
            schema = _decorated(random.Random(seed))
            transport = transport_schema(schema, dialect)
            originals = {id(node) for node in _walk(schema)}
            assert not originals & {id(node) for node in _walk(transport)}

        _each(check, cases=120)


# ── Property 3: only what the dialect can parse goes out ────────────────


@pytest.mark.parametrize("dialect", [OPENAI_STRICT, GEMINI])
class TestOnlyParseableKeywordsAreSent:
    def test_no_keyword_outside_the_dialect_survives(self, dialect):
        def check(seed):
            transport = transport_schema(_decorated(random.Random(seed)), dialect)
            allowed = DIALECTS[dialect].keeps | _EMITTABLE
            for node in _schema_nodes(transport):
                assert set(node) <= allowed, sorted(set(node) - allowed)

        _each(check)

    def test_const_is_always_rewritten(self, dialect):
        """Both dialects reject 'const'; a single-value 'enum' says the same thing."""

        def check(seed):
            schema = _decorated(random.Random(seed))
            transport = transport_schema(schema, dialect)
            assert all("const" not in node for node in _walk(transport))
            if "const" in schema and "enum" not in schema:
                assert transport["enum"] == [schema["const"]]

        _each(check)

    def test_the_payload_is_json(self, dialect):
        def check(seed):
            transport = transport_schema(_decorated(random.Random(seed)), dialect)
            assert json.loads(json.dumps(transport)) == transport

        _each(check)

    def test_transforming_a_transport_copy_changes_nothing(self, dialect):
        def check(seed):
            once = transport_schema(_decorated(random.Random(seed)), dialect)
            assert transport_schema(once, dialect) == once

        _each(check)


class TestDialectSpecifics:
    def test_gemini_never_receives_additional_properties(self):
        def check(seed):
            transport = transport_schema(_decorated(random.Random(seed)), GEMINI)
            assert all("additionalProperties" not in node for node in _walk(transport))

        _each(check)

    def test_a_dialect_that_takes_json_schema_gets_it_whole(self):
        def check(seed):
            schema = _decorated(random.Random(seed))
            assert transport_schema(schema, JSON_SCHEMA) == schema

        _each(check)


# ── What was dropped is still said, in the prompt ────────────────────────


class TestDroppedConstraintsAreStillStated:
    @pytest.mark.parametrize("dialect", [OPENAI_STRICT, GEMINI])
    def test_a_dropped_bound_is_named_in_the_notes(self, dialect):
        """Not every keyword renders, but a bound that restricts anything must."""

        def check(seed):
            schema = _decorated(random.Random(seed))
            dropped = {
                key
                for key in _BOUNDS
                if key not in DIALECTS[dialect].keeps and _restricts(schema, key)
            }
            if dropped:
                assert constraint_notes(schema, dialect) is not None, sorted(dropped)

        _each(check)

    @pytest.mark.parametrize("dialect", [OPENAI_STRICT, GEMINI])
    def test_the_notes_stay_inside_their_budget(self, dialect):
        def check(seed):
            notes = constraint_notes(
                _decorated(random.Random(seed)), dialect, max_lines=4, max_chars=300
            )
            if notes is not None:
                assert len(notes) <= 300
                # The budget plus the header and the two summary lines.
                assert len(notes.splitlines()) <= 7

        _each(check)

    def test_a_dialect_that_drops_nothing_has_nothing_to_say(self):
        def check(seed):
            assert constraint_notes(_decorated(random.Random(seed)), JSON_SCHEMA) is None

        _each(check)
