import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from apps.api.main import create_app
from fair.benchmarks.contracts import BenchmarkApproval, BenchmarkMetrics, BenchmarkPolicy, wilson
from fair.benchmarks.registry import BenchmarkRegistry
from fair.benchmarks.runner import BenchmarkDataset, run_benchmark
from fair.classifier.task_profiler import profile_task
from fair.providers.mock import MockAdapter
from fair.quality.thresholds import validate_thresholds
from fair.quality.version import ENGINE_VERSION
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import AuditEvent, Model, ModelTaskPerformance

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def request(**changes):
    return SolveRequest(
        **(
            {
                "client_id": "alice",
                "task": "Calculate the expression",
                "validation": {"kind": "arithmetic", "expression": "1+1"},
            }
            | changes
        )
    )


def case(number=0, **changes):
    return {
        "case_id": f"case-{number}",
        "split": "calibration" if number % 2 == 0 else "holdout",
        "request": request(
            validation={"kind": "arithmetic", "expression": f"{number}+1"}
        ).model_dump(mode="json"),
        "response": {"provider_id": "a", "model_id": "model", "text": str(number + 1)},
        "expected_acceptable": True,
        **changes,
    }


def dataset(**changes):
    return {
        "suite_id": "host-arithmetic",
        "suite_revision": "v1",
        "purpose": "FIXTURE",
        "provider_id": "a",
        "model_id": "model",
        "model_revision": "revision-one",
        "observed_at": NOW - timedelta(hours=2),
        "cases": [case(), case(1)],
        **changes,
    }


def approval(samples=100, successes=None, **changes):
    # Synthetic aggregates used only by tests exercising operator-approved workload gates.
    metric = dict(
        cases=samples,
        samples=samples,
        successes=samples if successes is None else successes,
        false_accepts=0,
        false_rejects=0,
        unverified=0,
        infrastructure_failures=0,
        quota_failures=0,
        validator_failures=0,
        task_class="arithmetic",
    )
    run = run_benchmark(dataset(), now=NOW - timedelta(hours=1))["run"]
    run.update(
        purpose="WORKLOAD",
        metrics=[dict(metric, split=split) for split in ("calibration", "holdout")],
    )
    return BenchmarkApproval.model_validate(
        {
            "run": run,
            "client_ids": ["alice"],
            "reviewer_id": "operator",
            "review_note": "Private representative-workload review",
            "representative_workload": True,
            "approved_at": NOW,
            "expires_at": NOW + timedelta(days=1),
            **changes,
        }
    )


def spec(name="a", model_id="model", **changes):
    return provider(
        name,
        models=[{"model_id": model_id, "model_revision": "revision-one", "context_window": 32768}],
        **changes,
    )


def assessed(record=None, req=None, **policy):
    req = req or request()
    registry = BenchmarkRegistry([record or approval()], clock=lambda: NOW)
    model = spec()
    return registry.assess(
        req,
        profile_task(req, {"standard": 82, "advanced": 88}),
        model,
        model.models[0],
        BenchmarkPolicy(**policy),
    )


def attach(router, records):
    registry = BenchmarkRegistry(records, clock=lambda: NOW)
    router.benchmarks = router.selector.benchmarks = registry
    return router


def test_wilson_known_values_and_no_invented_estimate():
    assert wilson(0, 0) == (None, None)
    assert wilson(50, 100) == pytest.approx((0.40383153, 0.59616847))
    assert wilson(100, 100)[0] == pytest.approx(0.9630065)
    assert wilson(1, 1)[0] < 0.21
    assert wilson(0, 100)[0] == 0
    with pytest.raises(ValueError):
        wilson(2, 1)


