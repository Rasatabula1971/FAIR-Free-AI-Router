import json
import re

# The opening of a markdown code fence: the backticks, an optional language tag,
# and the whitespace after it. Matched once from the start of the text, so it is
# linear. See _fenced for why the whole fence is not one pattern.
_FENCE_OPEN = re.compile(r"\s*```(?:[A-Za-z0-9_-]+)?\s*")

# How much text may surround the JSON before the answer stops being a wrapped
# document and becomes prose that happens to contain one. "Here is the JSON you
# asked for:" and "Let me know if you need anything else." are a few dozen
# characters each; a paragraph is where a model explains, hedges or works through
# alternatives, and that is not safe to throw away.
MAX_WRAPPER_CHARS = 300

_BRACKETS = frozenset("{}[]")


def _fenced(text):
    """The body of a code fence wrapping the whole answer, else None.

    Free models add a fence even when told not to; the JSON inside is what the
    schema is about. This used to be a single pattern,
    ``^\\s*```tag?\\s*\\n?(.*?)\\n?\\s*```\\s*$``, whose three whitespace runs
    competed for the same characters. An answer that opened a fence and then ran
    on in newlines took cubic time to refuse: 1,600 newlines held the event loop
    for seven seconds and 3,000 for most of a minute, with no deadline over it,
    because the quality gate runs after the attempt's own timeout has passed.
    Reading the fence from both ends gives the same body in linear time.
    """
    opening = _FENCE_OPEN.match(text)
    end = len(text.rstrip())
    if opening is None or end - 3 < opening.end() or not text.startswith("```", end - 3):
        return None
    return text[opening.end() : end - 3].rstrip()


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

    Nor is a value inside a larger structure that lost its outer braces:
    ``"status": "error", "data": {...}`` is a broken object, and lifting ``data``
    out of it would be choosing a part and calling it the answer. A quoted key
    and colon straight before the span, or a comma and another quoted key
    straight after it, mark the span as a member rather than a document.
    """
    opening = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    closing = max(text.rfind("}"), text.rfind("]"))
    if opening < 0 or closing < opening:
        raise ValueError("No JSON document")
    before, after = text[:opening], text[closing + 1 :]
    wrapper = before + after
    if not _BRACKETS.isdisjoint(wrapper):
        raise ValueError("Brackets outside the JSON document")
    lead, trail = before.rstrip(), after.lstrip()
    if (lead.endswith(":") and lead[:-1].rstrip().endswith('"')) or (
        trail.startswith(",") and trail[1:].lstrip().startswith('"')
    ):
        raise ValueError("The JSON is a member of a larger structure")
    if len("".join(wrapper.split())) > MAX_WRAPPER_CHARS:
        raise ValueError("Too much text around the JSON document")
    return text[opening : closing + 1]


def read_json(text):
    """(value, the JSON as the model wrote it, what was removed to reach it).

    The last is None for a bare answer, "FENCE" for a whole-answer code fence and
    "PROSE" for a document lifted out of a short wrapper. The strict readings are
    tried first, so an answer that was accepted before is read exactly as before.
    """
    fence = _fenced(text)
    body = text if fence is None else fence
    try:
        return strict_json(body), body, None if fence is None else "FENCE"
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
