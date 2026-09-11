import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from apps.api.main import create_app
from fair.classifier.task_profiler import profile_task
from fair.providers.base import ProviderUnavailable, RateLimited
from fair.providers.mock import MockAdapter
from fair.quality.engine import evaluate
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import EscalationRecord, ModelTaskPerformance, QualityRecord
from fair.schemas.domain import NormalizedModelResponse


def arithmetic(**changes):
    return SolveRequest(
        **(
            {
                "client_id": "alice",
                "task": "Calculate the expression",
                "validation": {"kind": "arithmetic", "expression": "0.1 + 0.2"},
            }
            | changes
        )
    )


def reference(**changes):
    return SolveRequest(
        **(
            {
                "client_id": "alice",
                "task": "Extract the answer as JSON",
                "validation": {"kind": "reference_json", "expected": {"answer": 42}},
            }
            | changes
        )
    )


def capable(name):
    return provider(
        name,
        models=[
            {
                "model_id": "model",
                "context_window": 32768,
                "capabilities": ["structured_output", "coding"],
            }
        ],
    )


@pytest.mark.parametrize(
    "expression,answer",
    [("0.1 + 0.2", "0.3"), ("1 / 3", "1/3"), ("(20 - 2) / 3", "6"), ("-3 * (4 + 2)", "-18")],
)
async def test_arithmetic_acceptance(make_router, expression, answer):
    router = make_router([(provider(), MockAdapter("a", text=answer))])
    result = await router.solve(
        arithmetic(validation={"kind": "arithmetic", "expression": expression})
    )
    assert result.status == "ACCEPTED"
    assert result.output == answer
    assert result.verification_state == "DETERMINISTIC_ARITHMETIC"
    assert result.quality.overall_score == 100
    assert result.quality.validation_fingerprint
    assert result.paid_inference_executed is False
    with router.sessions() as session:
        assert session.scalar(select(QualityRecord)).overall_score == 100
        assert session.get(EscalationRecord, result.request_id) is None


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "2 ** 100",
        "2 // 1",
        "1 / 0",
        "1e100",
        "[1,2]",
        "True",
        "(" * 30 + "1" + ")" * 30,
        "1+" * 50 + "1",
    ],
)
def test_unsafe_or_excessive_arithmetic_contract_rejected(expression):
    with pytest.raises((ValueError, ValidationError)):
        # Parentheses alone do not increase AST depth; use an excessive length for that case.
        if expression.startswith("(" * 30):
            expression = "(" * 65 + "1" + ")" * 65
        arithmetic(validation={"kind": "arithmetic", "expression": expression})


async def test_wrong_answer_retries_different_model_then_accepts(make_router):
    bad, good = MockAdapter("a", text="0.4"), MockAdapter("b", text="0.3")
    router = make_router([(provider("a"), bad), (provider("b"), good)])
    result = await router.solve(arithmetic())
    assert result.status == "ACCEPTED" and result.provider_id == "b"
    assert result.attempts[0].quality.hard_reject
    assert [a.disposition for a in result.attempts] == ["QUALITY_FAILURE", "ACCEPTED"]
    assert bad.calls == good.calls == 1
    assert "0.4" not in result.model_dump_json()


@pytest.mark.parametrize(
    "bad", ["1", "NaN", "Infinity", "0.3 with unsupported extra claims", "1/0"]
)
async def test_invalid_answers_escalate_without_returning_text(make_router, bad):
    router = make_router([(provider(), MockAdapter("a", text=bad))])
    result = await router.solve(arithmetic())
    assert result.status == "ESCALATION_REQUIRED"
    assert result.output is None and result.provider_id is None
    assert result.reason_code == "ALL_FREE_MODELS_FAILED_QUALITY"
    with router.sessions() as session:
        assert (
            session.get(EscalationRecord, result.request_id).detail_json["minimum_required"] == 82
        )


async def test_reference_json_accepts_only_exact_content(make_router):
    router = make_router([(capable("a"), MockAdapter("a", text='{"answer": 42}'))])
    result = await router.solve(reference())
    assert result.status == "ACCEPTED"
    assert result.verification_state == "HOST_REFERENCE_MATCH"


