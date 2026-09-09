"""Exact structured claim grounding across supplied facts; no prose entailment or retrieval."""

import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, ValidationError, field_validator

from fair.quality.contracts import FactKey
from fair.quality.json_data import strict_json
from fair.schemas.domain import DTO, ClaimCheck


def scalar(value):
    if type(value) not in {str, int, float, bool, type(None)}:
        raise ValueError("Fact values must be JSON scalars")
    if len(json.dumps(value, allow_nan=False)) > 1024:
        raise ValueError("Fact value exceeds 1024 characters")
    return value


def canonical(value):
    # Preserve strict JSON types: True, 1 and 1.0 do not stand for the same fact.
    return json.dumps(value, allow_nan=False, sort_keys=True)


class Fact(FactKey):
    value: JsonValue

    @field_validator("value")
    @classmethod
    def scalar_value(cls, value):
        return scalar(value)


class FactDocument(DTO):
    facts: list[Fact] = Field(max_length=50)


class FactReference(DTO):
    source_id: str = Field(min_length=1, max_length=128)
    pointer: str = Field(pattern=r"^/facts/(0|[1-9][0-9]?)$")

    def key(self):
        return self.source_id, self.pointer


class AnsweredClaim(DTO):
    claim_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    status: Literal["answered"]
    value: JsonValue
    sources: list[FactReference] = Field(min_length=1, max_length=200)

    @field_validator("value")
    @classmethod
    def scalar_value(cls, value):
        return scalar(value)


class AbstainedClaim(DTO):
    claim_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    status: Literal["abstained"]


class ClaimResponse(DTO):
    claims: list[Annotated[AnsweredClaim | AbstainedClaim, Field(discriminator="status")]] = Field(
        max_length=20
    )


def fact_index(evidence):
    """Validate all supplied sources, including unreferenced ones, before any provider call."""
    index = {}
    count = 0
    for source in evidence:
        try:
            document = FactDocument.model_validate(strict_json(source.text))
        except (ValueError, RecursionError) as error:
            raise ValueError("Claim evidence must contain bounded structured facts") from error
        count += len(document.facts)
        if count > 200:
            raise ValueError("Claim evidence exceeds 200 facts")
        for offset, fact in enumerate(document.facts):
            index.setdefault(fact.key(), []).append(
                (canonical(fact.value), (source.source_id, f"/facts/{offset}"))
            )
    return index


def parse_claim_response(text):
    if len(text) > 100000:
        raise ValueError("Claim response exceeds budget")
    response = ClaimResponse.model_validate(strict_json(text))
    ids = [claim.claim_id for claim in response.claims]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate response claims")
    return {claim.claim_id: claim for claim in response.claims}


def validate_claims(text, contract, evidence):
    index = fact_index(evidence)
    try:
        answers = parse_claim_response(text)
    except (ValueError, ValidationError, RecursionError):
        return False, ["CLAIM_FORMAT_FAILURE"], []
    reasons, reports = [], []
    if set(answers) - {claim.claim_id for claim in contract.claims}:
        reasons.append("UNREQUESTED_CLAIM")
    for target in contract.claims:
        records = index.get(target.key(), [])
        values = {value for value, _ in records}
        answer = answers.get(target.claim_id)
        if answer is None:
            status = "MISSING"
            reasons.append("MISSING_CLAIM")
        elif answer.status == "abstained":
            status = (
                "INSUFFICIENT_EVIDENCE"
                if not records
                else "CONFLICTING_EVIDENCE"
                if len(values) > 1
                else "ABSTAINED"
            )
        elif not records:
            status = "UNSUPPORTED"
            reasons.append("UNSUPPORTED_CLAIM")
        elif len(values) > 1:
            status = "CONFLICTING_EVIDENCE"
            # An unqualified answer must not choose one side of conflicting evidence.
            reasons.append("CLAIM_OVER_CONFLICTING_EVIDENCE")
        elif canonical(answer.value) not in values:
            status = "CONTRADICTED"
            reasons.append("CONTRADICTED_CLAIM")
        else:
            cited = [reference.key() for reference in answer.sources]
            required = {reference for _, reference in records}
            if len(cited) != len(set(cited)) or set(cited) != required:
                status = "INVALID_PROVENANCE"
                reasons.append("CLAIM_PROVENANCE_FAILURE")
            else:
                status = "SUPPORTED"
        reports.append(
            ClaimCheck(
                claim_id=target.claim_id,
                status=status,
                evidence_count=len(records),
            )
        )
    if reasons:
        return False, list(dict.fromkeys(reasons)), reports
    if all(report.status == "SUPPORTED" for report in reports):
        return True, [], reports
    # Explicit abstention is incomplete coverage, not a fabricated answer or a passing result.
    return None, [], reports


def comparable_claims(text):
    """Canonical semantic output, ignoring only claim and citation ordering."""
    answers = parse_claim_response(text)
    return {
        key: {
            **claim.model_dump(mode="json"),
            **(
                {"sources": sorted(ref.key() for ref in claim.sources)}
                if claim.status == "answered"
                else {}
            ),
        }
        for key, claim in answers.items()
    }
