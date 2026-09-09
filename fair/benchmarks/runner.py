"""Replay saved responses without credentials, provider calls or production learning."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, Field, StrictBool, model_validator

from fair.benchmarks.contracts import BenchmarkMetrics, BenchmarkRun, Identity
from fair.classifier.task_profiler import profile_task
from fair.quality.engine import acceptable, evaluate
from fair.quality.json_data import strict_json
from fair.quality.thresholds import DEFAULT_THRESHOLDS, validate_thresholds
from fair.quality.version import ENGINE_VERSION
from fair.schemas.api import SolveRequest
from fair.schemas.domain import DTO, NormalizedModelResponse


class BenchmarkCase(DTO):
    case_id: Identity
    split: Literal["calibration", "holdout"]
    request: SolveRequest
    outcome: Literal["RESPONSE", "INFRA_FAILURE", "QUOTA_FAILURE", "VALIDATOR_FAILURE"] = "RESPONSE"
    response: NormalizedModelResponse | None = None
    expected_acceptable: StrictBool | None = None

    @model_validator(mode="after")
    def labelled_response(self):
        if self.outcome == "RESPONSE":
            if self.response is None or self.expected_acceptable is None:
                raise ValueError("Responses require independent acceptance labels")
        elif self.response is not None or self.expected_acceptable is not None:
            raise ValueError("Service/quota outcomes must not contain answers or quality labels")
        # This runner evaluates one response, not final cross-check or source-registry policy.
        if (
            self.request.cross_check_required
            or self.request.quality_level == "high_impact_support"
            or self.request.source_policy is not None
        ):
            raise ValueError(
                "Replay evaluates local contracts only; policy/cross-check requests are unsupported"
            )
        return self


class BenchmarkDataset(DTO):
    suite_id: Identity
    suite_revision: Identity
    purpose: Literal["FIXTURE", "WORKLOAD"]
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: Identity
    observed_at: AwareDatetime
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def distinct_cases(self):
        ids, tasks = set(), set()
        for case in self.cases:
            if case.case_id in ids:
                raise ValueError("Duplicate benchmark case ID")
            ids.add(case.case_id)
            # Ignore caller identity and threshold level when detecting duplicate trials.
            excluded = {"client_id", "quality_level"}
            if case.request.validation is not None:
                excluded.add("task")
            content = case.request.model_dump(exclude=excluded)
            digest = fingerprint(content)
            if digest in tasks:
                raise ValueError("Duplicate tasks cannot inflate samples or cross split boundaries")
            tasks.add(digest)
            if case.response is not None and (
                case.response.provider_id,
                case.response.model_id,
            ) != (self.provider_id, self.model_id):
                raise ValueError("Benchmark response identity mismatch")
        return self


def fingerprint(value):
    def stable(item):
        if isinstance(item, dict):
            return {key: stable(value) for key, value in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(stable(value) for value in item)
        if isinstance(item, list):
            return [stable(value) for value in item]
        return item

    return hashlib.sha256(
        json.dumps(stable(value), sort_keys=True, allow_nan=False, default=str).encode()
    ).hexdigest()


def run_benchmark(dataset, *, now=None, thresholds=None):
    dataset = BenchmarkDataset.model_validate(dataset)
    now = now or datetime.now(UTC)
    groups, predictions = {}, []
    thresholds = validate_thresholds(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    for case in dataset.cases:
        profile = profile_task(case.request, thresholds)
        key = (profile.task_class, case.split)
        if key not in groups:
            groups[key] = dict(
                task_class=key[0],
                split=key[1],
                cases=0,
                samples=0,
                successes=0,
                false_accepts=0,
                false_rejects=0,
                unverified=0,
                infrastructure_failures=0,
                quota_failures=0,
                validator_failures=0,
            )
        group = groups[key]
        group["cases"] += 1
        if case.outcome != "RESPONSE":
            group[
                {
                    "INFRA_FAILURE": "infrastructure_failures",
                    "QUOTA_FAILURE": "quota_failures",
                    "VALIDATOR_FAILURE": "validator_failures",
                }[case.outcome]
            ] += 1
            continue
        report = evaluate(case.request, profile, case.response)
        predicted = acceptable(report, profile)
        predictions.append(
            (key, report.overall_score, report.hard_reject, predicted, case.expected_acceptable)
        )
        if report.overall_score is None:
            group["unverified"] += 1
            continue
        group["samples"] += 1
        group["successes"] += int(predicted and case.expected_acceptable)
        group["false_accepts"] += int(predicted and not case.expected_acceptable)
        group["false_rejects"] += int(not predicted and case.expected_acceptable)
    metrics = [BenchmarkMetrics(**groups[key]) for key in sorted(groups)]
    # Canonical JSON-mode sets are explicitly sorted for repeatable dataset fingerprints.
    canonical_dataset = dataset.model_dump(mode="json")
    for case, value in zip(dataset.cases, canonical_dataset["cases"], strict=True):
        value["request"]["required_capabilities"] = sorted(case.request.required_capabilities)
    run = BenchmarkRun(
        **dataset.model_dump(exclude={"cases"}),
        generated_at=now,
        engine_version=ENGINE_VERSION,
        dataset_fingerprint=fingerprint(canonical_dataset),
        metrics=metrics,
    )
    diagnostics = []
    for metric in metrics:
        rows = [row for row in predictions if row[0] == (metric.task_class, metric.split)]
        measured = [row for row in rows if row[1] is not None]
        sweeps = []
        for level, threshold in thresholds.items():
            chosen = [row for row in measured if row[3] and not row[2] and row[1] >= threshold]
            sweeps.append(
                {
                    "quality_level": level,
                    "threshold": threshold,
                    "accepted": len(chosen),
                    "false_accepts": sum(not row[4] for row in chosen),
                }
            )
        diagnostics.append(
            {
                "task_class": metric.task_class,
                "split": metric.split,
                **metric.estimate(),
                "brier_score": sum((row[1] / 100 - row[4]) ** 2 for row in measured) / len(measured)
                if measured
                else None,
                "threshold_sweep": sweeps,
            }
        )
    return {
        "run": run.model_dump(mode="json"),
        "diagnostics": diagnostics,
        "scope": "LOCAL_CONTRACT_REPLAY",
        "provider_calls": 0,
        "note": "Contract scores are binary test results. Benchmark pass rates describe this workload only; fixtures cannot qualify routes.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--thresholds", type=Path, help="Optional quality threshold YAML file")
    args = parser.parse_args()
    try:
        if args.dataset.stat().st_size > 20_000_000:
            raise ValueError("Dataset exceeds budget")
        thresholds = None
        if args.thresholds:
            if args.thresholds.stat().st_size > 10000:
                raise ValueError("Threshold file exceeds budget")
            thresholds = yaml.safe_load(args.thresholds.read_text(encoding="utf-8"))
        result = run_benchmark(
            strict_json(args.dataset.read_text(encoding="utf-8")), thresholds=thresholds
        )
        # Exclusive creation prevents accidentally replacing operator review evidence.
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, indent=2, allow_nan=False)
            output.write("\n")
    except Exception:
        parser.exit(
            2,
            "Benchmark failed: check dataset validity, file size and output path. Input contents are withheld.\n",
        )
    print("Offline benchmark report written; no providers called and no route approvals created.")


if __name__ == "__main__":
    main()
