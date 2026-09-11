import asyncio

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from apps.api.main import create_app
from fair.classifier.task_profiler import profile_task
from fair.performance.feedback import FeedbackDenied, FeedbackRegistry, FeedbackRequest
from fair.providers.base import AuthenticationFailed
from fair.providers.mock import MockAdapter
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import (
    AuditEvent,
    CacheEntry,
    ModelTaskPerformance,
    RoutingAttempt,
    TaskRequest,
)


def request(**changes):
    return SolveRequest(
        **(
            {
                "client_id": "alice",
                "task": "Calculate 2 + 2",
                "validation": {"kind": "arithmetic", "expression": "2 + 2"},
            }
            | changes
        )
    )


def build(make_router, **settings):
    mock = MockAdapter("a", "4")
    spec = provider(
        max_data_class="RESTRICTED",
        models=[
            {"model_id": "model", "context_window": 32768, "capabilities": ["structured_output"]}
        ],
    )
    router = make_router([(spec, mock)], **({"cache_enabled": True} | settings))
    return router, mock


async def test_hit_revalidates_with_new_lineage_and_no_quota_or_samples(make_router):
    router, mock = build(make_router)
    first = await router.solve(request())
    second = await router.solve(request())
    assert first.status == second.status == "ACCEPTED"
    assert not first.cache_hit and second.cache_hit
    assert (
        second.request_id != first.request_id and second.cached_from_request_id == first.request_id
    )
    assert second.attempts == [] and second.verification_state == "DETERMINISTIC_ARITHMETIC"
    assert mock.calls == (await router.quota.state("a")).used == 1
    with router.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RoutingAttempt)) == 1
        assert session.scalar(select(ModelTaskPerformance)).quality_samples == 1
        assert session.get(TaskRequest, second.request_id).result_json["cache_hit"]
        assert (
            session.scalar(
                select(AuditEvent).where(
                    AuditEvent.request_id == second.request_id, AuditEvent.event_type == "CACHE_HIT"
                )
            )
            is not None
        )


@pytest.mark.parametrize("privacy", ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"])
async def test_clients_never_share_entries(make_router, privacy):
    router, mock = build(make_router)
    first = await router.solve(request(privacy_class=privacy))
    bob = await router.solve(request(client_id="bob", privacy_class=privacy))
    again = await router.solve(request(privacy_class=privacy))
    assert not bob.cache_hit and again.cached_from_request_id == first.request_id
    assert mock.calls == 2
    await router.cache.clear("alice")
    assert (await router.solve(request(client_id="bob", privacy_class=privacy))).cache_hit
    assert not (await router.solve(request(privacy_class=privacy))).cache_hit


async def test_cache_survives_restart(make_router):
    router, mock = build(make_router)
    first = await router.solve(request())
    restarted = Router(router.registry, router.settings, router.thresholds, router.sessions)
    second = await restarted.solve(request())
    assert second.cached_from_request_id == first.request_id and mock.calls == 1


async def test_expiry_and_shorter_client_ttl_do_not_slide(make_router):
    router, mock = build(make_router, cache_ttl_seconds=100)
    now = [1000]
    router.cache.clock = lambda: now[0]
    await router.solve(request())
    now[0] = 1050
    assert (await router.solve(request())).cache_hit
    assert not (await router.solve(request(cache_ttl_seconds=10))).cache_hit
    now[0] = 1060
    assert not (await router.solve(request())).cache_hit
    assert mock.calls == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"task": "Calculate  2 + 2"},
        {"privacy_class": "INTERNAL"},
        {"quality_level": "advanced"},
        {"validation": {"kind": "arithmetic", "expression": "1 + 3"}},
        {"expected_schema": {"type": "number"}},
    ],
)
async def test_request_contract_changes_miss(make_router, changes):
    router, mock = build(make_router)
    await router.solve(request())
    assert not (await router.solve(request(**changes))).cache_hit
    assert mock.calls == 2


