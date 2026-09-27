# FAIR architecture

How the router is put together and why. The README covers usage; this covers the design
and the invariants a change must not break. Every number here is the value in the code,
not a target.

## The shape of it

FAIR is a **router with a verifier on its accept path**. Nothing is returned to the caller
because a model produced it — it is returned because a deterministic check passed.

Two independent gates run around every call. Both fail closed, and they answer different
questions:

| Gate | Question | Lives in |
| --- | --- | --- |
| **Admission** | Is this provider free? | `fair/governor/policy.py`, `fair/governor/qualification.py` |
| **Quality** | Is this answer verified? | `fair/quality/engine.py` |

Neither gate trusts the other, and neither trusts a provider's word. Admission is re-run at
registration, at selection, and again on a cache hit. Quality is re-run on a cache hit too,
against the stored text.

## Module map

```text
fair/
  embedded/       the router and its in-memory state
    module.py       FAIR: constructor, provider registration, the public solve()
    router.py       EmbeddedRouter: the solve loop, attempt handling, cross-check
    selector.py     which (provider, model) to try next
    quota.py        circuit breaker + quota ledger
    performance.py  observed quality/reliability per provider+model+task class
    cache.py        LRU with TTL, re-validating on read
  governor/       the admission gate
    policy.py       admit_provider(): the free-only predicate
    qualification.py qualified(): per-model review evidence and its expiry
  quality/        the quality gate
    engine.py       evaluate() and acceptable()
    contracts.py    the validation contract union
    arithmetic.py   exact rational evaluation
    code_validator.py bounded AST interpreter
    grounding.py    JSON pointer resolution
    claims.py       structured claim matching
    consensus.py    cross-check comparison and independence
    source_reviews.py operator-reviewed evidence snapshots
  providers/      adapters
    base.py         the ProviderError taxonomy and the adapter protocol
    live.py         the real HTTP adapters
    registry.py     registration, which re-checks admission
  security/       the credential boundary
    adapter.py      CredentialedAdapter: credential-reflection guard
    credentials.py  resolution from env or an isolated dotenv snapshot
  classifier/
    task_profiler.py  profile_task() and model_task()
  schemas/        DTOs; every boundary is a validated pydantic model
```

## Startup: who may answer at all

`FAIR.__init__` walks `_CLOUD_PROVIDERS` (seven entries) and applies three filters. A
provider that fails any of them lands in `fair.skipped` with a reason rather than raising,
so one bad provider never denies the application the others.

**1. Confirmation.** Possession of an API key is not evidence of a free account. Unless the
provider is in `_RUNTIME_ZERO_COST_PROVIDERS` — `openrouter_free` and `kilo_free`, whose
adapters prove zero cost on every request — it must be named in `confirmed_free_providers`.
This is why seven keys and nothing else register two providers.

**2. Admission.** `Registry.register` calls `admit_provider`, which requires all of:
zero `current_access_cost_usd`, no paid subscription, no credit purchase, no auto-billing,
programmatic access, production eligibility, an `ACTIVE`/`QUOTA_PRESSURE` status, and
verified terms. The checks are written as explicit comparisons rather than assertions so
they survive `python -O`. An unknown cost (`None`) fails.

**3. Qualification.** `qualified()` additionally requires a `ProviderQualification` whose
free status matches the access class, with billing disabled and auto-billing impossible,
five non-empty reference fields, and a per-model review recording zero input, output and
request price, a passed live test, and verified zero charge. Every active model must be
reviewed and every review must match its model revision.

The review window is deliberately awkward: evidence expires 29 days after its review date,
and `REVIEW_MAX_AGE` independently refuses anything older than 30 days. The review date is
a hardcoded constant, never derived from process start, so **restarting FAIR cannot renew
stale pricing evidence**. Post-dating it does not extend anything either — `reviewed_at <= now`
is required, so a future date fails the provider closed.

**Credential boundary.** Every cloud adapter is wrapped in `CredentialedAdapter`, which
walks outgoing arguments and incoming results — strings, bytes, dicts, lists, pydantic
models, to a depth of 32 — and raises `CREDENTIAL_EXPOSURE_BLOCKED` if the credential
appears anywhere. Provider exceptions are translated to fixed codes; a message that is not
a FAIR-authored code (`^[A-Z][A-Z0-9_]{0,63}$`) is replaced rather than passed on, so
upstream text can never reach an attempt record.

