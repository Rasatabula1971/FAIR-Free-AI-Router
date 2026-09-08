import asyncio

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fair.providers.base import (
    AuthenticationFailed,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.mock import MockAdapter
from fair.schemas.api import SolveRequest
from fair.schemas.db import (
    AuditEvent,
    EscalationRecord,
    Model,
    ModelTaskPerformance,
    RoutingAttempt,
    TaskRequest,
)


def spec(name, model_id=None, group=None, **changes):
    return provider(
        name,
        models=[
            {
                "model_id": model_id or f"model_{name}",
                "context_window": 32768,
                "capabilities": ["coding", "structured_output"],
                "independence_group": group,
            }
        ],
        **changes,
    )


def request(**changes):
    return SolveRequest(
        **(
            {
                "client_id": "alice",
                "task": "Calculate the expression",
                "validation": {"kind": "arithmetic", "expression": "0.1 + 0.2"},
                "cross_check_required": True,
            }
            | changes
        )
    )


async def test_independent_agreement_returns_primary_with_both_attempts(make_router):
    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), MockAdapter("b", text="3/10"))]
    )
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.output == "0.3" and result.provider_id == "a"
    assert result.cross_check.state == "PASSED" and result.cross_check.attempts_count == 1
    assert result.model_disagreement == "NONE"
    assert result.cross_check.agreement_basis == "EXACT_VALUE"
    assert result.verification_state == "DETERMINISTIC_ARITHMETIC"
    assert [a.role for a in result.attempts] == ["PRIMARY", "CROSS_CHECK"]
    assert result.paid_inference_executed is False
    with router.sessions() as session:
        assert (
            session.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "CROSS_CHECK_COMPLETED")
            ).payload_json["state"]
            == "PASSED"
        )
        assert session.get(TaskRequest, result.request_id).result_json == result.model_dump(
            mode="json"
        )


async def test_high_impact_cannot_opt_out(make_router):
    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), MockAdapter("b", text="0.3"))]
    )
    result = await router.solve(
        request(quality_level="high_impact_support", cross_check_required=False)
    )
    assert result.status == "ACCEPTED" and result.cross_check.required
    assert result.minimum_required == 92 and result.cross_check.state == "PASSED"


async def test_noncritical_default_still_uses_single_attempt(make_router):
    a, b = MockAdapter("a", text="0.3"), MockAdapter("b", text="0.3")
    router = make_router([(spec("a"), a), (spec("b"), b)])
    result = await router.solve(request(cross_check_required=False))
    assert result.status == "ACCEPTED" and b.calls == 0
    assert result.cross_check.state == "NOT_REQUESTED"


@pytest.mark.parametrize("mode", ["same_model", "same_group", "same_provider"])
async def test_aliases_and_same_provider_cannot_self_verify(make_router, mode):
    if mode == "same_provider":
        primary = spec("a")
        primary.models.append(primary.models[0].model_copy(update={"model_id": "another_model"}))
        entries = [(primary, MockAdapter("a", text="0.3"))]
    else:
        entries = [
            (
                spec(
                    name,
                    model_id="same_model" if mode == "same_model" else None,
                    group="shared-family" if mode == "same_group" else None,
                ),
                MockAdapter(name, text="0.3"),
            )
            for name in ("a", "b")
        ]
    router = make_router(entries)
    result = await router.solve(request())
    assert result.status == "ESCALATION_REQUIRED" and result.output is None
    assert result.reason_code == "INDEPENDENT_VERIFIER_UNAVAILABLE"
    assert len(result.attempts) == 1


async def test_group_snapshot_persisted(make_router):
    router = make_router([(spec("a", group="family-a"), MockAdapter("a"))])
    with router.sessions() as session:
        assert session.get(Model, ("a", "model_a")).independence_group == "family-a"


@pytest.mark.parametrize(
    "error", [ProviderUnavailable(), RateLimited(), QuotaExceeded(), AuthenticationFailed()]
)
async def test_verifier_infrastructure_failure_can_try_another_provider(make_router, error):
    router = make_router(
        [
            (spec("a"), MockAdapter("a", text="0.3")),
            (spec("b"), MockAdapter("b", error=error)),
            (spec("c"), MockAdapter("c", text="0.3")),
        ]
    )
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and len(result.attempts) == 3
    assert result.cross_check.attempts_count == 2
    assert result.cross_check.verification_attempt_number == 3
    with router.sessions() as session:
        row = session.get(ModelTaskPerformance, ("b", "model_b", "arithmetic"))
        assert row.quality_samples == row.quality_failures == 0