def test_runner_scores_labels_and_keeps_splits_separate():
    items = [
        case(),
        case(1, expected_acceptable=False),
        case(2, response={"provider_id": "a", "model_id": "model", "text": "wrong"}),
        case(
            3,
            response={"provider_id": "a", "model_id": "model", "text": "wrong"},
            expected_acceptable=False,
        ),
    ]
    result = run_benchmark(dataset(cases=items), now=NOW)
    calibration, holdout = result["run"]["metrics"]
    assert (
        calibration["samples"] == 2
        and calibration["successes"] == 1
        and calibration["false_rejects"] == 1
    )
    assert holdout["false_accepts"] == 1 and holdout["successes"] == 0
    assert result["provider_calls"] == 0
    assert all(row["brier_score"] == 0.5 for row in result["diagnostics"])
    assert len({row["accepted"] for row in result["diagnostics"][0]["threshold_sweep"]}) == 1


def test_service_failures_and_missing_validation_are_not_quality_samples():
    items = [case()]
    for number, outcome in enumerate(
        ("INFRA_FAILURE", "QUOTA_FAILURE", "VALIDATOR_FAILURE"), start=1
    ):
        items.append(case(number, outcome=outcome, response=None, expected_acceptable=None))
    items.append(
        case(
            4, request=request(validation=None, task="Unknown private task").model_dump(mode="json")
        )
    )
    result = run_benchmark(dataset(cases=items), now=NOW)
    metrics = result["run"]["metrics"]
    assert sum(row["samples"] for row in metrics) == 1
    assert sum(row["successes"] for row in metrics) == 1
    assert sum(row["unverified"] for row in metrics) == 1
    assert "Unknown private task" not in json.dumps(result)


@pytest.mark.parametrize("edit", ["id", "task", "client", "threshold", "prompt"])
def test_duplicate_trials_cannot_inflate_samples_or_leak_across_splits(edit):
    first, second = case(), case(1)
    if edit == "id":
        second["case_id"] = first["case_id"]
    else:
        second["request"] = copy.deepcopy(first["request"])
        if edit == "client":
            second["request"]["client_id"] = "bob"
        if edit == "threshold":
            second["request"]["quality_level"] = "commodity"
        if edit == "prompt":
            second["request"]["task"] = "Another wording of the same contract"
    with pytest.raises(ValidationError):
        BenchmarkDataset.model_validate(dataset(cases=[first, second]))


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_acceptable": None},
        {"expected_acceptable": "true"},
        {"outcome": "INFRA_FAILURE"},
        {"response": {"provider_id": "other", "model_id": "model", "text": "1"}},
    ],
)
def test_invalid_or_unlabelled_cases_rejected(changes):
    with pytest.raises(ValidationError):
        run_benchmark(dataset(cases=[case(**changes)]), now=NOW)


@pytest.mark.parametrize(
    "changes", [{"cross_check_required": True}, {"quality_level": "high_impact_support"}]
)
def test_replay_does_not_claim_end_to_end_policy_validation(changes):
    with pytest.raises(ValidationError):
        run_benchmark(
            dataset(cases=[case(request=request(**changes).model_dump(mode="json"))]), now=NOW
        )


def test_fingerprint_reproducible_and_sensitive_to_labels():
    first = run_benchmark(dataset(), now=NOW)
    second = run_benchmark(dataset(), now=NOW + timedelta(hours=1))
    assert first["run"]["dataset_fingerprint"] == second["run"]["dataset_fingerprint"]
    changed = run_benchmark(dataset(cases=[case(expected_acceptable=False), case(1)]), now=NOW)
    assert first["run"]["dataset_fingerprint"] != changed["run"]["dataset_fingerprint"]


@pytest.mark.parametrize(
    "mode,state",
    [
        ("fixture", "FIXTURE"),
        ("unreviewed", "UNREVIEWED"),
        ("revision", "VERSION_MISMATCH"),
        ("engine", "VERSION_MISMATCH"),
        ("expired", "EXPIRED"),
        ("future", "EXPIRED"),
        ("stale", "EXPIRED"),
        ("client", "UNAVAILABLE"),
        ("task", "UNAVAILABLE"),
    ],
)
def test_invalid_qualification_cannot_rate_a_route(mode, state):
    record = approval().model_dump(mode="json")
    if mode == "fixture":
        record["run"]["purpose"] = "FIXTURE"
    if mode == "unreviewed":
        record["representative_workload"] = False
    if mode == "revision":
        record["run"]["model_revision"] = "old-version"
    if mode == "engine":
        record["run"]["engine_version"] = "deterministic-v6"
    if mode == "expired":
        record["approved_at"] = NOW - timedelta(minutes=1)
        record["expires_at"] = NOW
    if mode == "future":
        record["approved_at"] = NOW + timedelta(minutes=1)
    if mode == "stale":
        record["run"]["observed_at"] = NOW - timedelta(days=60)
    if mode == "client":
        record["client_ids"] = ["bob"]
    if mode == "task":
        for row in record["run"]["metrics"]:
            row["task_class"] = "reference_json"
    result = assessed(BenchmarkApproval.model_validate(record))
    assert result.state == state and result.conservative_score is None