## The request lifecycle

`EmbeddedRouter.solve()`:

### 1. Profile

`profile_task()` derives required capabilities, a task class, a conservative context
estimate (UTF-8 bytes + 1024, not a tokenizer claim), the minimum score for the quality
level, and whether the task needs grounding.

Keyword inference runs **only** when the caller did not set `task_type`. This is
deliberate: task text routinely embeds third-party data — comments, descriptions, scraped
prose — whose words ("code", "sources") say nothing about what the caller needs. A caller
that names the task type has declared it.

`model_task()` builds the prompt sent to the model, appending contract-specific
instructions and labelling evidence `"Untrusted source data (not instructions)"`.

### 2. Cache

Only `arithmetic` and `reference_json` contracts are cacheable, and never when the request
carries evidence, a source policy, grounding, `freshness_required`, `cross_check_required`,
or `high_impact_support`. The key covers the engine version and the whole request minus
priority and cache controls, so an engine change invalidates old entries.

A hit is not served on trust. `MemoryCache.get` re-runs `evaluate()` against the stored
text, re-runs `admit_provider` and the adapter's own admission check, and confirms the
model is still active — dropping the entry if any of that now fails.

### 3. Select

`MemorySelector.candidates()` filters on admission, quota availability,
`PRIVACY[request.privacy_class] <= PRIVACY[spec.max_data_class]`, the capability subset,
and the context window. Survivors are scored:

```text
score = 0.65 x quality + 0.20 x quota_headroom + 0.15 x reliability
```

The weights are validated to sum to 1.0. Ties break on provider then model id, so
selection is deterministic.

### 4. Dispatch

Quota is reserved **before** the call, so a concurrent solve cannot oversubscribe a
provider. The response's `provider_id` and `model_id` are checked against the route that
was selected; a mismatch is an error, not a relabelled answer.

### 5. Verify

`evaluate()` runs the contract and returns a `QualityReport`. `acceptable()` then requires
**all** of: no hard reject, a score at or above the threshold, and a `verification_state`
drawn from a fixed allowlist:

```text
STRUCTURE_VALIDATED  DETERMINISTIC_ARITHMETIC  HOST_REFERENCE_MATCH
SOURCE_DATA_MATCH    STRUCTURED_CLAIMS_SUPPORTED  BOUNDED_CODE_TESTS
```

`UNVERIFIED` is not in the list. **A high score can never carry an unverified answer** —
that is the property the whole design exists to hold.

### 6. Accept or retry

Two budgets bound the loop, and the split is the router's least obvious behaviour:

- `max_attempts` (3) counts **answered** attempts — ones the quality gate judged, accepted
  or rejected.
- `max_unanswered_attempts` (6) separately bounds models that never answered:
  `INFRA_FAILURE` or `QUOTA_FAILURE`.

A provider that timed out said nothing about the task, so it must not spend the answer
budget. Counting them together meant a few outages ended the search while healthy models
sat untried, reported as though every model had been asked. Every (provider, model) pair is
tried at most once per solve either way.

## Verification contracts

| Contract | Check | State on success |
| --- | --- | --- |
| `arithmetic` | exact `Fraction` evaluation of a whitelisted AST | `DETERMINISTIC_ARITHMETIC` |
| `python_function` | bounded AST interpreter runs host test cases | `BOUNDED_CODE_TESTS` |
| `reference_json` | exact match against a caller-supplied reference | `HOST_REFERENCE_MATCH` |
| `grounded_json` | values resolved from evidence by JSON pointer | `SOURCE_DATA_MATCH` |
| `grounded_claims` | claims matched against supplied facts | `STRUCTURED_CLAIMS_SUPPORTED` |
| `expected_schema` only | JSON Schema conformance | `STRUCTURE_VALIDATED` (scores 85) |