async def test_verification_budget_is_separate_and_bounded(make_router):
    entries = [
        (spec("a"), MockAdapter("a", error=ProviderUnavailable())),
        (spec("b"), MockAdapter("b", text="0.3")),
    ]
    entries += [(spec(name), MockAdapter(name, error=RateLimited())) for name in ("c", "d", "e")]
    router = make_router(entries, max_attempts=2, max_verification_attempts=2)
    result = await router.solve(request())
    assert len(result.attempts) == 4 and result.cross_check.attempts_count == 2
    assert result.reason_code == "INDEPENDENT_VERIFIER_UNAVAILABLE"
    assert router.registry.adapters["e"].calls == 0


async def test_disagreement_withholds_output_without_shopping_for_agreement(make_router):
    router = make_router(
        [
            (spec("a"), MockAdapter("a", text="0.3")),
            (spec("b"), MockAdapter("b", text="0.4")),
            (spec("c"), MockAdapter("c", text="0.3")),
        ]
    )
    result = await router.solve(request())
    assert result.status == "ESCALATION_REQUIRED" and result.reason_code == "MODEL_DISAGREEMENT"
    assert result.output is None and result.provider_id is None and result.quality is None
    assert result.model_disagreement == "DETECTED" and result.best_quality_score == 100
    assert result.cross_check.state == "DISAGREEMENT" and router.registry.adapters["c"].calls == 0
    assert "0.4" not in result.model_dump_json()
    with router.sessions() as session:
        assert (
            session.get(EscalationRecord, result.request_id).detail_json["cross_check"]["state"]
            == "DISAGREEMENT"
        )


@pytest.mark.parametrize("verifier_text", ["garbage", "0.3 followed by extra claims"])
async def test_failed_verifier_never_approves_primary(make_router, verifier_text):
    router = make_router(
        [
            (spec("a"), MockAdapter("a", text="0.3")),
            (spec("b"), MockAdapter("b", text=verifier_text)),
        ]
    )
    result = await router.solve(request())
    assert result.status == "ESCALATION_REQUIRED" and result.reason_code == "CROSS_CHECK_REJECTED"
    assert result.model_disagreement == "NOT_ASSESSED"


async def test_matching_text_does_not_override_citation_hard_reject(make_router):
    class Fabricated(MockAdapter):
        async def complete(self, value):
            response = await super().complete(value)
            response.citations = [{"source_id": "fake", "quote": "invented"}]
            return response

    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), Fabricated("b", text="0.3"))]
    )
    result = await router.solve(request())
    assert result.status == "ESCALATION_REQUIRED" and result.cross_check.state == "REJECTED"
    assert result.model_disagreement == "NONE"


async def test_matching_unsupported_prose_is_not_verified(make_router):
    router = make_router(
        [
            (spec("a"), MockAdapter("a", text="identical claim")),
            (spec("b"), MockAdapter("b", text="identical claim")),
        ]
    )
    result = await router.solve(request(validation=None, quality_level="high_impact_support"))
    assert result.status == "ESCALATION_REQUIRED" and result.cross_check.state == "NOT_RUN"
    assert result.model_disagreement == "NOT_ASSESSED"
    assert all(attempt.role == "PRIMARY" for attempt in result.attempts)


async def test_different_passing_code_compared_by_hidden_tests_without_answer_leak(make_router):
    first = "def solve(x):\n    # secret-producer-comment\n    return x + 1"

    class Blind(MockAdapter):
        async def complete(self, value):
            assert "secret-producer-comment" not in value.model_dump_json()
            assert "987654" not in value.model_dump_json()
            return await super().complete(value)

    router = make_router(
        [
            (spec("a"), MockAdapter("a", text=first)),
            (spec("b"), Blind("b", text="def solve(x):\n    return 1 + x")),
        ]
    )
    task = request(
        task="Implement increment",
        validation={
            "kind": "python_function",
            "function_name": "solve",
            "cases": [
                {"arguments": [987654], "expected": 987655},
                {"arguments": [-1], "expected": 0},
            ],
        },
    )
    result = await router.solve(task)
    assert result.status == "ACCEPTED" and result.output == first
    assert result.cross_check.agreement_basis == "HOST_TEST_CASES"


