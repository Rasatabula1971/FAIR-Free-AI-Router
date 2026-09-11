import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from test_claim_grounding import answered, fact, model, output, request, source

from apps.api.main import create_app
from fair.classifier.task_profiler import model_task, profile_task
from fair.providers.mock import MockAdapter
from fair.quality.contracts import SourcePolicy
from fair.quality.engine import evaluate
from fair.quality.source_reviews import SourceReview, SourceReviewRegistry
from fair.schemas.api import SolveRequest
from fair.schemas.db import AuditEvent, ModelTaskPerformance
from fair.schemas.domain import NormalizedModelResponse

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def evidence(**changes):
    return {**source(), "review_id": "review-one", **changes}


def review(item=None, **changes):
    item = item or evidence()
    return SourceReview(
        **{
            "review_id": item["review_id"],
            "client_ids": {"alice"},
            "content_sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
            "origin_group": "publisher-one",
            "source_class": "PRIMARY",
            "decision": "APPROVED",
            "reviewer_id": "operator-one",
            "source_locator": "internal://reviewed-snapshot",
            "review_note": "Reviewed the original record and its context.",
            "observed_at": NOW - timedelta(hours=2),
            "reviewed_at": NOW - timedelta(hours=1),
            "expires_at": NOW + timedelta(days=1),
            **changes,
        }
    )


def reviewed_request(**changes):
    return request(**{"evidence": [evidence()], "source_policy": {}, **changes})


def registry(records=None):
    return SourceReviewRegistry([review()] if records is None else records, clock=lambda: NOW)


def router_for(make_router, records=None):
    router = make_router([(model("a"), MockAdapter("a", text=output(answered())))])
    router.source_reviews = registry(records)
    return router


async def test_reviewed_snapshot_passes_and_is_audited(make_router):
    router = router_for(make_router)
    result = await router.solve(reviewed_request())
    assert result.status == "ACCEPTED" and result.source_policy.state == "PASSED"
    assert result.quality.source_policy.state == "PASSED"
    assert result.source_policy.independent_origins == {"population": 1}
    with router.sessions() as session:
        audits = list(session.scalars(select(AuditEvent)))
    assert any(row.event_type == "SOURCE_POLICY_CHECKED" for row in audits)
    payload = json.dumps([row.payload_json for row in audits])
    assert "publisher-one" not in payload and "content_sha256" not in payload
    assert evidence()["text"] not in payload


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"decision": "REJECTED"}, "REJECTED"),
        ({"client_ids": {"bob"}}, "REVIEW_UNAVAILABLE"),
        ({"content_sha256": "0" * 64}, "CONTENT_MISMATCH"),
        ({"expires_at": NOW}, "EXPIRED"),
        ({"reviewed_at": NOW + timedelta(minutes=1)}, "FUTURE_REVIEW"),
        ({"observed_at": NOW - timedelta(days=2)}, "STALE"),
    ],
)
async def test_ineligible_snapshot_blocks_before_inference_or_quota(make_router, changes, status):
    router = router_for(make_router, [review(**changes)])
    result = await router.solve(reviewed_request())
    assert result.reason_code == "SOURCE_POLICY_UNSATISFIED" and result.output is None
    assert result.source_policy.checks[0].status == status
    assert not result.attempts and router.registry.adapters["a"].calls == 0
    assert (await router.quota.state("a")).used == 0
    with router.sessions() as session:
        assert not list(session.scalars(select(ModelTaskPerformance)))


@pytest.mark.parametrize("items", [[], [source()], [evidence(review_id="unknown")]])
async def test_missing_reviews_fail_closed(make_router, items):
    router = router_for(make_router)
    result = await router.solve(reviewed_request(evidence=items))
    assert result.source_policy.state == "BLOCKED" and not result.attempts


async def test_changed_content_cannot_reuse_approval(make_router):
    router = router_for(make_router)
    item = evidence(text=source(facts=[fact(2000)])["text"])
    result = await router.solve(reviewed_request(evidence=[item]))
    assert result.source_policy.checks[0].status == "CONTENT_MISMATCH"
    assert router.registry.adapters["a"].calls == 0


async def test_client_cannot_weaken_server_policy_or_omit_it(make_router):
    router = router_for(make_router, [review(source_class="SECONDARY")])
    router.settings.source_policy = SourcePolicy(allowed_source_classes={"PRIMARY"})
    for policy in (
        None,
        {"allowed_source_classes": ["PRIMARY", "SECONDARY"], "max_age_seconds": 31536000},
    ):
        result = await router.solve(reviewed_request(source_policy=policy))
        assert result.source_policy.checks[0].status == "SOURCE_CLASS_DISALLOWED"
    assert router.registry.adapters["a"].calls == 0


