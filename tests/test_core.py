import asyncio

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fair.classifier.task_profiler import profile_task
from fair.governor.policy import AdmissionDenied, admit_provider
from fair.providers.base import ProviderUnavailable, QuotaExceeded, RateLimited
from fair.providers.mock import MockAdapter
from fair.schemas.api import SolveRequest
from fair.schemas.db import AuditEvent, ModelTaskPerformance, RoutingAttempt


def req(**values):
    return SolveRequest(**({"client_id": "alice", "task": "Hello"} | values))


@pytest.mark.parametrize(
    "changes",
    [
        {"current_access_cost_usd": 1},
        {"current_access_cost_usd": None},
        {"auto_billing_required": True},
        {"requires_paid_subscription": True},
        {"requires_credit_purchase": True},
        {"production_eligibility": False},
        {"programmatic_access": False},
        {"status": "TERMS_REVIEW"},
        {"access_class": "FREE_RECURRING", "terms_last_verified": None},
    ],
)
def test_admission_denies_unsafe_or_unknown_routes(changes):
    with pytest.raises(AdmissionDenied):
        admit_provider(provider(**changes))


async def test_paid_adapter_injected_after_registration_never_called(make_router):
    router = make_router()
    adapter = router.registry.adapters["a"]
    router.registry.providers["a"].current_access_cost_usd = 1
    result = await router.solve(req())
    assert result.status == "ESCALATION_REQUIRED"
    assert result.paid_inference_executed is False
    assert adapter.calls == 0


async def test_execution_boundary_rechecks_faulty_selector(make_router):
    router = make_router()
    spec = router.registry.providers["a"]
    spec.current_access_cost_usd = 1

    async def fake_candidates(*args, **kwargs):
        return [(1, spec, spec.models[0])]

    router.selector.candidates = fake_candidates
    with pytest.raises(AdmissionDenied):
        await router.solve(req())
    assert router.registry.adapters["a"].calls == 0


async def test_terms_review_is_not_selected(make_router):
    adapter = MockAdapter("a")
    router = make_router([(provider(status="TERMS_REVIEW"), adapter)])
    assert not (await router.solve(req())).attempts
    assert adapter.calls == 0


async def test_kill_switch_blocks_calls(make_router):
    router = make_router()
    router.stopped = True
    assert (await router.solve(req())).reason_code == "SYSTEM_STOPPED"
    assert router.registry.adapters["a"].calls == 0


@pytest.mark.parametrize(
    "task_request",
    [
        req(required_capabilities={"coding"}),
        req(required_capabilities={"vision"}),
        req(task="x" * 33000),
        req(privacy_class="CONFIDENTIAL"),
        req(task="debug python code"),
    ],
)
async def test_capability_context_privacy_filter(make_router, task_request):
    router = make_router()
    assert not (await router.solve(task_request)).attempts


async def test_task_specific_quality_wins(make_router):
    router = make_router([(provider(n), MockAdapter(n)) for n in ("a", "b")])
    with router.sessions.begin() as session:
        session.add(
            ModelTaskPerformance(
                provider_id="b",
                model_id="model",
                task_class="general",
                quality_samples=10,
                quality_sum=900,
            )
        )
    profile = profile_task(req(), router.thresholds)
    assert (await router.selector.candidates(req(), profile, set()))[0][1].provider_id == "b"


async def test_quota_scarcity_preserves_near_equal_model(make_router):
    router = make_router([(provider(n, request_limit=100), MockAdapter(n)) for n in ("a", "b")])
    with router.sessions.begin() as session:
        for name, total in (("a", 850), ("b", 830)):
            session.add(
                ModelTaskPerformance(
                    provider_id=name,
                    model_id="model",
                    task_class="general",
                    quality_samples=10,
                    quality_sum=total,
                )
            )
    for _ in range(95):
        await router.quota.reserve(router.registry.providers["a"])
    assert (
        (await router.selector.candidates(req(), profile_task(req(), router.thresholds), set()))[0][
            1
        ].provider_id
        == "b"
    )


@pytest.mark.parametrize(
    "error, disposition",
    [
        (RateLimited("secret-key-canary"), "QUOTA_FAILURE"),
        (QuotaExceeded(), "QUOTA_FAILURE"),
        (ProviderUnavailable(), "INFRA_FAILURE"),
        (TimeoutError(), "INFRA_FAILURE"),
    ],
)
async def test_failover_and_no_error_or_raw_text_leak(make_router, error, disposition):
    first, second = MockAdapter("a", error=error), MockAdapter("b")
    router = make_router([(provider("a"), first), (provider("b"), second)])
    result = await router.solve(req())
    assert len(result.attempts) == 2
    assert result.attempts[0].disposition == disposition
    assert first.calls == second.calls == 1
    assert "secret-key-canary" not in result.model_dump_json()
    assert (await router.performance.scores("a", "model", "general"))[0] == 0.5
    if isinstance(error, QuotaExceeded):
        assert await router.quota.effective_status(router.registry.providers["a"]) == "QUOTA_EXHAUSTED"
    with router.sessions() as session:
        assert len(list(session.scalars(select(RoutingAttempt)))) == 2


