import json

from jsonschema import Draft202012Validator, ValidationError

from fair.schemas.domain import QualityReport


def evaluate(request, response) -> QualityReport:
    reasons = []
    verification = "UNVERIFIED"
    if not response.text.strip():
        reasons.append("EMPTY_RESPONSE")
    if response.finish_reason != "stop":
        reasons.append("INCOMPLETE_RESPONSE")
    if request.expected_schema is not None:
        try:
            data = json.loads(response.text)
            Draft202012Validator(request.expected_schema).validate(data)
            verification = "STRUCTURE_VALIDATED"
        except (ValueError, ValidationError):
            reasons.append("SCHEMA_FAILURE")
    # Structure and non-empty text do not establish truth or instruction adherence.
    # Until task-specific validators exist, never invent a quality score or accept.
    return QualityReport(
        hard_reject=bool(reasons), reject_reasons=reasons, verification_state=verification
    )
