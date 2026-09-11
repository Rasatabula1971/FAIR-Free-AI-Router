import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select

from apps.api.main import create_app
from fair.classifier.task_profiler import profile_task
from fair.performance.feedback import FeedbackDenied, FeedbackRegistry, FeedbackRequest
from fair.providers.mock import MockAdapter
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import AuditEvent, FeedbackEvent, ModelTaskPerformance, TaskRequest


def request(client="alice", **changes):
    return SolveRequest(
        client_id=client,
        task="Calculate",
        validation={"kind": "arithmetic", "expression": "2+2"},
        **changes,
    )


def record(router, values, disposition="ACCEPTED"):
    with router.sessions.begin() as session:
        for score in values:
            attempt = SimpleNamespace(
                provider_id="a",
                model_id="model",
                disposition=disposition,
                latency_ms=50,
                quality=SimpleNamespace(overall_score=score, reject_reasons=[])
                if score is not None
                else None,
            )
            router.performance.record(session, attempt, "arithmetic")


def describe(router):
    with router.sessions() as session:
        return router.performance.describe(
            session.get(ModelTaskPerformance, ("a", "model", "arithmetic"))
        )


async def test_feedback_is_idempotent_private_and_only_changes_own_preferences(make_router):
    router = make_router([(provider(), MockAdapter("a", text="4"))])
    result = await router.solve(request())
    registry = FeedbackRegistry(router.sessions)
    before = await router.performance.scores("a", "model", "arithmetic", client_id="alice")
    feedback = FeedbackRequest(
        request_id=result.request_id,
        accepted=False,
        reason="private reason",
        correction_text="private correction",
    )
    report = registry.submit("alice", feedback)
    assert registry.submit("alice", feedback) == report
    assert (await router.performance.scores("a", "model", "arithmetic", client_id="alice"))[0] < before[0]
    assert await router.performance.scores("a", "model", "arithmetic", client_id="bob") == before
    assert (await router.performance.scores("a", "model", "arithmetic", client_id="alice"))[1] == before[1]
    with pytest.raises(FeedbackDenied, match="ALREADY_RECORDED"):
        registry.submit("alice", FeedbackRequest(request_id=result.request_id, accepted=True))
    with pytest.raises(FeedbackDenied, match="NOT_FOUND"):
        registry.submit("bob", feedback)
    with router.sessions() as session:
        assert len(list(session.scalars(select(FeedbackEvent)))) == 1
        assert session.get(TaskRequest, result.request_id).result_json == result.model_dump(
            mode="json"
        )
        audit = list(session.scalars(select(AuditEvent)))
        assert sum(row.event_type == "FEEDBACK_RECORDED" for row in audit) == 1
        assert "private reason" not in json.dumps([row.payload_json for row in audit])
        row = session.scalar(select(FeedbackEvent))
        assert "private correction" not in repr(vars(row))


async def test_positive_feedback_is_bounded_by_benchmark_and_expires(make_router):
    router = make_router([(provider(), MockAdapter("a", text="4"))])
    result = await router.solve(request())
    registry = FeedbackRegistry(router.sessions)
    before = (await router.performance.scores("a", "model", "arithmetic"))[0]
    registry.submit("alice", FeedbackRequest(request_id=result.request_id, rating=5))
    assert (await router.performance.scores("a", "model", "arithmetic", client_id="alice"))[0] > before
    assert (
        (await router.performance.scores("a", "model", "arithmetic", client_id="alice", quality_prior=0.8))[
            0
        ]
        <= 0.8
    )
    router.performance.clock = lambda: datetime.now(UTC) + timedelta(days=31)
    assert (await router.performance.scores("a", "model", "arithmetic", client_id="alice"))[0] == before


async def test_feedback_failure_rolls_back_and_cannot_label_unverified(make_router):
    router = make_router()
    rejected = await router.solve(request())
    registry = FeedbackRegistry(router.sessions)
    with pytest.raises(FeedbackDenied, match="ACCEPTED_PRIMARY"):
        registry.submit("alice", FeedbackRequest(request_id=rejected.request_id, accepted=True))
    router.registry.adapters["a"].text = "4"
    result = await router.solve(request())

    def fail(mapper, connection, target):
        if target.event_type == "FEEDBACK_RECORDED":
            raise RuntimeError("audit unavailable")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError):
            registry.submit("alice", FeedbackRequest(request_id=result.request_id, accepted=True))
        with router.sessions() as session:
            assert not list(session.scalars(select(FeedbackEvent)))
    finally:
        event.remove(AuditEvent, "before_insert", fail)


