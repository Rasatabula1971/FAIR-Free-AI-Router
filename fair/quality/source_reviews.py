"""Operator-reviewed content snapshots. This policy does not establish real-world truth."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, Field, model_validator

from fair.quality.claims import fact_index
from fair.schemas.domain import DTO, SourceCheck, SourcePolicyReport


class SourceReview(DTO):
    review_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    client_ids: set[str] = Field(min_length=1, max_length=100)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    origin_group: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    source_class: Literal["PRIMARY", "SECONDARY"]
    decision: Literal["APPROVED", "REJECTED"]
    reviewer_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    source_locator: str = Field(min_length=1, max_length=2048)
    review_note: str = Field(min_length=1, max_length=2000)
    observed_at: AwareDatetime
    reviewed_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def chronology(self):
        if not self.observed_at <= self.reviewed_at < self.expires_at:
            raise ValueError("Require observed_at <= reviewed_at < expires_at")
        if any(not identity.strip() or len(identity) > 128 for identity in self.client_ids):
            raise ValueError("Invalid authorized client identity")
        if not self.source_locator.strip() or not self.review_note.strip():
            raise ValueError("Source locator and review rationale are required")
        return self


class ReviewFile(DTO):
    reviews: list[SourceReview] = Field(default_factory=list, max_length=10000)


class SourcePolicyBlocked(Exception):
    pass


class SourceReviewUnavailable(RuntimeError):
    pass


def independent_count(reviews):
    # Maximum matching of origin groups and exact content hashes: aliases or duplicate
    # snapshots cannot independently satisfy the corroboration requirement twice.
    graph = {}
    for review in reviews:
        graph.setdefault(review.origin_group, set()).add(review.content_sha256)
    assigned = {}

    def augment(origin, seen):
        for digest in sorted(graph[origin]):
            if digest in seen:
                continue
            seen.add(digest)
            if digest not in assigned or augment(assigned[digest], seen):
                assigned[digest] = origin
                return True
        return False

    return sum(augment(origin, set()) for origin in sorted(graph))


class SourceReviewRegistry:
    def __init__(self, reviews=(), clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.reviews = {}
        for review in reviews:
            record = SourceReview.model_validate(review).model_copy(deep=True)
            if record.review_id in self.reviews:
                raise ValueError("Duplicate source review ID")
            self.reviews[record.review_id] = record

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        if path.stat().st_size > 5_000_000:
            raise ValueError("Source review file exceeds budget")
        data = ReviewFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        return cls(data.reviews)

    def evaluate(self, request, server_policy=None):
        kind = request.validation.kind if request.validation is not None else None
        if kind not in {"grounded_json", "grounded_claims"}:
            return SourcePolicyReport()
        policies = [
            policy for policy in (server_policy, request.source_policy) if policy is not None
        ]
        if not policies:
            return SourcePolicyReport()
        allowed = set.intersection(*(policy.allowed_source_classes for policy in policies))
        policy_data = {
            "allowed_source_classes": sorted(allowed),
            "max_age_seconds": min(policy.max_age_seconds for policy in policies),
            "min_independent_origins": max(policy.min_independent_origins for policy in policies),
        }
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Source review clock must be timezone-aware")
        checks, valid, snapshots = [], {}, []
        for source in request.evidence:
            review = self.reviews.get(source.review_id)
            # Do not reveal whether a review exists for another client's private content.
            if review is None or request.client_id not in review.client_ids:
                status = "REVIEW_UNAVAILABLE"
            else:
                snapshot = review.model_dump(mode="json")
                snapshot["client_ids"] = sorted(review.client_ids)
                snapshots.append(snapshot)
                if hashlib.sha256(source.text.encode("utf-8")).hexdigest() != review.content_sha256:
                    status = "CONTENT_MISMATCH"
                elif review.decision != "APPROVED":
                    status = "REJECTED"
                elif now < review.reviewed_at:
                    status = "FUTURE_REVIEW"
                elif now >= review.expires_at:
                    status = "EXPIRED"
                elif (now - review.observed_at).total_seconds() > policy_data["max_age_seconds"]:
                    status = "STALE"
                elif review.source_class not in allowed:
                    status = "SOURCE_CLASS_DISALLOWED"
                else:
                    status = "REVIEWED"
                    valid[source.source_id] = review
            checks.append(SourceCheck(source_id=source.source_id, status=status))
        reasons = []
        if not allowed:
            reasons.append("SOURCE_POLICY_CONFLICT")
        if not checks:
            reasons.append("SOURCE_EVIDENCE_REQUIRED")
        if any(check.status != "REVIEWED" for check in checks):
            reasons.append("SOURCE_REVIEW_REQUIREMENTS_NOT_MET")
        if kind == "grounded_claims":
            index = fact_index(request.evidence)
            targets = {
                claim.claim_id: {ref[0] for _, ref in index.get(claim.key(), [])}
                for claim in request.validation.claims
            }
        else:
            targets = {field.output_key: {field.source_id} for field in request.validation.fields}
        counts = {
            key: independent_count([valid[source_id] for source_id in ids if source_id in valid])
            for key, ids in targets.items()
        }
        if any(count < policy_data["min_independent_origins"] for count in counts.values()):
            reasons.append("SOURCE_CORROBORATION_REQUIRED")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "policy": policy_data,
                    "reviews": sorted(snapshots, key=lambda row: row["review_id"]),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return SourcePolicyReport(
            state="BLOCKED" if reasons else "PASSED",
            checked_at=now,
            checks=checks,
            reasons=reasons,
            independent_origins=counts,
            policy_fingerprint=fingerprint,
        )