async def test_real_timeout_is_bounded(make_router):
    class Slow(MockAdapter):
        async def complete(self, request):
            await asyncio.sleep(1)

    router = make_router([(provider(), Slow("a"))], timeout_seconds=0.01)
    result = await router.solve(req())
    assert result.attempts[0].error_type == "PROVIDER_UNAVAILABLE"


async def test_retry_limit_and_no_repeat(make_router):
    router = make_router(
        [(provider(n), MockAdapter(n)) for n in ("a", "b", "c", "d")], max_attempts=2
    )
    assert len((await router.solve(req())).attempts) == 2


async def test_all_unavailable_escalates(make_router):
    router = make_router([])
    assert (await router.solve(req())).reason_code == "NO_ELIGIBLE_FREE_MODELS"


async def test_circuit_breaker_cooldown(make_router):
    router = make_router()
    clock = [100.0]
    router.quota.clock = lambda: clock[0]
    for _ in range(3):
        await router.quota.failure("a")
    assert not await router.quota.available(router.registry.providers["a"])
    clock[0] += 61
    assert await router.quota.available(router.registry.providers["a"])
    await router.quota.success("a")
    assert not (await router.quota.state("a")).failures


async def test_quota_reservations_are_conservative(make_router):
    router = make_router([(provider(request_limit=1), MockAdapter("a"))])
    results = await asyncio.gather(router.solve(req()), router.solve(req()))
    assert sum(len(r.attempts) for r in results) == 1


@pytest.mark.parametrize("text, hard_reject", [("{}", True), ('{"answer":42}', False)])
async def test_structure_alone_is_never_accepted(make_router, text, hard_reject):
    spec = provider(
        models=[
            {"model_id": "model", "context_window": 32768, "capabilities": ["structured_output"]}
        ]
    )
    router = make_router([(spec, MockAdapter("a", text=text))])
    result = await router.solve(req(expected_schema={"type": "object", "required": ["answer"]}))
    assert result.status == "ESCALATION_REQUIRED"
    assert result.attempts[0].quality.hard_reject == hard_reject
    assert result.best_quality_score == (0 if hard_reject else None)


def test_api_auth_isolation_audit_and_admin(make_router):
    router = make_router()
    app = create_app(router, {"alice": "alice-key", "bob": "bob-key"}, "admin-key")
    with TestClient(app) as client:
        assert client.post("/v1/solve", json=req().model_dump(mode="json")).status_code == 401
        headers = {"X-API-Key": "alice-key"}
        assert (
            client.post(
                "/v1/solve", headers=headers, json=req(client_id="bob").model_dump(mode="json")
            ).status_code
            == 403
        )
        result = client.post("/v1/solve", headers=headers, json=req().model_dump(mode="json"))
        assert result.status_code == 200
        path = "/v1/requests/" + result.json()["request_id"]
        assert client.get(path, headers=headers).json() == result.json()
        assert (
            client.get(path + "/audit", headers=headers).json()[-1]["event_type"]
            == "ESCALATION_REQUIRED"
        )
        for suffix in ("", "/audit"):
            assert client.get(path + suffix, headers={"X-API-Key": "bob-key"}).status_code == 404
        assert client.post("/v1/system/stop", headers=headers).status_code == 403
        assert client.post("/v1/system/stop", headers={"X-API-Key": "admin-key"}).json()["stopped"]
        assert router.stopped
        assert (
            client.post("/v1/system/resume", headers={"X-API-Key": "admin-key"}).status_code == 200
        )
        assert not router.stopped


def test_invalid_schema_rejected_before_call(make_router):
    router = make_router()
    with TestClient(create_app(router, {"alice": "key"}, "admin")) as client:
        for schema in ({"type": "bogus"}, {"$ref": "https://example.com/schema"}):
            result = client.post(
                "/v1/solve",
                headers={"X-API-Key": "key"},
                json={**req().model_dump(mode="json"), "expected_schema": schema},
            )
            assert result.status_code == 422
    assert router.registry.adapters["a"].calls == 0


async def test_audit_is_append_only_in_orm(make_router):
    router = make_router()
    await router.solve(req())
    with router.sessions() as session:
        row = session.scalars(select(AuditEvent)).first()
        row.event_type = "tampered"
        with pytest.raises(ValueError, match="append-only"):
            session.commit()