def test_feedback_http_ownership_and_performance_reports(make_router):
    router = make_router([(provider(), MockAdapter("a", text="4"))])
    with TestClient(create_app(router, {"alice": "a", "bob": "b"}, "admin")) as client:
        result = client.post(
            "/v1/solve", headers={"X-API-Key": "a"}, json=request().model_dump(mode="json")
        ).json()
        body = {"request_id": result["request_id"], "rating": 5}
        assert client.post("/v1/feedback", json=body).status_code == 401
        assert client.post("/v1/feedback", headers={"X-API-Key": "b"}, json=body).status_code == 404
        report = client.post("/v1/feedback", headers={"X-API-Key": "a"}, json=body).json()
        path = f"/v1/requests/{result['request_id']}/feedback"
        assert client.get(path, headers={"X-API-Key": "a"}).json() == report
        assert client.get(path, headers={"X-API-Key": "b"}).status_code == 404
        assert client.get("/v1/models/performance", headers={"X-API-Key": "a"}).status_code == 403
        performance = client.get("/v1/models/performance", headers={"X-API-Key": "admin"}).json()[0]
        assert (
            performance["recent_quality_samples"] == 1 and performance["observation_confidence"] > 0
        )
        assert "alice" not in json.dumps(performance)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"rating": 0},
        {"rating": 6},
        {"rating": "5"},
        {"rating": True},
        {"accepted": True, "correction_text": "x" * 10001},
    ],
)
def test_invalid_feedback(values):
    with pytest.raises(ValidationError):
        FeedbackRequest(request_id="id", **values)


async def test_rolling_drift_changes_rank_with_separate_baseline(make_router):
    router = make_router()
    record(router, [100] * 40)
    before = (await router.performance.scores("a", "model", "arithmetic"))[0]
    assert describe(router)["drift_state"] == "STABLE"
    record(router, [20] * 20, "QUALITY_FAILURE")
    report = describe(router)
    assert report["baseline_quality_samples"] == 40
    assert report["baseline_average_quality"] == 100 and report["recent_average_quality"] == 20
    assert report["benchmark_review_required"] and report["drift_state"] == "DEGRADED"
    quality = (await router.performance.scores("a", "model", "arithmetic"))[0]
    assert quality < before and quality < (44 + 2.5) / 65
    record(router, [100] * 20)
    assert describe(router)["drift_state"] == "STABLE"


async def test_confidence_grows_decays_and_infra_does_not_refresh_quality(make_router):
    router = make_router()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    router.performance.clock = lambda: now
    record(router, [100])
    sparse = describe(router)
    assert sparse["drift_state"] == "INSUFFICIENT_DATA"
    record(router, [100] * 9)
    strong = describe(router)["observation_confidence"]
    assert strong > sparse["observation_confidence"]
    before = (await router.performance.scores("a", "model", "arithmetic"))[0]
    now += timedelta(days=30)
    record(router, [None] * 21, "INFRA_FAILURE")
    report = describe(router)
    assert report["observation_confidence"] == pytest.approx(strong / 2)
    assert report["recent_attempts"] == report["recent_infra_failures"] == 20
    assert report["recent_quality_samples"] == 10
    assert (await router.performance.scores("a", "model", "arithmetic"))[0] == before
    record(router, [None] * 21, "QUOTA_FAILURE")
    assert describe(router)["recent_quota_failures"] == 20
    assert (await router.performance.scores("a", "model", "arithmetic"))[0] == before


def test_unknown_legacy_freshness_has_no_invented_confidence(make_router):
    router = make_router()
    record(router, [100] * 10)
    with router.sessions.begin() as session:
        session.get(ModelTaskPerformance, ("a", "model", "arithmetic")).last_quality_at = None
    assert describe(router)["observation_confidence"] == 0


async def drain(router):
    while router.shadow_tasks:
        await asyncio.gather(*list(router.shadow_tasks))


def shadow_router(make_router, **settings):
    first = provider(request_limit=1000, max_data_class="RESTRICTED")
    second = provider(
        "b",
        request_limit=1000,
        max_data_class="RESTRICTED",
        models=[{"model_id": "other", "context_window": 32768}],
    )
    return make_router(
        [(first, MockAdapter("a", text="4")), (second, MockAdapter("b", text="4/1"))],
        **({"shadow_enabled": True} | settings),
    )