@pytest.mark.parametrize(
    "text",
    [
        '{"answer":true}',
        '{"answer":42,"answer":0}',
        '{"answer":42.0}',
        '{"answer":42,"extra":"claim"}',
        '{"answer":NaN}',
        '{"answer":1e999}',
    ],
)
async def test_json_type_duplicates_extra_fields_and_nonfinite_fail(make_router, text):
    router = make_router([(capable("a"), MockAdapter("a", text=text))])
    assert (await router.solve(reference())).status == "ESCALATION_REQUIRED"


async def test_schema_hard_reject_overrides_correct_reference(make_router):
    router = make_router([(capable("a"), MockAdapter("a", text='{"answer":42}'))])
    result = await router.solve(reference(expected_schema={"required": ["missing"]}))
    assert result.status == "ESCALATION_REQUIRED"
    assert "SCHEMA_FAILURE" in result.attempts[0].quality.reject_reasons


@pytest.mark.parametrize(
    "source,quote,reason",
    [("invented", "hello", "FABRICATED_CITATION"), ("known", "made up", "UNSUPPORTED_CITATION")],
)
def test_citation_hard_reject_overrides_correct_arithmetic(source, quote, reason):
    request = arithmetic(evidence=[{"source_id": "known", "text": "hello"}])
    response = NormalizedModelResponse(
        provider_id="a",
        model_id="model",
        text="0.3",
        citations=[{"source_id": source, "quote": quote}],
    )
    report = evaluate(request, profile_task(request, {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}), response)
    assert report.hard_reject and report.overall_score == 0
    assert reason in report.reject_reasons


def test_structured_contradiction_hard_rejected():
    request = arithmetic()
    response = NormalizedModelResponse(
        provider_id="a",
        model_id="model",
        text="0.3",
        assertions=[
            {"subject": "sample", "predicate": "color", "value": "red"},
            {"subject": "Sample", "predicate": "color", "value": "blue"},
        ],
    )
    report = evaluate(request, profile_task(request, {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92}), response)
    assert "MATERIAL_CONTRADICTION" in report.reject_reasons


@pytest.mark.parametrize(
    "changes",
    [
        {"quality_level": "high_impact_support"},
        {"freshness_required": True},
        {"task_type": "research"},
        {"task_type": "coding"},
    ],
)
async def test_uncovered_tasks_do_not_become_verified(make_router, changes):
    router = make_router([(capable("a"), MockAdapter("a", text="0.3"))])
    result = await router.solve(arithmetic(**changes))
    assert result.status == "ESCALATION_REQUIRED"
    assert result.verification_state == "UNVERIFIED"


async def test_learning_changes_ranking_and_survives_router_restart(make_router):
    router = make_router(
        [
            (provider("a"), MockAdapter("a", text="0.4")),
            (provider("b"), MockAdapter("b", text="0.3")),
        ]
    )
    await router.solve(arithmetic())
    restarted = Router(router.registry, router.settings, router.thresholds, router.sessions)
    result = await restarted.solve(arithmetic())
    assert len(result.attempts) == 1 and result.provider_id == "b"
    assert (await restarted.performance.scores("b", "model", "arithmetic"))[0] > 0.5
    assert (await restarted.performance.scores("a", "model", "arithmetic"))[0] < 0.5
    assert (await restarted.performance.scores("a", "model", "general"))[0] == 0.5


@pytest.mark.parametrize("error", [RateLimited(), ProviderUnavailable()])
async def test_infrastructure_does_not_reduce_measured_quality(make_router, error):
    adapter = MockAdapter("a", text="0.3")
    router = make_router([(provider(), adapter)])
    await router.solve(arithmetic())
    before = (await router.performance.scores("a", "model", "arithmetic"))[0]
    adapter.error = error
    await router.solve(arithmetic())
    assert (await router.performance.scores("a", "model", "arithmetic"))[0] == before
    with router.sessions() as session:
        row = session.get(ModelTaskPerformance, ("a", "model", "arithmetic"))
        assert row.quality_samples == 1 and row.quality_sum == 100


