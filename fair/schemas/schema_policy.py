"""Admission policy for caller-supplied JSON Schemas.

FAIR validates provider output on the event loop shared by every client, so a
schema that takes seconds to evaluate takes the service down for everyone while
it runs. The admission rules exist to bound what a caller can make that cost,
and they work by bounding what can be *expressed*: a schema is refused unless
its evaluation cost is bounded by its own size.

Two keywords escape that. ``pattern`` and ``patternProperties`` hand a
caller-chosen regular expression to Python's ``re``, which has no time budget,
so ``^(a+)+$`` in a few bytes blocks the thread that runs it. And
``unevaluatedProperties``/``unevaluatedItems`` collect annotations, which makes
the validator re-evaluate every subschema beneath them: nested inside
combinators that is exponential in depth while the schema text grows linearly.
A 677-byte schema took 10.4 seconds, and 12,497 bytes took 2.3 -- both well
inside the 20,000-character cap that bounds everything else.

``$ref`` and ``$dynamicRef`` are refused elsewhere (fair.schemas.api) for the
same reason: recursion buys unbounded evaluation from bounded text.

This is a bound on expressible cost, not a proof of safety. Five other
constructions were tried -- nested ``if``/``then``/``else``, nested ``not``,
``contains`` over combinators, ``dependentSchemas`` chains, ``propertyNames``
over combinators -- and every one needed exponentially more *text* to cost
exponentially more time, so the size cap already stops them. A keyword that
re-evaluates without repeating text would defeat this, and only running
validation in a bounded killable worker would make it a proof.
"""

from collections.abc import Mapping

REGEX_KEYWORDS = frozenset({"pattern", "patternProperties"})
# Annotation-collecting keywords: their cost is not bounded by the schema's size.
REEVALUATING_KEYWORDS = frozenset({"unevaluatedProperties", "unevaluatedItems"})
UNBOUNDED_KEYWORDS = REGEX_KEYWORDS | REEVALUATING_KEYWORDS

# Keywords whose value is a single subschema.
_SUBSCHEMA = (
    "additionalProperties",
    "additionalItems",
    "items",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contentSchema",
)
# Keywords whose value is a list of subschemas.
_SUBSCHEMA_LIST = ("allOf", "anyOf", "oneOf", "prefixItems")
# Keywords whose value maps arbitrary names to subschemas. The names are data:
# a property called "pattern" is not the ``pattern`` keyword.
_SUBSCHEMA_MAP = ("properties", "$defs", "definitions", "dependentSchemas", "dependencies")


def find_unbounded_keyword(schema):
    """Return a keyword whose cost the schema's size does not bound, or None.

    Walks real schema nodes only, so ``enum``/``const``/``default`` data and
    property *names* are never mistaken for keywords -- a property called
    "pattern" is data. Iterative, so a deeply nested schema cannot exhaust the
    stack: the check itself has to be bounded to be worth anything.
    """
    stack = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, Mapping):
            continue
        for keyword in UNBOUNDED_KEYWORDS:
            if keyword in node:
                return keyword
        for keyword in _SUBSCHEMA:
            if keyword in node:
                stack.append(node[keyword])
        for keyword in _SUBSCHEMA_LIST:
            value = node.get(keyword)
            if isinstance(value, list):
                stack.extend(value)
        for keyword in _SUBSCHEMA_MAP:
            value = node.get(keyword)
            if isinstance(value, Mapping):
                stack.extend(value.values())
        # Draft 2019-09 tuple form and dependency lists hold schemas in lists.
        if isinstance(node.get("items"), list):
            stack.extend(node["items"])
    return None