def test_sparse_success_is_not_certain_and_levels_now_differ():
    assert assessed(approval(samples=1)).state == "INSUFFICIENT"
    # 95/100 has a Wilson lower bound near 88.8%; 90/100 is near 82.6%.
    record = approval(samples=100, successes=90)
    assert assessed(record).state == "PASSED"
    assert assessed(record, request(quality_level="advanced")).state == "BELOW_THRESHOLD"
    assert assessed(approval(samples=100, successes=82)).state == "BELOW_THRESHOLD"


@pytest.mark.parametrize(
    "field,state",
    [
        ("false_accepts", "FALSE_ACCEPTANCE"),
        ("unverified", "VALIDATION_GAP"),
        ("validator_failures", "VALIDATION_GAP"),
    ],
)
def test_holdout_errors_cannot_be_hidden_by_perfect_calibration(field, state):
    record = approval().model_dump(mode="json")
    row = record["run"]["metrics"][1]
    row[field] = 1
    if field == "false_accepts":
        row["successes"] -= 1
    else:
        row["cases"] += 1
    assert assessed(BenchmarkApproval.model_validate(record)).state == state


def test_missing_holdout_and_inconsistent_totals_rejected():
    record = approval().model_dump(mode="json")
    record["run"]["metrics"] = record["run"]["metrics"][:1]
    assert assessed(BenchmarkApproval.model_validate(record)).state == "INSUFFICIENT"
    metric = record["run"]["metrics"][0]
    for changes in ({"samples": 101}, {"successes": 101}, {"samples": True}):
        with pytest.raises(ValidationError):
            BenchmarkMetrics.model_validate(metric | changes)


async def test_missing_qualification_consumes_no_quota_or_model_learning(make_router):
    router = make_router([(spec(), MockAdapter("a", text="2"))], benchmark_policy={})
    result = await router.solve(request())
    assert result.reason_code == "BENCHMARK_QUALIFICATION_UNSATISFIED" and not result.attempts
    assert result.output is None and router.registry.adapters["a"].calls == 0
    assert router.quota.state("a").used == 0
    with router.sessions() as session:
        assert not list(session.scalars(select(ModelTaskPerformance)))


async def test_qualification_changes_ranking_and_reports_evidence(make_router):
    first = approval(successes=90)
    second = approval().model_dump(mode="json")
    second["run"]["provider_id"] = "b"
    router = attach(
        make_router(
            [(spec(), MockAdapter("a", text="2")), (spec("b"), MockAdapter("b", text="2"))],
            benchmark_policy={},
        ),
        [first, second],
    )
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.provider_id == "b" and len(result.attempts) == 1
    assert result.quality.overall_score == 100
    assert all(check.benchmark_fingerprint for check in result.benchmark_checks)
    with router.sessions() as session:
        assert session.get(Model, ("b", "model")).model_revision == "revision-one"
        audits = list(session.scalars(select(AuditEvent)))
    assert any(row.event_type == "BENCHMARK_QUALIFICATION_CHECKED" for row in audits)
    assert "Private representative-workload review" not in json.dumps(
        [row.payload_json for row in audits]
    )


async def test_high_benchmark_rating_never_overrides_wrong_answer(make_router):
    router = attach(
        make_router([(spec(), MockAdapter("a", text="wrong"))], benchmark_policy={}), [approval()]
    )
    result = await router.solve(request())
    assert result.output is None and result.reason_code == "ALL_FREE_MODELS_FAILED_QUALITY"
    assert result.benchmark_checks[0].state == "PASSED"