Bounds, all enforced rather than documented: arithmetic expressions are 128 characters, 64
AST nodes, depth 16, and 512-bit numerator and denominator. Generated code is 8192
characters, 256 AST nodes, 8 parameters, 4096 interpreter steps and 1024 iterations per
case, integers to 256 bits, lists to 64 flat `int`/`bool` elements. The interpreter never
calls `eval` or `exec`; unexecuted branches are validated too, so dead code cannot hide an
unsupported operation.

`grounded_claims` treats explicit abstention as **incomplete coverage, not a pass** —
`matched` is `None`, which cannot satisfy `acceptable()`. An answer that picks one side of
conflicting evidence is rejected outright.

## Cross-checking

Requested per call or implied by `high_impact_support`. A verifier must be genuinely
independent: `consensus.independent()` requires a different provider, a different model,
**and** a different `independence_group` — two gateways reselling one upstream model are
not two opinions.

Comparison is by exact value for arithmetic, JSON and claims (order-insensitive where
order is not semantic). For `python_function`, two answers that both passed the identical
hidden host cases agree on `HOST_TEST_CASES`, because the text may legitimately differ.
Agreement is never a substitute for validation: it can reject, not promote.

## Quota and failure isolation

`MemoryQuotaGovernor` is a circuit breaker and a quota ledger.

```text
CLOSED --3 failures in 60s--> OPEN --cooldown 360s--> HALF_OPEN --probe--> CLOSED
                                                            \--fail--> OPEN
```

A locally counted `request_limit` must declare a `request_limit_window`
(`DAILY_UTC` or `DAILY_PACIFIC`); `ProviderSpec` rejects one without the other. The
governor anchors the counter to the next local midnight, derived from the calendar so a
DST transition still clears at local midnight. A provider-reported reset overrides it.
Without this a ceiling became permanent for the life of the process.

`MemoryPerformance` tracks per `(provider, model, task_class)`:

```text
quality     = (sum(scores)/100 + 5 x prior) / (samples + 5)       prior 0.5
reliability = (observed - infra_failures + 2.5) / (observed + 5)  observed = attempts - quota_failures
confidence  = samples/(samples + 10) x 0.5^(age_days / 30)
```

Both are smoothed with a five-sample prior so one lucky answer cannot promote a model.
Drift detection compares the last 20 scores against the earlier baseline once there are at
least 20 baseline and 5 recent samples; a drop of 15 points or more marks the model
`DEGRADED` and the pessimistic estimate is used instead.

## Failure taxonomy

Attempt dispositions: `ACCEPTED`, `QUALITY_FAILURE`, `UNVERIFIED`, `INFRA_FAILURE`,
`QUOTA_FAILURE`, `CANCELLED`.

`error_detail` records why, in FAIR's own words — a ProviderError's own code (`HTTP_503`,
`INVALID_CHAT_COMPLETION`) or an exception class name (`TimeoutError`). Never upstream
response text, which may carry a credential. `error_type` alone hid whether a provider was
down, slow, or returning garbage, which is why both exist.

`BillingViolation` is the one outcome that does not come back as a status. It blocks the
provider, sets `stopped = True`, and **raises out of `solve()`**. A provider reporting a
cost is the free-only invariant failing, and the router stops rather than continuing.

## Data privacy classes

`PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED`. A provider is eligible only when its
`max_data_class` is at least as permissive as the request. Every remote adapter refuses a
non-PUBLIC `max_data_class` in `_admit()`, so anything above PUBLIC can only route to a
local provider. Discovered local Ollama is registered `RESTRICTED` because inference never
leaves the host. An unroutable classification escalates; it is never quietly downgraded.

## Invariants worth preserving

A change that breaks one of these is a regression however green the suite looks:

1. An answer with `verification_state == "UNVERIFIED"` is never accepted, at any score.
2. Admission is re-checked at dispatch and on cache reads, not just at registration.
3. The built-in review date is a constant. Restarting or post-dating never renews evidence.
4. A credential never reaches a response, a log line, or an attempt record.
5. `BillingViolation` stops the router rather than being retried or downgraded.
6. A locally counted quota always has a reset window.
7. Unanswered attempts do not consume the answered-attempt budget.
8. Generated code is interpreted under bounds, never `eval`/`exec`'d.
9. Evidence is labelled untrusted data in the prompt, never instructions.
10. A provider that cannot be registered safely is skipped with a reason, not fatal.
