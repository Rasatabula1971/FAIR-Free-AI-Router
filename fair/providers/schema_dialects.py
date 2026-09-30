"""Structured-output dialects: what each provider's API accepts inside a schema.

FAIR takes a full JSON Schema 2020-12 document from the caller and validates every
response against that original document locally (``fair.quality.engine.evaluate``).
The copy a provider receives only steers generation, so a keyword the provider's
structured-output implementation cannot parse may be dropped in transit without
weakening any guarantee: the constraint is still enforced when the answer comes
back, and a response that breaks it is still SCHEMA_FAILURE.

Transport therefore carries shape -- types, properties, required, items, enums --
and nothing else. That is narrower than any one provider accepts, deliberately. A
common subset is correct without a per-provider keyword list, and a keyword list
copied from documentation is exactly the kind of unreviewed local value the rest of
this package refuses to keep.

What is dropped is not lost twice over: ``constraint_notes`` renders the dropped
constraints as text the adapter appends to the prompt, so the model is still told
"1 to 5 items" when the field carrying that is gone.

Nothing here invents structure. A schema its author must repair -- an object with
no ``additionalProperties`` under OpenAI strict mode, a ``required`` list that omits
a declared property, a combinator no dialect accepts -- is reported by
``fair.tools.schema_compat`` and never silently rewritten, because either repair
would change what the schema means.
"""

import copy
from dataclasses import dataclass

OPENAI_STRICT = "openai_strict"
GEMINI = "gemini"
JSON_SCHEMA = "json_schema"

# Keywords that describe shape. Everything outside this set is a constraint or an
# annotation: dropped for transport, restated in the prompt, still enforced locally.
_SHAPE = frozenset(
    {"type", "properties", "required", "items", "enum", "description", "anyOf", "$ref", "$defs"}
)


@dataclass(frozen=True)
class Dialect:
    """``keeps`` is None for a dialect that takes JSON Schema unchanged."""

    name: str
    keeps: frozenset[str] | None
    # OpenAI strict mode and Gemini both reject 'const'; a single-value 'enum' says
    # the same thing and is accepted by every dialect here.
    const_as_enum: bool = True
    # False where the dialect rejects the keyword outright. OpenAI strict mode is the
    # opposite case: it *requires* additionalProperties:false on every object, so the
    # keyword is carried and a schema that omits it is an author fix, not a transform.
    keeps_additional_properties: bool = True


DIALECTS = {
    OPENAI_STRICT: Dialect(OPENAI_STRICT, _SHAPE | {"additionalProperties"}),
    GEMINI: Dialect(GEMINI, _SHAPE, keeps_additional_properties=False),
    JSON_SCHEMA: Dialect(JSON_SCHEMA, None),
}

_CONST_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"))


def _const_type(value):
    # bool before int: bool is a subclass of int and "integer" would be wrong.
    for python_type, name in _CONST_TYPES:
        if type(value) is python_type:
            return name
    return None


def transport_schema(schema, dialect):
    """Return the copy of ``schema`` to send to a provider speaking ``dialect``."""
    spec = DIALECTS[dialect]
    if spec.keeps is None or not isinstance(schema, dict):
        return copy.deepcopy(schema)
    return _convert(schema, spec)


def _convert(node, spec):
    if isinstance(node, list):
        return [_convert(item, spec) for item in node]
    if not isinstance(node, dict):
        return copy.deepcopy(node)
    out: dict = {}
    if spec.const_as_enum and "const" in node and "enum" not in node:
        out["enum"] = [copy.deepcopy(node["const"])]
        if "type" not in node:
            inferred = _const_type(node["const"])
            if inferred is not None:
                out["type"] = inferred
    for key, value in node.items():
        if key == "const":
            continue
        if key == "additionalProperties" and not spec.keeps_additional_properties:
            continue
        if key not in spec.keeps:
            continue
        if key in {"properties", "$defs"} and isinstance(value, dict):
            out[key] = {name: _convert(item, spec) for name, item in value.items()}
        elif key in {"items", "anyOf"}:
            out[key] = _convert(value, spec)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _number(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _describe(node):
    """One phrase for the constraints ``node`` carries, or None."""
    low, high = node.get("minItems"), node.get("maxItems")
    parts = []
    if high == 0:
        parts.append("must be an empty array")
    elif low is not None and high is not None:
        parts.append(f"{low} to {high} items")
    elif low is not None:
        parts.append(f"at least {low} items")
    elif high is not None:
        parts.append(f"at most {high} items")
    if node.get("uniqueItems") is True:
        parts.append("items must be unique")
    low, high = node.get("minLength"), node.get("maxLength")
    if low is not None and low > 0 and high is not None:
        parts.append(f"{low} to {high} characters")
    elif low == 1:
        parts.append("must not be empty")
    elif low is not None and low > 1:
        parts.append(f"at least {low} characters")
    elif high is not None:
        parts.append(f"at most {high} characters")
    if node.get("pattern") is not None:
        parts.append(f"must match {node['pattern']}")
    if node.get("format") is not None:
        parts.append(f"{node['format']} format")
    low, high = node.get("minimum"), node.get("maximum")
    if low is not None and high is not None:
        parts.append(f"between {_number(low)} and {_number(high)}")
    elif low is not None:
        parts.append(f"at least {_number(low)}")
    elif high is not None:
        parts.append(f"at most {_number(high)}")
    return ", ".join(parts) or None


_HEADER = "Constraints the response must satisfy that this schema format cannot express:"


def constraint_notes(schema, dialect, *, max_lines=18, max_chars=1400):
    """Prompt text restating the constraints ``transport_schema`` drops, or None."""
    spec = DIALECTS[dialect]
    if spec.keeps is None or not isinstance(schema, dict):
        return None
    found: list[tuple[str, str]] = []
    _collect(schema, spec, "result", found)
    if not found:
        return None
    # minLength:1 is the single most repeated constraint in real schemas; one counted
    # line for it keeps the specific limits visible instead of scrolling past them.
    empties = [path for path, text in found if text == "must not be empty"]
    lines = [f"- {path}: {text}" for path, text in found if text != "must not be empty"]
    shown, hidden = lines[:max_lines], len(lines) - min(len(lines), max_lines)
    if empties:
        shown.append(f"- every other string field, {len(empties)} in all, must not be empty")
    if hidden:
        shown.append(f"- and {hidden} further field constraints declared by the schema")
    note = "\n".join([_HEADER, *shown])
    return note if len(note) <= max_chars else note[:max_chars].rsplit("\n", 1)[0]


def _collect(node, spec, path, found):
    if isinstance(node, list):
        for item in node:
            _collect(item, spec, path, found)
        return
    if not isinstance(node, dict):
        return
    dropped = {
        key: value
        for key, value in node.items()
        if key not in spec.keeps and key != "const" and value is not None
    }
    text = _describe(dropped)
    if text is not None:
        found.append((path, text))
    for name, item in (node.get("properties") or {}).items():
        _collect(item, spec, f"{path}.{name}", found)
    if "items" in node:
        _collect(node["items"], spec, f"{path}[]", found)
    for key in ("anyOf", "$defs"):
        value = node.get(key)
        if isinstance(value, dict):
            for name, item in value.items():
                _collect(item, spec, f"{path}.{name}", found)
        elif isinstance(value, list):
            _collect(value, spec, path, found)