def test_server_age_and_corroboration_floors_are_combined():
    report = registry().evaluate(
        reviewed_request(source_policy={"max_age_seconds": 31536000}),
        SourcePolicy(max_age_seconds=3600, min_independent_origins=2),
    )
    assert report.checks[0].status == "STALE" and report.state == "BLOCKED"
    assert "SOURCE_CORROBORATION_REQUIRED" in report.reasons


def test_disjoint_class_policies_cannot_pass():
    report = registry().evaluate(
        reviewed_request(source_policy={"allowed_source_classes": ["SECONDARY"]}),
        SourcePolicy(allowed_source_classes={"PRIMARY"}),
    )
    assert "SOURCE_POLICY_CONFLICT" in report.reasons


@pytest.mark.parametrize(
    "mode,expected", [("independent", 2), ("same_origin", 1), ("same_content", 1), ("unrelated", 1)]
)
def test_independent_corroboration_is_per_claim_and_deduplicated(mode, expected):
    first = evidence()
    facts = [fact(), fact(900, context="2024; residents")]
    if mode == "same_content":
        facts = [fact()]
    elif mode == "unrelated":
        facts = [fact(900, context="2024; residents")]
    second = {**source("second", facts), "review_id": "review-two"}
    records = [
        review(first),
        review(second, origin_group="publisher-one" if mode == "same_origin" else "publisher-two"),
    ]
    report = registry(records).evaluate(
        reviewed_request(evidence=[first, second], source_policy={"min_independent_origins": 2})
    )
    assert report.independent_origins == {"population": expected}
    assert report.state == ("PASSED" if expected == 2 else "BLOCKED")


async def test_credibility_gate_does_not_erase_conflicting_evidence(make_router):
    first, second = evidence(), {**source("second", [fact(2000)]), "review_id": "review-two"}
    router = router_for(make_router, [review(first), review(second, origin_group="publisher-two")])
    result = await router.solve(reviewed_request(evidence=[first, second]))
    assert result.source_policy.state == "PASSED" and result.output is None
    assert result.attempts[0].quality.reject_reasons == ["CLAIM_OVER_CONFLICTING_EVIDENCE"]


async def test_expiry_during_call_withholds_result_without_quality_penalty(make_router):
    router = router_for(make_router)

    class Expire(MockAdapter):
        async def complete(self, req):
            router.source_reviews.clock = lambda: NOW + timedelta(days=2)
            return await super().complete(req)

    router.registry.adapters["a"] = Expire("a", text=output(answered()))
    result = await router.solve(reviewed_request())
    assert result.output is None and result.reason_code == "SOURCE_POLICY_UNSATISFIED"
    assert len(result.attempts) == 1 and result.attempts[0].disposition == "UNVERIFIED"
    with router.sessions() as session:
        stats = session.get(ModelTaskPerformance, ("a", "model", "grounded_claims"))
        assert stats.quality_samples == stats.infra_failures == 0


async def test_review_service_failure_is_normalized_and_never_dispatches(make_router):
    router = router_for(make_router)

    def broken_clock():
        raise RuntimeError("private-clock-error")

    router.source_reviews.clock = broken_clock
    result = await router.solve(reviewed_request())
    assert result.status == "FAILED" and result.source_policy.state == "SERVICE_FAILED"
    assert "private-clock-error" not in result.model_dump_json()
    assert router.registry.adapters["a"].calls == 0


def test_direct_quality_call_without_trusted_registry_cannot_approve_policy():
    req = reviewed_request()
    profile = profile_task(req, {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92})
    report = evaluate(
        req,
        profile,
        NormalizedModelResponse(provider_id="a", model_id="model", text=output(answered())),
    )
    assert report.verification_state == "UNVERIFIED" and report.overall_score is None


def test_review_metadata_is_withheld_from_provider():
    assert "review-one" not in model_task(reviewed_request())
    assert "publisher-one" not in model_task(reviewed_request())