async def test_key_canonicalizes_sets_and_object_order_but_not_arrays(make_router):
    router, _ = build(make_router)
    first = request(validation={"kind": "reference_json", "expected": {"a": [1, 2], "b": 3}})
    second = request(validation={"kind": "reference_json", "expected": {"b": 3, "a": [1, 2]}})

    def key(req):
        return router.cache.key(req, profile_task(req, router.thresholds))

    assert key(first) == key(second)
    assert key(first) != key(
        request(validation={"kind": "reference_json", "expected": {"a": [2, 1], "b": 3}})
    )


@pytest.mark.parametrize("field,value", [("model_revision", "rev2"), ("context_window", 32000)])
async def test_model_configuration_changes_invalidate(make_router, field, value):
    router, mock = build(make_router)
    await router.solve(request())
    setattr(router.registry.providers["a"].models[0], field, value)
    assert not (await router.solve(request())).cache_hit
    assert mock.calls == 2


async def test_threshold_and_engine_changes_invalidate(make_router, monkeypatch):
    router, mock = build(make_router)
    await router.solve(request())
    router.thresholds["standard"] = 83
    assert not (await router.solve(request())).cache_hit
    monkeypatch.setattr("fair.cache.exact.ENGINE_VERSION", "next-version")
    assert not (await router.solve(request())).cache_hit
    assert mock.calls == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"cache_mode": "bypass"},
        {"freshness_required": True},
        {"task": "Find current sources"},
        {"cross_check_required": True},
        {"quality_level": "high_impact_support"},
        {"validation": None},
        {"evidence": [{"source_id": "data", "text": "4"}]},
    ],
)
async def test_unsafe_or_bypassed_requests_never_reuse(make_router, changes):
    router, _ = build(make_router)
    await router.solve(request())
    req = request(**changes)
    assert router.cache.key(req, profile_task(req, router.thresholds)) is None
    assert not (await router.solve(req)).cache_hit


async def test_disabled_is_default_and_refresh_replaces_source(make_router):
    router, mock = build(make_router, cache_enabled=False)
    await router.solve(request())
    assert not (await router.solve(request())).cache_hit
    router.settings.cache_enabled = True
    await router.solve(request())
    fresh = await router.solve(request(cache_mode="refresh"))
    assert not fresh.cache_hit
    assert (await router.solve(request())).cached_from_request_id == fresh.request_id
    assert mock.calls == 4


async def test_stop_security_block_and_disabled_provider_cannot_release_cache(make_router):
    router, mock = build(make_router)
    await router.solve(request())
    router.stopped = True
    assert (await router.solve(request())).reason_code == "SYSTEM_STOPPED"
    router.stopped = False
    await router.quota.block_security("a")
    assert (await router.solve(request())).status != "ACCEPTED"
    router.registry.providers["a"].status = "DISABLED"
    assert (await router.solve(request())).status != "ACCEPTED"
    assert mock.calls == 1


async def test_exhausted_quota_allows_validated_reuse(make_router):
    router, mock = build(make_router)
    await router.solve(request())
    await router.quota.exhaust("a")
    assert (await router.solve(request())).cache_hit
    assert mock.calls == 1 and (await router.quota.state("a")).exhausted


async def test_invalid_cached_output_is_revalidated_and_replaced(make_router):
    router, mock = build(make_router)
    first = await router.solve(request())
    with router.sessions.begin() as session:
        row = session.get(TaskRequest, first.request_id)
        row.result_json = row.result_json | {"output": "5"}
    result = await router.solve(request())
    assert not result.cache_hit and result.output == "4" and mock.calls == 2


async def test_failures_are_not_cached(make_router):
    router, mock = build(make_router)
    mock.text = "5"
    await router.solve(request())
    assert not (await router.solve(request())).cache_hit
    with router.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CacheEntry)) == 0