async def test_missing_validator_not_counted_as_bad_model(make_router):
    router = make_router()
    result = await router.solve(SolveRequest(client_id="alice", task="Hello"))
    assert result.attempts[0].disposition == "UNVERIFIED"
    with router.sessions() as session:
        row = session.get(ModelTaskPerformance, ("a", "model", "general"))
        assert row.quality_samples == row.quality_failures == 0
        assert row.unverified == 1


def test_accepted_result_is_private_and_performance_is_admin_only(make_router):
    router = make_router([(provider(), MockAdapter("a", text="0.3"))])
    with TestClient(
        create_app(router, {"alice": "alice-key", "bob": "bob-key"}, "admin-key")
    ) as client:
        result = client.post(
            "/v1/solve",
            headers={"X-API-Key": "alice-key"},
            json=arithmetic().model_dump(mode="json"),
        )
        assert result.json()["status"] == "ACCEPTED"
        path = "/v1/requests/" + result.json()["request_id"]
        assert client.get(path, headers={"X-API-Key": "bob-key"}).status_code == 404
        assert (
            client.get("/v1/models/performance", headers={"X-API-Key": "alice-key"}).status_code
            == 403
        )
        metrics = client.get("/v1/models/performance", headers={"X-API-Key": "admin-key"}).json()
        assert metrics[0]["average_quality"] == 100


async def test_reference_not_sent_to_model_or_audit(make_router):
    class Capture(MockAdapter):
        async def complete(self, request):
            assert "private-reference" not in request.model_dump_json()
            return await super().complete(request)

    router = make_router([(capable("a"), Capture("a", text='"wrong"'))])
    result = await router.solve(
        reference(validation={"kind": "reference_json", "expected": "private-reference"})
    )
    assert "private-reference" not in result.model_dump_json()


def test_schema_references_rejected_for_direct_core_users():
    with pytest.raises(ValidationError):
        arithmetic(expected_schema={"$ref": "https://example.com/private"})


def test_invalid_contract_returns_422_before_provider_call(make_router):
    router = make_router()
    with TestClient(create_app(router, {"alice": "key"}, "admin")) as client:
        body = arithmetic().model_dump(mode="json")
        body["validation"]["expression"] = "2**999999"
        assert client.post("/v1/solve", headers={"X-API-Key": "key"}, json=body).status_code == 422
    assert router.registry.adapters["a"].calls == 0


async def test_hallucinating_mock_never_returned_and_penalty_persisted(make_router):
    class Hallucinating(MockAdapter):
        async def complete(self, request):
            response = await super().complete(request)
            response.citations = [{"source_id": "fabricated", "quote": "unsupported"}]
            return response

    router = make_router(
        [
            (provider("a"), Hallucinating("a", text="0.3")),
            (provider("b"), MockAdapter("b", text="0.3")),
        ]
    )
    result = await router.solve(arithmetic())
    assert result.status == "ACCEPTED" and result.provider_id == "b"
    assert result.attempts[0].quality.reject_reasons == ["FABRICATED_CITATION"]
    with router.sessions() as session:
        row = session.get(ModelTaskPerformance, ("a", "model", "arithmetic"))
        assert row.hallucination_events == row.quality_failures == 1


async def test_validator_failure_is_not_a_provider_failure(make_router, monkeypatch):
    def broken(*args):
        raise RuntimeError("sensitive-validator-error")

    monkeypatch.setattr("fair.router.orchestrator.evaluate", broken)
    router = make_router([(provider(), MockAdapter("a", text="0.3"))])
    result = await router.solve(arithmetic())
    assert result.status == "FAILED" and result.reason_code == "VALIDATION_SERVICE_FAILED"
    assert result.output is None
    assert "sensitive-validator-error" not in result.model_dump_json()
    with router.sessions() as session:
        row = session.get(ModelTaskPerformance, ("a", "model", "arithmetic"))
        assert row.quality_failures == row.infra_failures == row.quality_samples == 0
