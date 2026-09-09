# Benchmark calibration and qualification

Step 3 adds offline evaluation of independently labelled responses and optional, server-managed
qualification of routes. Contract results remain 100 (all implemented checks passed), 0 (known
failure), or null (unverified). They are not probabilities of truth. Benchmark scores measure
usable, correctly accepted responses on a specified workload, with uncertainty from sample size.

## Run the offline diagnostic suite

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m fair.benchmarks.runner benchmarks/offline-fixture.json --thresholds config/quality_thresholds.yaml --output benchmark-report.json
```

The output file must not already exist. No provider, API key, production database, native-code
sandbox or network is used. The runner reads saved normalized responses and executes the local
deterministic validators. It never loads an adapter, creates a route approval or updates model
performance. Its JSON report contains aggregates and fingerprints, without prompts, reference
answers, evidence or response text.

The ten handwritten fixture cases cover arithmetic, JSON reference checks, grounded extraction,
bounded Python functions, unsupported prose and a quota failure. One deliberately weak function
contract tests only `increment(1) == 2`; a constant-return implementation passes that test but
fails the independent label for incrementing arbitrary integers. The report exposes that false
accept so the operator can strengthen the host contract. It does not silently relabel the answer.
The fixture is diagnostic evidence about the harness, not a measurement of any AI model.

## Build a workload dataset

Use the fixture file as a format reference. Top-level fields identify the suite and revision,
`purpose` (`FIXTURE` or `WORKLOAD`), exact provider/model/revision, observation time and cases.
Each case contains a unique `case_id`, `split` (`calibration` or `holdout`), a normal `SolveRequest`,
and one of these outcomes:

| Outcome | Required data | Treatment |
| --- | --- | --- |
| `RESPONSE` (default) | Normalized `response` and strict boolean `expected_acceptable` | Evaluate the local contract and compare with the independent label. |
| `INFRA_FAILURE` | Neither response nor label | Count availability failure; exclude from quality samples. |
| `QUOTA_FAILURE` | Neither response nor label | Count quota failure; exclude from quality samples. |
| `VALIDATOR_FAILURE` | Neither response nor label | Count validation gap; prevent qualification. |

Every response must match the dataset's provider and model identity. The response fields are
`provider_id`, `model_id`, `text`, optional `finish_reason`, `citations` and `assertions`. Native
execution is unavailable in this replay runner, so native-function responses remain unverified.
Requests requiring cross-checks, high-impact final decisions or source-review policies are
rejected as unsupported by this local-contract runner. A qualified model used for those live
request paths must still pass their separate runtime gates.

An operator must label answers against the actual task requirements, independently of FAIR's
score. Keep enough source material to audit the labels privately. Do not generate acceptance
labels by copying the validator's answer, its score, or a model's self-assessment.

Choose representative tasks and freeze calibration/holdout membership before evaluating
candidate results. Use calibration cases to inspect and improve contracts; reserve holdout
cases for the final check. If holdout results guide another change, use a new untouched holdout
and suite revision. Run the same frozen workload for each candidate model. Avoid selecting only
easy tasks, dropping failures, or comparing models on different task mixes.

Duplicate case IDs and duplicate requests are rejected, including across splits. Client IDs,
quality levels and prompt wording for an identical explicit contract do not make a new trial.
This catches exact/contract duplicates, not all near-duplicates, correlated tasks or training
contamination. Representative sampling, labels, split independence and model provenance remain
operator responsibilities; renaming a fixture `WORKLOAD` cannot establish them.

Datasets are bounded to 10,000 cases and 20 MB through the CLI. Keep private datasets and reports
outside Git. Only `config/benchmarks.local.yaml` is automatically ignored; arbitrary private
filenames are not. The shipped fixture contains no private or live-provider data.

## Read the scores

Reports group outcomes by task class and split. A measured sample has a numeric contract score.
A success both passes the local contract and has an independent acceptable label. A false
accept passes locally but has a negative label; a false reject fails locally but has a positive
label. Null-score responses are counted as unverified and excluded from quality samples.
Unverified or validator-failure cases prevent qualification, even when other cases passed.

`pass_rate = successes / samples`. With zero samples the rate and interval are null. The report
uses a two-sided 95% Wilson interval for a binomial proportion, following
[NIST's Wilson method](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm). Its lower
bound accounts for sparse samples; it does not correct biased or correlated workload sampling.

Qualification uses `100 * min(calibration.lower_95, holdout.lower_95)`. Each split separately
needs the configured minimum sample count. No observed false accepts are allowed in either
split. Zero observed false accepts is not a guarantee of zero future false accepts.

For illustration, if **both splits** have these results:

| Correct usable responses / measured responses | Qualification score, approximately | Default levels met |
| --- | --- | --- |
| 1 / 1 | 20.7, but insufficient samples | None with the default 30-sample minimum |
| 90 / 100 | 82.6 | Commodity, standard |
| 95 / 100 | 88.8 | Commodity, standard, advanced |
| 100 / 100 | 96.3 | All four levels |

These are statistical examples, not measured model ratings. Default thresholds remain 75, 82,
88 and 92. Configured values must be finite numbers above zero and at most 100, nondecreasing
from commodity through high-impact support. When the optional benchmark gate is enabled,
the threshold applies to the conservative workload score as well as the local contract result.
A high-impact request still needs a distinct qualified checker.

Diagnostics also show threshold sweeps and a Brier score comparing numeric contract scores
divided by 100 against independent labels. With binary scores this is a disagreement diagnostic,
not proof of probabilistic calibration. All current positive contract thresholds make the same
per-answer decision; the sweep exposes that equivalence instead of inventing graded precision.
The workload qualification gate gives the levels different measured admission requirements.

## Review and enable a measured workload

No benchmark gate is enabled by default. An operator first reviews a workload report, its raw
evidence and label process. Do not approve the fixture or copy synthetic counts into approvals.
In a private YAML file, put the generated report's **`run` object unchanged** under an approval's
`run` field, with these additional fields:

```yaml
approvals:
  - run: {}  # Replace with the complete generated run object; an empty object is invalid.
    client_ids: [my-app]
    reviewer_id: operator-one
    review_note: Explain workload coverage, independent labels and reserved holdout review.
    representative_workload: true
    approved_at: '2026-09-09T18:00:00Z'
    expires_at: '2026-10-09T18:00:00Z'