def test_review_file_load_restart_and_duplicate_ids(tmp_path):
    path = tmp_path / "reviews.yaml"
    record = review().model_dump(mode="json")
    path.write_text(yaml.safe_dump({"reviews": [record]}), encoding="utf-8")
    fingerprints = []
    for _ in range(2):
        loaded = SourceReviewRegistry.from_file(path)
        loaded.clock = lambda: NOW
        report = loaded.evaluate(reviewed_request())
        assert report.state == "PASSED"
        fingerprints.append(report.policy_fingerprint)
    assert fingerprints[0] == fingerprints[1]
    path.write_text(yaml.safe_dump({"reviews": [record, record]}), encoding="utf-8")
    with pytest.raises(ValueError):
        SourceReviewRegistry.from_file(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at": NOW + timedelta(days=3)},
        {"expires_at": NOW - timedelta(days=1)},
        {"reviewed_at": NOW.replace(tzinfo=None)},
        {"client_ids": set()},
        {"client_ids": {" "}},
        {"decision": "TRUST_ME"},
        {"content_sha256": "bad"},
    ],
)
def test_invalid_operator_reviews_rejected(changes):
    with pytest.raises(ValidationError):
        review(**changes)


def test_requests_cannot_self_approve_evidence():
    body = reviewed_request().model_dump(mode="json")
    body["evidence"][0]["decision"] = "APPROVED"
    with pytest.raises(ValidationError):
        SolveRequest.model_validate(body)
    body = reviewed_request().model_dump(mode="json")
    body["validation"] = {"kind": "arithmetic", "expression": "1+1"}
    with pytest.raises(ValidationError):
        SolveRequest.model_validate(body)


def test_http_reports_and_history_remain_client_isolated(make_router):
    router = router_for(make_router)
    with TestClient(create_app(router, {"alice": "key", "bob": "other"}, "admin")) as client:
        response = client.post(
            "/v1/solve",
            headers={"X-API-Key": "key"},
            json=reviewed_request().model_dump(mode="json"),
        )
        assert response.status_code == 200 and response.json()["source_policy"]["state"] == "PASSED"
        path = f"/v1/requests/{response.json()['request_id']}"
        assert client.get(path, headers={"X-API-Key": "key"}).json() == response.json()
        assert client.get(path, headers={"X-API-Key": "other"}).status_code == 404


async def test_reviewed_snapshot_does_not_claim_live_freshness(make_router):
    result = await router_for(make_router).solve(reviewed_request(freshness_required=True))
    assert result.source_policy.state == "PASSED" and result.verification_state == "UNVERIFIED"
    assert result.output is None


@pytest.mark.parametrize("expiry_call,expected_calls", [(3, 1), (5, 2)])
async def test_expiry_during_cross_check_never_releases_answer(
    make_router, expiry_call, expected_calls
):
    a, b = model("a"), model("b")
    b.models[0].model_id = "independent"
    router = make_router(
        [
            (a, MockAdapter("a", text=output(answered()))),
            (b, MockAdapter("b", text=output(answered()))),
        ]
    )
    router.source_reviews = registry()
    calls = 0

    def advancing_clock():
        nonlocal calls
        calls += 1
        return NOW if calls < expiry_call else NOW + timedelta(days=2)

    router.source_reviews.clock = advancing_clock
    result = await router.solve(reviewed_request(cross_check_required=True))
    assert result.output is None and result.reason_code == "SOURCE_POLICY_UNSATISFIED"
    assert result.source_policy.state == "BLOCKED"
    assert sum(adapter.calls for adapter in router.registry.adapters.values()) == expected_calls
    if expiry_call == 3:
        assert result.cross_check.state == "SOURCE_BLOCKED"


def test_review_binding_is_exact_including_whitespace():
    item = evidence(text=evidence()["text"] + " ")
    assert (
        registry().evaluate(reviewed_request(evidence=[item])).checks[0].status
        == "CONTENT_MISMATCH"
    )


def test_unreviewed_extra_evidence_is_not_silently_ignored():
    req = reviewed_request(
        evidence=[evidence(), source("unreviewed", [fact(900, context="2024; residents")])]
    )
    report = registry().evaluate(req)
    assert report.state == "BLOCKED" and report.checks[1].status == "REVIEW_UNAVAILABLE"


def test_grounded_json_review_policy_checks_selected_source():
    item = evidence(text='{"population":1000}')
    req = reviewed_request(
        evidence=[item],
        validation={
            "kind": "grounded_json",
            "fields": [
                {"output_key": "population", "source_id": "report", "pointer": "/population"},
            ],
        },
    )
    reviews = registry([review(item)])
    assert reviews.evaluate(req).state == "PASSED"
    assert reviews.evaluate(req, SourcePolicy(min_independent_origins=2)).state == "BLOCKED"
