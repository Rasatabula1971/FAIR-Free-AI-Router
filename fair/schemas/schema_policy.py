"""Admission policy for caller-supplied JSON Schemas.

jsonschema evaluates ``pattern`` and ``patternProperties`` with Python's ``re``,
which has no time budget. A pathological expression such as ``^(a+)+$`` blocks
the thread that runs it, and FAIR validates provider output on the event loop
shared by every client. Until validation can run in a killable worker, schemas
that would execute a caller-chosen regular expression are refused at admission.
"""

from collections.abc import Mapping

REGEX_KEYWORDS = frozenset({"pattern", "patternProperties"})

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


def find_regex_keyword(schema):
    """Return the regex-bearing keyword used by ``schema``, or None.

    Walks real schema nodes only, so ``enum``/``const``/``default`` data and
    property *names* are never mistaken for keywords. Iterative, so a deeply
    nested schema cannot exhaust the stack.
    """
    stack = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, Mapping):
            continue
        for keyword in REGEX_KEYWORDS:
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
