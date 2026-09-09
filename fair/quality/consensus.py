"""Compare independently generated outputs; agreement never substitutes for validation."""

import json

from fair.quality.arithmetic import numeric_answer
from fair.quality.claims import canonical, comparable_claims
from fair.quality.json_data import strict_json


def independent(primary, candidate):
    _, primary_provider, primary_model = primary
    _, provider, model = candidate

    def norm(value):
        return value.strip().casefold()

    return (
        norm(provider.provider_id) != norm(primary_provider.provider_id)
        and norm(model.model_id) != norm(primary_model.model_id)
        and norm(model.independence_group or model.model_id)
        != norm(primary_model.independence_group or primary_model.model_id)
    )


def compare(request, first, second, both_validated):
    """Return (agreement or None, comparison scope), never raw answers or test outputs."""
    kind = request.validation.kind if request.validation is not None else None
    try:
        if kind == "arithmetic":
            return numeric_answer(first.text) == numeric_answer(second.text), "EXACT_VALUE"
        if kind == "grounded_claims":
            return (
                canonical(comparable_claims(first.text))
                == canonical(comparable_claims(second.text)),
                "EXACT_VALUE",
            )
        if kind in {"reference_json", "grounded_json"}:
            return (
                json.dumps(strict_json(first.text), sort_keys=True)
                == json.dumps(strict_json(second.text), sort_keys=True),
                "EXACT_VALUE",
            )
        if kind in {"python_function", "native_python_function"} and both_validated:
            # Both already passed the identical hidden host cases. Text may legitimately differ.
            return True, "HOST_TEST_CASES"
    except (ValueError, RecursionError):
        pass
    return None, "NOT_ASSESSED"