@pytest.mark.parametrize("blocked", ["paid", "terms", "privacy", "quota", "capability"])
async def test_verifier_must_pass_all_routing_gates(make_router, blocked):
    a, b = spec("a", max_data_class="RESTRICTED"), spec("b", max_data_class="RESTRICTED")
    router = make_router([(a, MockAdapter("a", text="0.3")), (b, MockAdapter("b", text="0.3"))])
    verifier = router.registry.providers["b"]
    changes = {}
    if blocked == "paid":
        verifier.current_access_cost_usd = 1
    elif blocked == "terms":
        verifier.status = "TERMS_REVIEW"
    elif blocked == "quota":
        router.quota.exhaust("b")
    elif blocked == "privacy":
        verifier.max_data_class = "PUBLIC"
        changes["privacy_class"] = "CONFIDENTIAL"
    else:
        verifier.models[0].capabilities.clear()
        changes["required_capabilities"] = {"structured_output"}
    result = await router.solve(request(**changes))
    assert result.status == "ESCALATION_REQUIRED" and router.registry.adapters["b"].calls == 0


async def test_kill_switch_blocks_cross_check_after_primary(make_router):
    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), MockAdapter("b", text="0.3"))]
    )

    class Stop(MockAdapter):
        async def complete(self, value):
            result = await super().complete(value)
            router.stopped = True
            return result

    router.registry.adapters["a"] = Stop("a", text="0.3")
    result = await router.solve(request())
    assert result.reason_code == "SYSTEM_STOPPED" and result.cross_check.state == "STOPPED"
    assert result.output is None and router.registry.adapters["b"].calls == 0


async def test_validator_error_in_cross_check_is_service_failure(make_router, monkeypatch):
    from fair.quality.engine import evaluate

    def evaluator(req, profile, response):
        if response.provider_id == "b":
            raise RuntimeError("sensitive")
        return evaluate(req, profile, response)

    monkeypatch.setattr("fair.router.orchestrator.evaluate", evaluator)
    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), MockAdapter("b", text="0.3"))]
    )
    result = await router.solve(request())
    assert result.status == "FAILED" and result.cross_check.state == "SERVICE_FAILED"
    assert result.output is None and "sensitive" not in result.model_dump_json()


async def test_cancelled_cross_check_has_durable_lineage(make_router):
    started, release = asyncio.Event(), asyncio.Event()

    class Wait(MockAdapter):
        async def complete(self, value):
            started.set()
            await release.wait()

    router = make_router([(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), Wait("b"))])
    job = asyncio.create_task(router.solve(request()))
    await asyncio.wait_for(started.wait(), 2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    with router.sessions() as session:
        assert session.scalar(select(TaskRequest)).status == "CANCELLED"
        details = [row.detail_json for row in session.scalars(select(RoutingAttempt))]
        assert any(d["role"] == "CROSS_CHECK" and d["disposition"] == "CANCELLED" for d in details)


def test_cross_check_http_history_stays_client_isolated(make_router):
    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), MockAdapter("b", text="0.3"))]
    )
    with TestClient(
        create_app(router, {"alice": "alice-key", "bob": "bob-key"}, "admin")
    ) as client:
        result = client.post(
            "/v1/solve", headers={"X-API-Key": "alice-key"}, json=request().model_dump(mode="json")
        )
        assert result.status_code == 200 and result.json()["cross_check"]["state"] == "PASSED"
        path = "/v1/requests/" + result.json()["request_id"]
        assert client.get(path, headers={"X-API-Key": "alice-key"}).json() == result.json()
        assert client.get(path + "/audit", headers={"X-API-Key": "bob-key"}).status_code == 404


async def test_comparison_service_failure_withholds_primary(make_router, monkeypatch):
    def broken(*args):
        raise RuntimeError("private-comparison-error")

    monkeypatch.setattr("fair.router.orchestrator.compare", broken)
    router = make_router(
        [(spec("a"), MockAdapter("a", text="0.3")), (spec("b"), MockAdapter("b", text="0.3"))]
    )
    result = await router.solve(request())
    assert result.status == "FAILED" and result.cross_check.state == "SERVICE_FAILED"
    assert result.output is None and "private-comparison-error" not in result.model_dump_json()


async def test_reference_json_cross_check_ignores_object_key_order(make_router):
    router = make_router(
        [
            (spec("a"), MockAdapter("a", text='{"a":1,"b":2}')),
            (spec("b"), MockAdapter("b", text='{"b":2,"a":1}')),
        ]
    )
    task = request(
        task="Extract JSON", validation={"kind": "reference_json", "expected": {"a": 1, "b": 2}}
    )
    result = await router.solve(task)
    assert result.status == "ACCEPTED" and result.cross_check.agreement_basis == "EXACT_VALUE"