async def test_expiry_during_inference_withholds_answer(make_router):
    router = attach(
        make_router([(spec(), MockAdapter("a", text="2"))], benchmark_policy={}), [approval()]
    )

    class Expire(MockAdapter):
        async def complete(self, req):
            router.benchmarks.clock = lambda: NOW + timedelta(days=2)
            return await super().complete(req)

    router.registry.adapters["a"] = Expire("a", text="2")
    result = await router.solve(request())
    assert result.output is None and result.reason_code == "BENCHMARK_QUALIFICATION_UNSATISFIED"
    assert result.benchmark_checks[0].state == "EXPIRED"
    assert result.attempts[0].disposition == "ACCEPTED"  # local contract outcome stays separate


async def test_cross_check_requires_qualified_distinct_checker(make_router):
    second = approval().model_dump(mode="json")
    second["run"].update(provider_id="b", model_id="independent")
    router = attach(
        make_router(
            [
                (spec(), MockAdapter("a", text="2")),
                (spec("b", "independent"), MockAdapter("b", text="2")),
            ],
            benchmark_policy={},
        ),
        [approval()],
    )
    result = await router.solve(request(cross_check_required=True))
    assert result.reason_code == "BENCHMARK_QUALIFICATION_UNSATISFIED" and result.output is None
    assert router.registry.adapters["b"].calls == 0
    attach(router, [approval(), second])
    result = await router.solve(request(quality_level="high_impact_support"))
    assert result.status == "ACCEPTED" and result.cross_check.state == "PASSED"


async def test_expiry_of_checker_qualification_prevents_release(make_router):
    second = approval().model_dump(mode="json")
    second["run"].update(provider_id="b", model_id="independent")
    second["expires_at"] = NOW + timedelta(minutes=1)
    router = attach(
        make_router(
            [
                (spec(), MockAdapter("a", text="2")),
                (spec("b", "independent"), MockAdapter("b", text="2")),
            ],
            benchmark_policy={},
        ),
        [approval(), second],
    )

    class Expire(MockAdapter):
        async def complete(self, req):
            router.benchmarks.clock = lambda: NOW + timedelta(minutes=2)
            return await super().complete(req)

    router.registry.adapters["b"] = Expire("b", text="2")
    result = await router.solve(request(cross_check_required=True))
    assert result.output is None and result.benchmark_checks[-1].state == "EXPIRED"


async def test_dispatch_rechecks_even_if_selection_is_bypassed(make_router):
    router = attach(make_router([(spec(), MockAdapter("a", text="2"))], benchmark_policy={}), [])
    selected = router.registry.providers["a"]
    router.selector.candidates = lambda *args: [(1, selected, selected.models[0])]
    result = await router.solve(request())
    assert not result.attempts and router.registry.adapters["a"].calls == 0


async def test_registry_failure_is_private_and_never_dispatches(make_router):
    router = attach(
        make_router([(spec(), MockAdapter("a", text="2"))], benchmark_policy={}), [approval()]
    )

    def broken():
        raise RuntimeError("secret-clock-error")

    router.benchmarks.clock = broken
    result = await router.solve(request())
    assert result.status == "FAILED" and result.benchmark_checks[0].state == "SERVICE_FAILED"
    assert "secret-clock-error" not in result.model_dump_json()
    assert router.registry.adapters["a"].calls == 0