async def test_negative_feedback_invalidates_and_hits_cannot_amplify_feedback(make_router):
    router, mock = build(make_router)
    first = await router.solve(request())
    cached = await router.solve(request())
    feedback = FeedbackRegistry(router.sessions)
    with pytest.raises(FeedbackDenied, match="FEEDBACK_USE_ORIGINAL_REQUEST"):
        feedback.submit("alice", FeedbackRequest(request_id=cached.request_id, accepted=True))
    feedback.submit("alice", FeedbackRequest(request_id=first.request_id, accepted=False))
    assert not (await router.solve(request())).cache_hit and mock.calls == 2


async def test_bounded_eviction_and_expired_cleanup(make_router):
    router, _ = build(make_router, cache_max_entries=2)
    now = [1000]
    router.cache.clock = lambda: now[0]
    for client in ["a", "b", "c"]:
        await router.solve(request(client_id=client))
        now[0] += 1
    with router.sessions() as session:
        assert set(session.scalars(select(CacheEntry.client_id))) == {"b", "c"}
    now[0] += 4000
    await router.solve(request())
    with router.sessions() as session:
        assert list(session.scalars(select(CacheEntry.client_id))) == ["alice"]


async def test_concurrent_exact_requests_share_one_inference(make_router):
    router, mock = build(make_router)
    results = await asyncio.gather(*(router.solve(request()) for _ in range(5)))
    assert mock.calls == 1 and sum(result.cache_hit for result in results) == 4


def test_cache_api_is_authenticated_and_client_scoped(make_router):
    router, _ = build(make_router)
    app = create_app(router, {"alice": "alice-key", "bob": "bob-key"}, "admin-key")
    with TestClient(app) as client:

        def solve(name):
            return client.post(
                "/v1/solve",
                json=request(client_id=name).model_dump(mode="json"),
                headers={"X-API-Key": name + "-key"},
            ).json()

        alice, bob = solve("alice"), solve("bob")
        assert client.delete("/v1/cache").status_code == 401
        assert client.delete("/v1/cache", headers={"X-API-Key": "alice-key"}).json() == {
            "entries_removed": 1
        }
        assert solve("bob")["cached_from_request_id"] == bob["request_id"]
        assert not solve("alice")["cache_hit"]
        assert (
            client.get(
                "/v1/requests/" + alice["request_id"], headers={"X-API-Key": "bob-key"}
            ).status_code
            == 404
        )


async def test_reference_json_result_reuse(make_router):
    router, mock = build(make_router)
    mock.text = '{"answer": 4}'
    req = request(validation={"kind": "reference_json", "expected": {"answer": 4}})
    assert (await router.solve(req)).status == "ACCEPTED"
    assert (await router.solve(req)).verification_state == "HOST_REFERENCE_MATCH"
    assert mock.calls == 1


async def test_live_admission_revocation_blocks_cached_release(make_router):
    router, mock = build(make_router)
    await router.solve(request())

    def revoked():
        raise AuthenticationFailed("CURRENT_TERMS_REVIEW_REQUIRED")

    mock.check_admission = revoked
    mock.error = AuthenticationFailed("CURRENT_TERMS_REVIEW_REQUIRED")
    result = await router.solve(request())
    assert not result.cache_hit and result.status != "ACCEPTED"


async def test_cache_hit_does_not_launch_shadow(make_router, monkeypatch):
    router, _ = build(make_router, shadow_enabled=True)
    await router.solve(request())
    await asyncio.sleep(0)
    calls = []

    async def observe(*args):
        calls.append(args)

    monkeypatch.setattr(router, "_shadow", observe)
    assert (await router.solve(request())).cache_hit
    await asyncio.sleep(0)
    assert calls == []


async def test_cache_write_outage_preserves_durable_accepted_result(make_router, monkeypatch):
    router, _ = build(make_router)

    def unavailable(*args):
        raise SQLAlchemyError("private database diagnostic")

    monkeypatch.setattr(router.cache, "put", unavailable)
    result = await router.solve(request())
    assert result.status == "ACCEPTED"
    with router.sessions() as session:
        row = session.get(TaskRequest, result.request_id)
        assert row.status == row.result_json["status"] == "ACCEPTED"
        assert (
            session.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "CACHE_WRITE_FAILED")
            ).payload_json
            == {}
        )
