import json
import re

# A single markdown code fence wrapping the whole answer. Free models add one
# even when told not to; the JSON inside is what the schema is about.
_FENCE = re.compile(r"^\s*```(?:[A-Za-z0-9_-]+)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

# How much text may surround the JSON before the answer stops being a wrapped
# document and becomes prose that happens to contain one. "Here is the JSON you
# asked for:" and "Let me know if you need anything else." are a few dozen
# characters each; a paragraph is where a model explains, hedges or works through
# alternatives, and that is not safe to throw away.
MAX_WRAPPER_CHARS = 300

_BRACKETS = frozenset("{}[]")


def _unwrap(text):
    """The one JSON object or array in ``text``, when only a short wrapper surrounds it.

    Everything from the first opening bracket to the last closing one has to be a
    single document, and nothing outside it may contain a bracket at all. That is
    what makes "exactly one" hold without choosing: two documents, an example
    ahead of the answer, or prose that itself uses brackets all leave a bracket
    outside or a span that does not parse, and are refused as they always were.
    Nothing is repaired and nothing is guessed -- the span is parsed as strictly
    as a bare answer is.

    Only an object or an array is ever lifted out. A number or a word standing in
    a sentence is not an answer someone wrapped.
    """
    opening = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    closing = max(text.rfind("}"), text.rfind("]"))
    if opening < 0 or closing < opening:
        raise ValueError("No JSON document")
    wrapper = text[:opening] + text[closing + 1 :]
    if not _BRACKETS.isdisjoint(wrapper):
        raise ValueError("Brackets outside the JSON document")
    if len("".join(wrapper.split())) > MAX_WRAPPER_CHARS:
        raise ValueError("Too much text around the JSON document")
    return text[opening : closing + 1]


def read_json(text):
    """(value, the JSON as the model wrote it, what was removed to reach it).

    The last is None for a bare answer, "FENCE" for a whole-answer code fence and
    "PROSE" for a document lifted out of a short wrapper. The strict readings are
    tried first, so an answer that was accepted before is read exactly as before.
    """
    fence = _FENCE.match(text)
    body = fence.group(1) if fence else text
    try:
        return strict_json(body), body, "FENCE" if fence else None
    except ValueError:
        document = _unwrap(text)
        return strict_json(document), document, "PROSE"


def json_document(text):
    """The answer's JSON value; see read_json for what is tolerated around it."""
    return read_json(text)[0]


def json_text(text):
    """The answer's JSON as text, without whatever the model wrapped it in."""
    return read_json(text)[1]


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("Non-finite JSON number")

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    json.dumps(value, allow_nan=False)
    return value
