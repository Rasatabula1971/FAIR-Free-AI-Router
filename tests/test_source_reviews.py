"""Source-review policy tests — the reviewed-snapshot gate on grounded answers.

This module had no coverage at all. It decides whether evidence handed to a
grounded contract has been reviewed by an operator, so every branch below is a
way an unreviewed, stale, rejected or uncorroborated source could otherwise be
treated as good.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from fair.quality.contracts import SourcePolicy
from fair.quality.source_reviews import (
    SourceReview,
    SourceReviewRegistry,
    independent_count,
)
from fair.schemas.api import SolveRequest

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review(review_id="r1", text='{"answer": "x"}', **overrides):
    values = dict(
        review_id=review_id,
        client_ids={"c"},
        content_sha256=_digest(text),
        origin_group="group_a",
        source_class="PRIMARY",
        decision="APPROVED",
        reviewer_id="alice",
        source_locator="https://example.invalid/doc",
        review_note="checked against the published filing",
        observed_at=NOW - timedelta(hours=1),
        reviewed_at=NOW - timedelta(minutes=30),
        expires_at=NOW + timedelta(days=7),
    )
    return values | overrides


def _request(sources=(("s1", "r1", '{"answer": "x"}'),), policy=None, client_id="c"):
    payload = {
        "client_id": client_id,
        "task": "extract the answer",
        "validation": {
            "kind": "grounded_json",
            "fields": [
                {"output_key": f"key{index}", "source_id": source_id, "pointer": "/answer"}
                for index, (source_id, _, _) in enumerate(sources)
            ],
        },
        "evidence": [
            {"source_id": source_id, "review_id": review_id, "text": text}
            for source_id, review_id, text in sources
        ],
    }
    if policy is not None:
        payload["source_policy"] = policy
    return SolveRequest.model_validate(payload)


def _registry(*reviews):
    return SourceReviewRegistry(reviews, clock=lambda: NOW)


# ── When the policy does not apply ───────────────────────────────────────


class TestPolicyNotRequested:
    def test_no_policy_on_the_request_is_not_requested(self):
        report = _registry(_review()).evaluate(_request())
        assert report.state == "NOT_REQUESTED"
        assert report.checks == []

    def test_an_ungrounded_contract_is_not_reviewed(self):
        request = SolveRequest.model_validate(
            {
                "client_id": "c",
                "task": "15*23",
                "validation": {"kind": "arithmetic", "expression": "15*23"},
            }
        )
        assert _registry(_review()).evaluate(request).state == "NOT_REQUESTED"

    def test_a_server_policy_applies_without_a_request_policy(self):
        report = _registry(_review()).evaluate(
            _request(), server_policy=SourcePolicy(min_independent_origins=1)
        )
        assert report.state == "PASSED"


# ── The accepting path ──────────────────────────────────────────────────


class TestApprovedSource:
    def test_a_fresh_approved_matching_review_passes(self):
        report = _registry(_review()).evaluate(_request(policy={"min_independent_origins": 1}))
        assert report.state == "PASSED"
        assert [check.status for check in report.checks] == ["REVIEWED"]
        assert report.reasons == []
        assert report.checked_at == NOW
        assert report.policy_fingerprint is not None

    def test_the_fingerprint_covers_the_policy(self):
        registry = _registry(_review())
        loose = registry.evaluate(_request(policy={"min_independent_origins": 1}))
        strict = registry.evaluate(
            _request(policy={"min_independent_origins": 1, "max_age_seconds": 7200})
        )
        assert loose.policy_fingerprint != strict.policy_fingerprint


# ── Every way a source fails review ─────────────────────────────────────


class TestRejectedSources:
    def _status(self, review_overrides=None, policy=None, client_id="c", reviews=None):
        policy = policy or {"min_independent_origins": 1}
        if reviews is None:
            reviews = [_review(**(review_overrides or {}))]
        report = _registry(*reviews).evaluate(_request(policy=policy, client_id=client_id))
        assert report.state == "BLOCKED"
        assert "SOURCE_REVIEW_REQUIREMENTS_NOT_MET" in report.reasons
        return report.checks[0].status

    def test_evidence_that_does_not_match_the_reviewed_snapshot(self):
        assert self._status({"content_sha256": _digest("something else")}) == "CONTENT_MISMATCH"

    def test_a_review_the_operator_rejected(self):
        assert self._status({"decision": "REJECTED"}) == "REJECTED"

    def test_a_review_that_has_expired(self):
        assert self._status({"expires_at": NOW - timedelta(seconds=1)}) == "EXPIRED"

    def test_a_review_dated_in_the_future(self):
        future = {
            "observed_at": NOW + timedelta(hours=1),
            "reviewed_at": NOW + timedelta(hours=2),
            "expires_at": NOW + timedelta(days=1),
        }
        assert self._status(future) == "FUTURE_REVIEW"

    def test_content_observed_longer_ago_than_the_policy_allows(self):
        assert (
            self._status({}, policy={"min_independent_origins": 1, "max_age_seconds": 60})
            == "STALE"
        )

    def test_a_source_class_the_policy_excludes(self):
        policy = {"min_independent_origins": 1, "allowed_source_classes": ["PRIMARY"]}
        assert (
            self._status({"source_class": "SECONDARY"}, policy=policy) == "SOURCE_CLASS_DISALLOWED"
        )

    def test_no_review_at_all(self):
        assert self._status(reviews=[]) == "REVIEW_UNAVAILABLE"

    def test_a_review_belonging_to_another_client_is_indistinguishable_from_none(self):
        """Whether another tenant has reviewed some content is not disclosed."""
        assert self._status({"client_ids": {"other"}}) == "REVIEW_UNAVAILABLE"


class TestPolicyCombination:
    def test_two_policies_take_the_stricter_of_each_bound(self):
        report = _registry(_review()).evaluate(
            _request(policy={"max_age_seconds": 86400, "min_independent_origins": 1}),
            server_policy=SourcePolicy(max_age_seconds=60, min_independent_origins=1),
        )
        assert report.state == "BLOCKED"
        assert report.checks[0].status == "STALE"

    def test_disjoint_source_classes_are_a_policy_conflict(self):
        report = _registry(_review()).evaluate(
            _request(policy={"allowed_source_classes": ["PRIMARY"], "min_independent_origins": 1}),
            server_policy=SourcePolicy(
                allowed_source_classes={"SECONDARY"}, min_independent_origins=1
            ),
        )
        assert report.state == "BLOCKED"
        assert "SOURCE_POLICY_CONFLICT" in report.reasons


# ── Corroboration ───────────────────────────────────────────────────────


class TestCorroboration:
    TEXT_A = '{"answer": "x"}'
    TEXT_B = '{"answer": "x", "note": "second"}'

    def test_one_origin_cannot_satisfy_a_two_origin_requirement(self):
        report = _registry(_review()).evaluate(_request(policy={"min_independent_origins": 2}))
        assert report.state == "BLOCKED"
        assert "SOURCE_CORROBORATION_REQUIRED" in report.reasons

    def test_two_distinct_origins_corroborate(self):
        reviews = [
            _review("r1", self.TEXT_A, origin_group="group_a"),
            _review("r2", self.TEXT_B, origin_group="group_b"),
        ]
        request = SolveRequest.model_validate(
            {
                "client_id": "c",
                "task": "extract",
                "validation": {
                    "kind": "grounded_json",
                    "fields": [
                        {"output_key": "a", "source_id": "s1", "pointer": "/answer"},
                        {"output_key": "b", "source_id": "s2", "pointer": "/answer"},
                    ],
                },
                "evidence": [
                    {"source_id": "s1", "review_id": "r1", "text": self.TEXT_A},
                    {"source_id": "s2", "review_id": "r2", "text": self.TEXT_B},
                ],
                "source_policy": {"min_independent_origins": 1},
            }
        )
        report = _registry(*reviews).evaluate(request)
        assert report.state == "PASSED"
        assert report.independent_origins == {"a": 1, "b": 1}

    def test_aliases_of_one_origin_do_not_count_twice(self):
        """Two snapshots from the same origin group is one independent origin."""
        reviews = [
            SourceReview.model_validate(_review("r1", self.TEXT_A, origin_group="same")),
            SourceReview.model_validate(_review("r2", self.TEXT_B, origin_group="same")),
        ]
        assert independent_count(reviews) == 1

    def test_distinct_origins_with_distinct_content_each_count(self):
        reviews = [
            SourceReview.model_validate(_review("r1", self.TEXT_A, origin_group="one")),
            SourceReview.model_validate(_review("r2", self.TEXT_B, origin_group="two")),
        ]
        assert independent_count(reviews) == 2

    def test_the_same_snapshot_from_two_origins_counts_once(self):
        """Identical content is one snapshot however many origins republish it."""
        reviews = [
            SourceReview.model_validate(_review("r1", self.TEXT_A, origin_group="one")),
            SourceReview.model_validate(_review("r2", self.TEXT_A, origin_group="two")),
        ]
        assert independent_count(reviews) == 1

    def test_no_reviews_is_no_origins(self):
        assert independent_count([]) == 0


# ── Record and registry construction ────────────────────────────────────


class TestSourceReviewRecord:
    def test_review_dates_must_be_ordered(self):
        with pytest.raises(ValueError, match="observed_at <= reviewed_at < expires_at"):
            SourceReview.model_validate(_review(reviewed_at=NOW - timedelta(days=2)))

    def test_a_blank_client_identity_is_rejected(self):
        with pytest.raises(ValueError, match="client identity"):
            SourceReview.model_validate(_review(client_ids={"  "}))

    def test_a_blank_review_rationale_is_rejected(self):
        with pytest.raises(ValueError, match="rationale"):
            SourceReview.model_validate(_review(review_note="   "))

    def test_duplicate_review_ids_are_rejected(self):
        with pytest.raises(ValueError, match="Duplicate source review ID"):
            SourceReviewRegistry([_review("r1"), _review("r1")])

    def test_a_naive_clock_is_rejected(self):
        registry = SourceReviewRegistry([_review()], clock=lambda: datetime(2026, 9, 26, 12))
        with pytest.raises(ValueError, match="timezone-aware"):
            registry.evaluate(_request(policy={"min_independent_origins": 1}))

    def test_stored_reviews_are_copies(self):
        source = _review()
        registry = SourceReviewRegistry([source])
        source["reviewer_id"] = "mutated"
        assert registry.reviews["r1"].reviewer_id == "alice"


class TestReviewFile:
    def _write(self, tmp_path, payload):
        path = tmp_path / "reviews.yaml"
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        return path

    def test_reviews_load_from_yaml(self, tmp_path):
        review = _review()
        review["client_ids"] = sorted(review["client_ids"])
        for field in ("observed_at", "reviewed_at", "expires_at"):
            review[field] = review[field].isoformat()
        registry = SourceReviewRegistry.from_file(self._write(tmp_path, {"reviews": [review]}))
        assert registry.reviews["r1"].reviewer_id == "alice"

    def test_an_empty_review_file_is_an_empty_registry(self, tmp_path):
        assert SourceReviewRegistry.from_file(self._write(tmp_path, {"reviews": []})).reviews == {}

    def test_an_oversized_review_file_is_refused(self, tmp_path):
        path = tmp_path / "big.yaml"
        path.write_text("# " + "x" * 5_000_001, encoding="utf-8")
        with pytest.raises(ValueError, match="exceeds budget"):
            SourceReviewRegistry.from_file(path)

    def test_a_malformed_review_file_is_refused(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("reviews: [{review_id: r1}]", encoding="utf-8")
        with pytest.raises(ValueError):
            SourceReviewRegistry.from_file(path)