```

Use actual review times with timezones: `observed_at <= generated_at <= approved_at < expires_at`.
Restrict the file's write access to operators permitted to approve models. The registry is a
trusted configuration boundary, not a signed attestation service; counts and provenance still
need operator review. It recomputes all qualification scores from validated integer counts.
It never trusts a supplied rounded score. Multiple approvals overlapping a client/provider/
model/task are rejected; replace the old record explicitly when reviewing a new run.

Set the model's `model_revision` in `config/providers.yaml` to the exact evaluated revision or
an operator-controlled deployment revision. A missing or different revision cannot qualify.
For mutable model aliases, update this revision when the underlying model changes and rerun
affected benchmarks. FAIR cannot infer an upstream change that the operator has not recorded.

Add to `config/routing.yaml`:

```yaml
benchmark_policy:
  min_samples: 30
  max_age_seconds: 2592000
```

Then configure the private file and restart the API:

```powershell
$env:FAIR_BENCHMARKS_FILE = 'C:\FAIR Free AI Router\config\benchmarks.local.yaml'
.\.venv\Scripts\python.exe -c "from fair.benchmarks.registry import BenchmarkRegistry; BenchmarkRegistry.from_file('config/benchmarks.local.yaml'); print('Approval file is valid')"
```

An enabled policy requires approved evidence for each candidate task class and authenticated
client. Clients cannot submit ratings or disable server benchmark policy in `/v1/solve`.
An absent registry grants no qualifications. An explicitly configured missing/malformed file
prevents startup. The file loads at startup, so restart after replacing or revoking an approval.
In Docker, mount the file read-only and configure its container path; the development Compose
file does not automatically forward this variable or mount the file.

## Runtime behavior and history

Qualification is checked during selection, at dispatch and before release of a locally accepted
answer. The final check includes the independent checker's approval. Expiry withholds output;
it never causes FAIR to release an unqualified answer or spend inference quota on a blocked
pre-dispatch attempt. Paid-route, privacy, capability, quota, source-policy, kill-switch and
answer-validation gates continue to apply. A good benchmark never rescues a rejected answer.

The benchmark lower bound replaces the neutral quality prior for qualified routes, with the
existing five-observation damping. Production quality failures can lower routing priority;
production success cannot raise it above the reviewed benchmark bound. This blend is a routing
heuristic, not a second confidence interval. Accumulated same-ID production history may lower
ranking after a revision, but cannot provide or raise the new revision's benchmark qualification.
Infrastructure/quota events do not enter the quality calculation. Benchmark observations remain
separate from production counters and never inflate live sample totals.

Solve responses and client-isolated request history contain `benchmark_checks` with per-route
states, times, sample counts, conservative score, required threshold and approval fingerprint.
The same reports are recorded as `BENCHMARK_QUALIFICATION_CHECKED` audit events. No reviewer
notes, raw benchmark tasks, labels or responses appear in those reports. The local attempt
disposition records the answer's contract check; final request acceptance can still be blocked
by qualification expiry or independent verification.

| State | Meaning |
| --- | --- |
| `UNAVAILABLE` | No approval for this client, provider, model and task. |
| `FIXTURE` / `UNREVIEWED` | Diagnostic data or workload representativeness has not been approved. |
| `VERSION_MISMATCH` | Model revision or validator engine version differs. |
| `EXPIRED` | Observation too old, approval expired, or approval time in the future. |
| `INSUFFICIENT` | Missing split or too few measured samples in either split. |
| `VALIDATION_GAP` | At least one unverified or validator-failure case in this task group. |
| `FALSE_ACCEPTANCE` | Independent labels expose a false accept in either split. |
| `BELOW_THRESHOLD` | The conservative score is below the request's quality level. |
| `PASSED` | This reviewed workload qualification meets the current policy. |
| `SERVICE_FAILED` | Qualification could not be evaluated; internal errors are withheld. |

When qualification prevents selection/release, FAIR returns
`BENCHMARK_QUALIFICATION_UNSATISFIED` without output. A qualification service failure preventing
selection/release returns `FAILED` / `VALIDATION_SERVICE_FAILED`. Existing policy failures keep
their own reasons. Qualification does not establish broad factual truth, live freshness or
general code correctness.

Migration `0005` adds nullable model revisions; engine version is `deterministic-v8`, benchmark
runner version is `benchmark-v1`. Old quality reports retain their original engine version.
Model or validator-version changes require a fresh workload run and operator review. This step
does not implement live benchmark collection, shadow scheduling, drift detection or automated
probability fitting. Live-provider ratings await actual representative workload recordings.