def test_approval_file_restart_and_client_isolation(make_router, tmp_path):
    path = tmp_path / "benchmarks.yaml"
    path.write_text(
        yaml.safe_dump({"approvals": [approval().model_dump(mode="json")]}), encoding="utf-8"
    )
    router = make_router([(spec(), MockAdapter("a", text="2"))], benchmark_policy={})
    restarted = Router(
        router.registry,
        router.settings,
        router.thresholds,
        router.sessions,
        benchmarks=BenchmarkRegistry.from_file(path),
    )
    restarted.benchmarks.clock = lambda: NOW
    with TestClient(create_app(restarted, {"alice": "key", "bob": "other"}, "admin")) as client:
        body = request().model_dump(mode="json")
        result = client.post("/v1/solve", headers={"X-API-Key": "key"}, json=body).json()
        assert result["status"] == "ACCEPTED"
        url = f"/v1/requests/{result['request_id']}"
        assert client.get(url, headers={"X-API-Key": "key"}).json() == result
        assert client.get(url, headers={"X-API-Key": "other"}).status_code == 404
        body["benchmark_policy"] = None
        assert client.post("/v1/solve", headers={"X-API-Key": "key"}, json=body).status_code == 422
    with pytest.raises(ValueError):
        BenchmarkRegistry([approval(), approval()])


def test_cli_writes_private_aggregate_and_refuses_overwrite(tmp_path):
    source, target = tmp_path / "dataset.json", tmp_path / "report.json"
    data = BenchmarkDataset.model_validate(dataset()).model_dump(mode="json")
    data["observed_at"] = "2020-01-01T00:00:00Z"
    source.write_text(json.dumps(data), encoding="utf-8")
    command = [sys.executable, "-m", "fair.benchmarks.runner", str(source), "--output", str(target)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["run"]["engine_version"] == ENGINE_VERSION and "cases" not in report["run"]
    assert subprocess.run(command, capture_output=True).returncode == 2
    source.write_text('{"private-invalid-input":true}', encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2 and "private-invalid-input" not in result.stderr


def test_shipped_fixture_exposes_insufficient_code_tests_without_qualifying_models():
    data = json.loads(Path("benchmarks/offline-fixture.json").read_text(encoding="utf-8"))
    report = run_benchmark(data, now=NOW)
    assert report["run"]["purpose"] == "FIXTURE"
    metrics = {(row["task_class"], row["split"]): row for row in report["run"]["metrics"]}
    assert metrics[("python_function", "holdout")]["false_accepts"] == 1
    assert metrics[("general", "holdout")]["unverified"] == 1
    assert sum(row["cases"] for row in metrics.values()) == 10


async def test_live_failures_reduce_benchmark_ranking_without_changing_qualification(make_router):
    second = approval(successes=90).model_dump(mode="json")
    second["run"]["provider_id"] = "b"
    router = attach(
        make_router(
            [(spec(), MockAdapter("a", text="wrong")), (spec("b"), MockAdapter("b", text="2"))],
            benchmark_policy={},
        ),
        [approval(), second],
    )
    first = await router.solve(request())
    assert [attempt.provider_id for attempt in first.attempts] == ["a", "b"]
    result = await router.solve(request())
    assert result.provider_id == "b" and len(result.attempts) == 1
    assert all(check.state == "PASSED" for check in result.benchmark_checks)


@pytest.mark.parametrize(
    "thresholds",
    [
        {},
        {"standard": -1},
        {"standard": 101},
        {"standard": float("nan")},
        {"standard": True},
        {"standard": 90, "advanced": 80},
        {"unknown": 75},
    ],
)
def test_invalid_threshold_configuration_rejected(thresholds):
    with pytest.raises(ValueError):
        validate_thresholds(thresholds)


def test_custom_threshold_sweep_uses_operator_values():
    report = run_benchmark(dataset(), now=NOW, thresholds={"standard": 85})
    assert report["diagnostics"][0]["threshold_sweep"][0]["threshold"] == 85


async def test_unqualified_alias_does_not_mask_missing_independent_checker(make_router):
    router = attach(
        make_router(
            [(spec(), MockAdapter("a", text="2")), (spec("b"), MockAdapter("b", text="2"))],
            benchmark_policy={},
        ),
        [approval()],
    )
    result = await router.solve(request(cross_check_required=True))
    assert result.reason_code == "INDEPENDENT_VERIFIER_UNAVAILABLE" and result.output is None


def test_numeric_pinned_model_revision_is_supported():
    record = approval().model_dump(mode="json")
    record["run"]["model_revision"] = "2026-09-09"
    BenchmarkApproval.model_validate(record)
    model = spec().models[0]
    model.model_revision = "2026-09-09"
