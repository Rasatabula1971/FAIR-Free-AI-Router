import json


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
