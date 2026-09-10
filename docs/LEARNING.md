# Feedback, recent performance and shadow checks

Step 7 adds client-specific feedback preferences, recent performance reporting, a drift
heuristic and optional shadow checks. These signals rank eligible routes; they do not establish
answer truth, relax deterministic validation or qualify a model for a benchmark-gated workload.
Apply migration `0007` before starting this version. The quality engine remains `deterministic-v8`.

## Host feedback

An authenticated client can POST `/v1/feedback` for its own accepted foreground result:

```json
{
  "request_id": "<accepted request ID>",
  "accepted": false,
  "rating": 2,
  "reason": "Incorrect for the host workflow",
  "correction_text": "Optional host correction"
}
```

At least one of `accepted` or `rating` is required. Ratings are integers from 1 to 5. An explicit
`accepted: false` gives score 0 regardless of rating; otherwise the rating maps to 0/25/50/75/100.
`accepted: true` without a rating gives 100. Feedback refers to the final producer, not an
independent checker. Failed, unverified, interrupted and shadow requests cannot receive it.

There is one feedback record per request. An identical retry returns the original record;
conflicting resubmission returns HTTP 409. Missing/other-client requests return 404. Invalid
bodies return 422. `GET /v1/requests/{id}/feedback` is restricted to the owning client.

The transaction stores rating/acceptance, target model/task, input fingerprint and hashes of
the optional reason/correction, with an append-only request audit event. It does not store raw
reason/correction text. Retain that text outside FAIR if needed. Corrections are never executed,
included in model prompts, used as new validation contracts, or treated as verified evidence.
An audit failure rolls back feedback. Successful feedback leaves the original result and
verification state intact.

Routing uses at most the latest 20 feedback scores from that same client/model/task within the
configured age window. Each feedback score has weight 3 by default, against at most 20 model
observations plus five prior observations. Negative feedback lowers that client's preference;
positive feedback can raise it. Other clients' preferences are unaffected. Feedback does not
alter reliability counters or the measured model-quality sample count. The reviewed benchmark
bound still caps ranking when qualification is enabled, and all answer-level gates still apply.

This is host preference learning, not objective calibration. Authenticated clients can influence
their own routing and share the existing aggregate validated performance history. The one-record
limit and finite window prevent repeated submissions for one request from multiplying weight.
The application has no feedback edit/delete endpoint or automatic retraining pipeline.

## Performance and drift

The existing admin-only `GET /v1/models/performance` retains lifetime counts and now adds:

- Recent quality: the last 20 measured quality scores, excluding infrastructure failures,
  quota failures, missing validation coverage and cancellations.
- Recent operations: the last 20 recorded non-cancelled attempts, including latency and separate
  infrastructure/quota counts. These do not overwrite lifetime totals.
- Historical quality baseline: lifetime measured quality excluding the recent window.
- `drift_state`, `quality_drop_points`, and `benchmark_review_required`.
- `last_quality_at` and `observation_confidence`.

Drift needs at least 20 historical baseline samples and 5 recent samples. A recent mean at least
15 points below the baseline marks `DEGRADED` and limits route quality to the lower recent
estimate, with the same five-observation prior. Otherwise the state is `STABLE`, or
`INSUFFICIENT_DATA` when sample coverage is inadequate. When recent observations improve the
flag can clear. This is a mean-drop heuristic, not a statistical hypothesis test or proof of
provider drift; workload mix and contract coverage can change the mean. A flag asks the operator
to revisit benchmarks; it does not mutate approvals or trigger an external notification.

Observation confidence is `n / (n + 10)` times exponential age decay, with a default 30-day
half-life from the last quality sample. It reports sample support/freshness, not the probability
that an answer is correct. Infrastructure/quota activity and feedback do not refresh that
timestamp. Legacy history has unknown freshness and confidence 0 until a new quality observation.
Recent operational windows begin with new attempts; migration does not invent prior timing.

## Optional shadow checks

Shadow collection is disabled by default. When enabled, accepted PUBLIC foreground tasks with
no supplied evidence and below the high-impact quality level may be copied to another eligible
model. The original input remains in memory only. Sensitive requests, high-impact requests,
unknown quota allowance and known model aliases do not qualify.

The per-client persistent budget permits at most one shadow admission per ten accepted
foreground requests with the default rate. The first shadow therefore needs ten accepted
foreground requests. Failed/cancelled/expired shadow admissions still consume budget. Existing
pre-Step-7 accepted history counts toward that budget when shadowing is enabled. The budget
survives restart and never recursively counts shadow successes as foreground credit.

An alternative model must have known request quota with more than 70% remaining by default.
The governor checks headroom before enqueue, at selection and before reservation; no allowance
is invented for providers with unknown limits. Shadow requests use the same client identity
at P4, queue limits, timeout, admission, privacy, source/benchmark and validation gates. They
receive one primary attempt, with no retry or additional cross-check budget. A waiting P4
request cannot take a turn ahead of waiting P0–P3 work; an already-running shadow is
non-preemptive like every other solve.

Validated shadow outcomes contribute ordinary model/task observations. Comparison uses the
existing deterministic scope (exact values or host test cases). Agreement does not independently
prove correctness. The response text is never returned or stored as accepted output: shadow
history reports `execution_kind: "SHADOW"`, its parent request ID and `output: null`.
The original request gets a `SHADOW_COMPLETED` audit entry for completed checks; the shadow
request retains comparison metadata and attempt lineage. It cannot receive host feedback.

Stop and shutdown govern shadow work too. Shutdown tracks and cancels background copies before
database disposal. After a crash, shadow metadata can be reconciled through Step 6's manual
recovery endpoint; there is no queue replay. Embedders should call `await router.close()`.

## Configuration

Defaults in `config/routing.yaml`:

```yaml
feedback_weight: 3
feedback_max_age_days: 30
confidence_half_life_days: 30
drift_drop_points: 15
shadow_enabled: false
shadow_min_headroom: 0.7
shadow_max_rate: 0.1
```

Set feedback weight to 0 to retain audit feedback without ranking influence. Shadow rate is
capped at 10%, and minimum headroom cannot be set below 40%. To use a more conservative tier,
for example, configure headroom 0.4 and rate 0.03. This version supports one configured tier,
not automatic multi-tier sampling. Restart after changing configuration.

This remains a single-process scheduler and a request-count shadow budget, not a token/compute
budget. Automatic drift alerts, representative live benchmark qualification, retraining, and
general semantic agreement remain outside this increment. All live providers remain disabled.