async def test_shadow_rate_lineage_comparison_and_output_withholding(make_router):
    router = shadow_router(make_router)
    for index in range(20):
        result = await router.solve(request())
        assert result.output == "4"
        await drain(router)
        with router.sessions() as session:
            shadows = list(
                session.scalars(select(TaskRequest).where(TaskRequest.execution_kind == "SHADOW"))
            )
            assert len(shadows) == (index + 1) // 10
        if index == 9:
            await router.close()
            router = Router(router.registry, router.settings, router.thresholds, router.sessions)
    assert router.registry.adapters["b"].calls == 2
    with router.sessions() as session:
        for row in shadows:
            assert row.parent_request_id and row.result_json["output"] is None
            assert row.result_json["execution_kind"] == "SHADOW"
            audits = list(
                session.scalars(select(AuditEvent).where(AuditEvent.request_id == row.id))
            )
            assert audits[0].payload_json["priority"] == "P4"
            comparison = next(event for event in audits if event.event_type == "SHADOW_COMPARISON")
            assert comparison.payload_json["agreement"] is True
            assert "4/1" not in json.dumps(row.result_json)
            assert "4/1" not in json.dumps([event.payload_json for event in audits])
    with pytest.raises(FeedbackDenied, match="ACCEPTED_PRIMARY"):
        FeedbackRegistry(router.sessions).submit(
            "alice", FeedbackRequest(request_id=shadows[0].id, accepted=True)
        )
    await router.close()


@pytest.mark.parametrize(
    "mode",
    [
        "disabled",
        "private",
        "evidence",
        "unknown_quota",
        "low_quota",
        "same_model",
        "paid",
        "stopped",
    ],
)
async def test_shadow_is_opt_in_and_preserves_governance(make_router, mode):
    router = shadow_router(make_router)
    args = {}
    if mode == "disabled":
        router.settings.shadow_enabled = False
    if mode == "private":
        args["privacy_class"] = "CONFIDENTIAL"
    if mode == "evidence":
        args["evidence"] = [{"source_id": "x", "text": "public evidence"}]
    candidate = router.registry.providers["b"]
    if mode == "unknown_quota":
        candidate.request_limit = None
    if mode == "low_quota":
        candidate.request_limit = 1
        await router.quota.reserve(candidate)
    if mode == "same_model":
        candidate.models[0].independence_group = "model"
    if mode == "paid":
        candidate.current_access_cost_usd = 1
    for _ in range(10):
        await router.solve(request(**args))
        if mode == "stopped":
            router.stopped = True
        await drain(router)
    assert router.registry.adapters["b"].calls == 0
    with router.sessions() as session:
        assert not list(
            session.scalars(select(TaskRequest).where(TaskRequest.execution_kind == "SHADOW"))
        )
    await router.close()


async def test_feedback_changes_selector_only_for_owner(make_router):
    router = make_router(
        [(provider(), MockAdapter("a", text="4")), (provider("b"), MockAdapter("b", text="4"))]
    )
    result = await router.solve(request())
    FeedbackRegistry(router.sessions).submit(
        "alice", FeedbackRequest(request_id=result.request_id, accepted=False)
    )
    for client, expected in [("alice", "b"), ("bob", "a")]:
        value = request(client)
        candidates = await router.selector.candidates(
            value, profile_task(value, router.thresholds), set()
        )
        assert candidates[0][1].provider_id == expected


async def test_shutdown_cancels_active_shadow_without_returning_output(make_router):
    router = shadow_router(make_router)
    for _ in range(9):
        await router.solve(request())
        await drain(router)
    started = asyncio.Event()

    async def wait(value):
        started.set()
        await asyncio.Event().wait()

    router.registry.adapters["b"].complete = wait
    result = await router.solve(request())
    await started.wait()
    await router.close()
    assert result.output == "4" and not router.shadow_tasks and not router.inflight
    with router.sessions() as session:
        row = session.scalar(select(TaskRequest).where(TaskRequest.execution_kind == "SHADOW"))
        assert row.status == "CANCELLED" and row.result_json is None


async def test_shadow_rechecks_quota_after_waiting_behind_user_work(make_router):
    router = shadow_router(make_router)
    for _ in range(9):
        await router.solve(request())
        await drain(router)
    started, release = asyncio.Event(), asyncio.Event()
    original = router.registry.adapters["a"].complete

    async def held(value):
        if value.client_id == "bob":
            started.set()
            await release.wait()
        return await original(value)

    router.registry.adapters["a"].complete = held
    tenth = asyncio.create_task(router.solve(request()))
    user = asyncio.create_task(router.solve(request("bob")))
    await started.wait()
    await tenth
    async with asyncio.timeout(2):
        while router.scheduler.queued == 0:
            await asyncio.sleep(0)
    assert router.registry.adapters["b"].calls == 0
    candidate = router.registry.providers["b"]
    candidate.request_limit = 1
    await router.quota.reserve(candidate)
    release.set()
    await user
    await drain(router)
    assert router.registry.adapters["b"].calls == 0
    await router.close()
