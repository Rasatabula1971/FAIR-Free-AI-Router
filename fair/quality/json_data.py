import json
import re

# A single markdown code fence wrapping the whole answer. Free models add one
# even when told not to; the JSON inside is what the schema is about. Only a
# fence around the entire text is removed -- prose before or after it is
# still a malformed answer.
_FENCE = re.compile(r"^\s*```(?:[A-Za-z0-9_-]+)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def unfence(text):
    """The body of a whole-answer markdown code fence, else the text unchanged."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


def json_document(text):
    """strict_json over the answer, tolerating one whole-answer code fence."""
    return strict_json(unfence(text))


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
