import hashlib
import json

from jsonschema import Draft202012Validator, ValidationError

from fair.quality.arithmetic import calculate, numeric_answer
from fair.quality.code_validator import validate_function
from fair.quality.grounding import grounded_result
from fair.quality.json_data import strict_json
from fair.schemas.domain import QualityReport


def evaluate(request, profile, response) -> QualityReport:
    reasons = []
    checks = {}
    verification = "UNVERIFIED"
    score = None
    if not response.text.strip():
        reasons.append("EMPTY_RESPONSE")
    if response.finish_reason != "stop":
        reasons.append("INCOMPLETE_RESPONSE")
    if request.expected_schema is not None:
        try:
            data = strict_json(response.text)
            Draft202012Validator(request.expected_schema).validate(data)
            verification = "STRUCTURE_VALIDATED"
            checks["schema"] = "PASS"
        except (ValueError, ValidationError, RecursionError):
            reasons.append("SCHEMA_FAILURE")
            checks["schema"] = "FAIL"

    evidence = {source.source_id: source.text for source in request.evidence}
    for citation in response.citations:
        if citation.source_id not in evidence:
            reasons.append("FABRICATED_CITATION")
        elif citation.quote not in evidence[citation.source_id]:
            reasons.append("UNSUPPORTED_CITATION")
    if response.citations:
        checks["citations"] = (
            "FAIL" if any("CITATION" in r for r in reasons) else "QUOTE_MATCH_ONLY"
        )
    assertions = {}
    for claim in response.assertions:
        key = (claim.subject.strip().casefold(), claim.predicate.strip().casefold())
        if key in assertions and assertions[key] != claim.value.strip().casefold():
            reasons.append("MATERIAL_CONTRADICTION")
        assertions[key] = claim.value.strip().casefold()
    kind = request.validation.kind if request.validation is not None else None
    if profile.requires_grounding and not response.citations and kind != "grounded_json":
        reasons.append("GROUNDING_REQUIRED")

    if request.validation is not None:
        if request.validation.kind == "arithmetic":
            try:
                matched = numeric_answer(response.text) == calculate(request.validation.expression)
            except ValueError:
                matched = False
            checks["arithmetic"] = "PASS" if matched else "FAIL"
            verification = "DETERMINISTIC_ARITHMETIC" if matched else "UNVERIFIED"
            if not matched:
                reasons.append("ARITHMETIC_MISMATCH")
        elif kind == "reference_json":
            try:
                actual = json.dumps(strict_json(response.text), sort_keys=True, allow_nan=False)
                expected = json.dumps(request.validation.expected, sort_keys=True, allow_nan=False)
                matched = actual == expected
            except (ValueError, RecursionError):
                matched = False
            checks["reference_json"] = "PASS" if matched else "FAIL"
            verification = "HOST_REFERENCE_MATCH" if matched else "UNVERIFIED"
            if not matched:
                reasons.append("REFERENCE_MISMATCH")
        elif kind == "grounded_json":
            expected = grounded_result(request.validation, request.evidence)
            try:
                actual = strict_json(response.text)
                matched = json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
            except (ValueError, RecursionError):
                matched = False
            checks["grounded_json"] = "PASS" if matched else "FAIL"
            verification = "SOURCE_DATA_MATCH" if matched else "UNVERIFIED"
            if not matched:
                reasons.append("GROUNDED_DATA_MISMATCH")
        else:
            matched, failure = validate_function(response.text, request.validation)
            checks["python_function"] = "PASS" if matched else "FAIL"
            checks["code_test_count"] = str(len(request.validation.cases))
            verification = "BOUNDED_CODE_TESTS" if matched else "UNVERIFIED"
            if not matched:
                reasons.append(failure)
        # 100 means all deterministic contract checks passed, not a truth probability.
        score = 100.0 if matched else 0.0

    unsupported = (
        (profile.requires_grounding and kind != "grounded_json")
        or request.freshness_required
        or request.quality_level == "high_impact_support"
        or bool(
            profile.required_capabilities
            - (
                {"structured_output", "coding"}
                if kind == "python_function"
                else {"structured_output"}
            )
        )
    )
    if unsupported:
        checks["coverage"] = "TASK_VALIDATOR_UNAVAILABLE"
        verification = "UNVERIFIED"
        if not reasons:
            score = None
    if reasons:
        verification = "UNVERIFIED"
        score = 0.0
    return QualityReport(
        overall_score=score,
        hard_reject=bool(reasons),
        reject_reasons=list(dict.fromkeys(reasons)),
        verification_state=verification,
        validator_results=checks,
        validation_fingerprint=hashlib.sha256(
            json.dumps(
                {
                    "validation": request.validation.model_dump(mode="json")
                    if request.validation
                    else None,
                    "schema": request.expected_schema,
                    "evidence": [source.model_dump() for source in request.evidence],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    )


def acceptable(report, profile):
    return (
        report is not None
        and not report.hard_reject
        and report.overall_score is not None
        and report.overall_score >= profile.minimum_quality_score
        and report.verification_state
        in {
            "DETERMINISTIC_ARITHMETIC",
            "HOST_REFERENCE_MATCH",
            "SOURCE_DATA_MATCH",
            "BOUNDED_CODE_TESTS",
        }
    )
