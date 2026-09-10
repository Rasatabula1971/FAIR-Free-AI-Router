# Build status

Started 2026-09-08 in `C:\FAIR Free AI Router`, branch `codex/sprint-a-core`.
The user requested starting the build. Document instructions were treated as product
requirements, not separate authorization for deployment, paid inference or publishing.

## First core increment

- Python package, FastAPI app factory, configuration and offline mock adapter.
- Domain enums and validated DTOs for providers, models, profiles, attempts and escalation.
- Free-only admission checks with fail-closed defaults, rechecked before execution.
- Deterministic profiling and model filtering by capabilities, privacy and context.
- Configurable selection skeleton with neutral priors and task-specific measured-score input.
- Conservative request counters, bounded attempts, timeouts, throttle cooldown and circuit failure window.
- Request, attempt and audit persistence with initial Alembic migration.
- Authenticated solve, provider listing, client-owned history/audit and admin stop/resume.
- Non-empty, completion and JSON-schema checks; fail-closed escalation pending quality validation.
- Docker/PostgreSQL development packaging and GitHub Actions test workflow.

## Second increment

- Migration `0002`: relational provider, model, task-profile, quota, health-event and system-state
  tables. Legacy request/profile JSON is retained; new requests also write relational profiles.
- Durable reservations, exhaustion, observed quota resets, security blocks and throttle deadlines.
- Persistent circuit failure history, explicit CLOSED/OPEN/HALF_OPEN states and one leased probe.
  Failed or abandoned probes reopen the circuit; a restart cannot imply successful recovery.
- Database-backed stop/resume with the state change and audit event in one transaction.
  Migration carries forward the last legacy audited stop/resume command.
- Configuration snapshots reconcile at startup without resetting runtime governance state.
- Normalized adapter health, quota and model discovery contracts with offline mock implementations.
- Authenticated provider-health endpoint and provider summaries reflecting runtime blocks.
- Restart regression tests, migration preservation checks and a PostgreSQL CI test job.

## Sprint A integration verification

- PostgreSQL migration/audit-trigger tests and the packaged Docker API smoke check now pass
  on GitHub Actions. See the sixth increment below for scope and evidence. Neither PostgreSQL
  nor Docker is installed on the local Windows build machine; its PostgreSQL test stays skipped.
- Production validation remains a gate before any live-provider integration.

## Third increment: first Sprint B quality path

- Explicit host validation contracts for bounded exact arithmetic and complete JSON reference
  matching; accepted responses include output, provider/model, quality and verification scope.
- Hard rejection overrides a passing contract for schema failures, invalid/incomplete output,
  fabricated citation references, unsupported quotes and conflicting structured assertions.
- Distinct `UNVERIFIED` attempt disposition prevents missing validator coverage from being
  recorded as measured model failure. General, grounded, coding and high-impact tasks still
  escalate when appropriate validators are unavailable.
- Quality rejection retries a different eligible model; failed text is withheld. Validator
  service errors return `FAILED` without charging them to model quality or availability.
- Migration `0003` adds quality reports, escalation records and persistent model/task metrics.
  Selector rankings now use those measured metrics with a neutral prior. Quota and infrastructure
  failures do not lower measured quality. A small recent-quality window is retained.
- Admin-only performance endpoint; accepted request history remains isolated by client.
- Contract fingerprints preserve validation identity without storing host reference answers
  or evidence in audit rows. See [QUALITY_CONTRACTS.md](QUALITY_CONTRACTS.md) for exact scope.

## Subsequent sprints

- B (in progress): deterministic acceptance and persistent performance are implemented below.
  Remaining work includes broader code execution support, grounding/consistency checks,
  automated source credibility evaluation and calibration on actual model workloads. Scoped independent cross-checks are
  implemented in the fifth increment.
  Opt-in native execution of the bounded numeric subset is implemented in the seventh increment.
  Multi-source structured claim grounding is implemented in the eighth increment; free-form
  prose entailment remains outside the verified scope. Operator-reviewed source policies are
  implemented in the ninth increment. Offline benchmark calibration and reviewed workload
  qualification are implemented in the tenth increment. Step 4 expands numeric/list code
  validation and scoped sandbox recovery in the eleventh increment. Step 5 adds bounded
  fair client scheduling in the twelfth increment.
- C: distributed scheduling, retrieval capability boundaries and stronger operational isolation.
- D: feedback, rolling metrics, optional shadow checks and drift tracking are implemented in
  Step 7; representative live collection and automated review/alerts remain future work.
- E: Step 8 implements Groq, OpenRouter free and local Ollama adapters. Local inference is
  verified; live cloud account/inference verification remains pending credentials and approval.
- F: remaining verified adapters, Python/JavaScript SDKs, cache and operating runbooks.

## Known constraints

All shipped live providers are inactive; Step 8 adds opt-in adapters and an isolated local smoke.
Unknown provider costs fail admission. Unknown request limits have no invented allowance;
exhaustion without reset information stays blocked across restarts. No provider
credential is needed for this increment. JSON schema references are disabled to prevent
schema-driven network retrieval. Schema validation is structural only. One request runs at a
time with bounded, process-local client fairness within each priority class. Do not use this increment as a shared
production deployment or enable live inference before the subsequent gates are met.

Provider configuration remains file-backed, with relational snapshots for inspection. Runtime
state is durable. Unknown exhaustion and authentication blocks have a scoped, audited operator
recovery API in Step 6; quota resets require explicit new-window observations. Normalized
adapter observations cover request quotas; token/compute quota tracking is still future work.

The HTTP test dependencies currently emit upstream deprecation warnings; tests still pass.
GitHub Actions now passes core, PostgreSQL, Docker and native-sandbox jobs. Earlier increment
validation notes describe the results available at that time; current evidence is in increment fifteen.

## Validation of the first increment

- `pytest -q`: 34 passed, two upstream dependency deprecation warnings.
- `ruff check .`: passed.
- Alembic SQLite upgrade, schema-drift check, downgrade and re-upgrade: passed.
- Application lifespan smoke test against migrated SQLite: healthy startup, nine inactive
  candidates plus two offline demo fixtures; solve produced two attempts and explicit
  escalation with `paid_inference_executed: false`.
- Docker was not available on the build machine; PostgreSQL/container execution is unverified.

## Validation of the second increment

- Full suite: 52 passed, one PostgreSQL test skipped locally, two upstream deprecation warnings.
- Ruff lint and formatting checks: passed.
- Migration `0002` upgrade and schema-drift check: passed on the existing SQLite fixture.
- Automated migration round-trip preserves existing audit history and the stop command.
- Actual application lifespan restart test uses migrated storage and demo configuration,
  preserving stop state, quota consumption and request history through two app instances.
- PostgreSQL migration/audit-trigger job is defined in CI; remote CI and Docker remain unrun.

## Validation of the third increment

- Full suite: 96 passed, one PostgreSQL test skipped, two upstream deprecation warnings.
- Lint, formatting and SQLite migration/schema-drift checks pass.
- Regression coverage includes a deliberately hallucinating mock rejected before fallback,
  exact arithmetic, malformed JSON, hard-reject precedence, private accepted output, ranking
  changes across router restart, and separate accounting for quality/infra/quota/unverified outcomes.
- Full Sprint B is not complete: isolated code tests, general claim/source alignment,
  independent verification and calibrated quality scoring remain. Live providers are disabled.

## Fourth increment: source extraction and bounded code tests

- `grounded_json` contract binds each output field to a host-selected JSON source/path. Values
  and provenance must match exactly; fabricated values, wrong attribution and extra claims fail.
- `python_function` contract checks every host-supplied integer/boolean case using a bounded
  interpreter for a small Python AST subset. No native code execution, imports, calls, loops,
  external access or subprocesses are exposed to generated code. Host cases are withheld.
- Both contracts integrate with capability selection, quality rejection/fallback, persistent
  model/task metrics and explicit `SOURCE_DATA_MATCH`/`BOUNDED_CODE_TESTS` verification states.
- Engine version advances to `deterministic-v2`. No new migration is required. High-impact,
  freshness, unrelated capabilities and arbitrary prose/native code remain uncovered.
- Clarified that the phrase "source code" alone is not a request for factual grounding.

## Validation of the fourth increment

- Full suite: 179 passed, one PostgreSQL test skipped locally, two upstream deprecation warnings.
- Lint, formatting and migration/schema-drift checks pass.
- Adversarial tests reject imports, calls, decorators, loops, recursive functions, attributes,
  unexecuted dangerous branches, huge values and resource-budget violations without executing
  generated code. Source tests reject incorrect provenance, unsupported content and invalid input.
- Docker/PostgreSQL remain unavailable locally. Native sandboxing, general claim entailment,
  independent cross-model verification and calibrated scoring remain unfinished Sprint B work.

## Fifth increment: bounded independent verification

- Optional `cross_check_required` and mandatory verification for high-impact requests. A second
  eligible provider/model independently solves the same task without seeing the first answer.
- Both responses must pass the existing deterministic contract and agree in scope. Matching
  text alone cannot verify unsupported tasks or override hard rejection. Exact-value comparison
  covers arithmetic/JSON; code agreement is limited to the same hidden host test cases.
- Separate bounded checker budget, infrastructure failover, immediate escalation on measured
  disagreement/rejection, and withholding of provisional output on every unsuccessful check.
- Migration `0004` persists optional model independence groups; provider/model identities and
  known alias groups enforce configured route diversity. Unknown shared ancestry is not inferred.
- Unified attempt execution applies zero-spend, quota, quality and audit controls to both roles.
  Cancellation now persists a cancelled attempt as well as request status.
- Reports include cross-check state, role, attempt references and comparison scope. Candidate
  quality and final request acceptance remain distinct. Engine version is `deterministic-v3`.

## Validation of the fifth increment

- Full suite: 208 passed, one PostgreSQL test skipped locally, two upstream deprecation warnings.
- Lint, formatting, migration upgrade and SQLite schema-drift checks pass.
- Tests cover mandatory high-impact checks, producer/alias exclusion, verifier eligibility,
  separate call budgets, quota/outage failover, disagreement without repeated agreement-seeking,
  hidden producer answers/tests, code/JSON agreement, service failures and durable cancellation.
- PostgreSQL/Docker, native sandboxing, general claim entailment and calibrated quality scoring
  remain unverified or unfinished. Live providers remain disabled; remote CI has not been run.

## Sixth increment: PostgreSQL and Docker integration verification

- Fixed GitHub Actions service health-command quoting so PostgreSQL starts before its tests.
- Added a Docker job that builds the actual application image and starts the development
  Compose stack with a disposable PostgreSQL volume and offline demo fixtures.
- Added `scripts/docker_smoke.py`: checks readiness, inactive live providers, client/admin
  authentication, offline routing with withheld unverified output, and persisted request/audit
  history. Restarts the API and confirms that history and the stop switch survive, then resumes
  routing. CI removes its disposable stack and volume even when a check fails.
- Core tests, SQLite migration/schema checks, PostgreSQL migration round trips and append-only
  audit triggers, and Docker smoke checks passed in
  [GitHub run 34298968029](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34298968029)
  for code commit `0f9ba14`.
- This verifies the development package on GitHub's Linux runner. Local Windows Docker remains
  untested; production deployment, live adapters and native generated-code sandboxing are still
  outside the verified scope. Sprint B grounding and quality calibration work remains.

## Seventh increment: opt-in native function validation

- Added `native_python_function`, retaining the bounded numeric language and hidden host cases.
  Admitted source runs under native CPython in a dedicated container; only the router receives
  expected answers. Results report `NATIVE_CODE_TESTS` and the immutable sandbox image ID.
- Disabled by default. The operator must build and explicitly configure the trusted image.
  Containers have no network or host mounts and run as non-root with a read-only filesystem,
  dropped capabilities, no new privileges, seccomp, and memory/CPU/process/output limits.
- Native validation integrates with quality fallback, independent checks, durable attempt
  history and separate performance statistics. Executor failures do not lower model quality.
  Stop during validation withholds results; cancellation attempts cleanup and records lineage.
- Local suite: 238 passed, 10 integration tests skipped (one PostgreSQL and nine native Docker),
  with two upstream warnings. Lint and formatting passed.
- All four GitHub jobs passed for `744e4ac`: core, PostgreSQL, Docker API smoke, and native sandbox.
  The nine native-container tests verify correct/incorrect/strict-type answers, actual router
  acceptance, CPython semantics, OS restrictions, and timeout/cancellation/output-limit cleanup.
  Evidence: [run 34300569475](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34300569475).
- Engine version is `deterministic-v4`; no database migration is needed for the new JSON report
  scope. Earlier persisted reports retain their versions.
- This is limited development container isolation on Linux CI. Windows Docker, arbitrary code,
  external packages and unattended orphan recovery are not verified/implemented. Broader
  grounding, source credibility and calibrated quality scoring remain open. Live providers
  remain disabled. See QUALITY_CONTRACTS.md for setup, limits and failure behavior.

## Eighth increment: structured claim grounding across sources

- Added `grounded_claims`, a bounded contract matching requested subject/predicate/context keys
  against all supplied structured fact documents, not just the model's selected citations.
- Rejects unsupported or contradicted answers, assertions over conflicting evidence, missing or
  extra claims, malformed/duplicate responses, and incomplete or fabricated attribution.
- Explicit abstention remains unverified with a null score. Missing/conflicting evidence does
  not lower measured model quality when the model abstains. Partial support cannot accept a task.
- Added per-claim statuses/evidence counts, `STRUCTURED_CLAIMS_SUPPORTED`, separate task metrics,
  and additional grounding-failure accounting. Raw facts and rejected values remain outside
  quality/audit reports. Independent verification handles reordered claims and references.
- Engine version is `deterministic-v5`; no migration is required for these JSON report fields.
- Local validation: 293 tests passed, 10 PostgreSQL/native Docker tests skipped, two upstream
  warnings. Includes 55 new grounding tests; lint and formatting pass.
- All four GitHub jobs passed for code commit `d4d6ea8`: core, PostgreSQL, Docker API smoke and
  native sandbox. Evidence: [run 34301398979](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34301398979).
- This completes the structured-data increment of the broader grounding step. Prose entailment,
  source credibility, freshness and automatic conversion of prose into trusted facts are not
  implemented. Live providers remain disabled. See QUALITY_CONTRACTS.md for the complete format,
examples and acceptance scope.

## Ninth increment: operator-reviewed source policies (Step 2)

- Reviews bind exact evidence text to a SHA-256 hash, authorized clients, a configured origin
  group, source class, operator decision/rationale and timezone-aware observation/review/expiry
  times. Requests cannot approve themselves by supplying trust labels.
- Optional request and server policies combine conservatively. Missing, rejected, changed,
  expired, stale or disallowed evidence blocks dispatch. Corroboration is checked per requested
  fact; shared origin groups and identical content hashes cannot count twice.
- Policies are checked before dispatch, after responses and before release, including
  independent verification. Expiry withholds output without treating unavailable review
  coverage as a model-quality failure. Grounding still rejects conflicting reviewed facts.
- Reports and audit history retain check statuses and fingerprints without raw evidence or
  private review metadata. File-backed reviews load at startup; restart after changes.
- Added the [operator guide](SOURCE_REVIEWS.md) with complete review/request examples, server
  configuration, renewal procedures, Docker configuration requirements and failure statuses.
- Resumed validation on 2026-09-09: 332 tests passed, 10 PostgreSQL/native Docker tests skipped
  locally, two upstream deprecation warnings. All four GitHub jobs passed for code commit
  `dc4b2a8`: core, PostgreSQL, Docker API smoke and native sandbox. Evidence:
  [run 34302245481](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34302245481).
- Engine version is `deterministic-v6`; no database migration is required. Step 2's operator
  review scope is complete. Automated source assessment, live freshness and prose entailment
  remain outside that scope. Next is Step 3: benchmark-based quality-score calibration.
  Live providers remain disabled.

## Tenth increment: benchmark calibration and qualification (Step 3)

- Added an offline runner for saved, independently labelled model responses with frozen
  calibration/holdout splits, exact duplicate detection, per-task outcome counts, threshold
  sweeps, disagreement diagnostics and 95% Wilson pass-rate bounds. Service/quota failures
  stay outside quality samples. No provider calls or production statistics are generated.
- Added ten explicitly synthetic diagnostic cases and a reproducible aggregate report. A
  deliberately under-specified code contract exposes a false accept against its independent
  label. Fixtures can never qualify a model; no live model rating has been invented.
- Optional server benchmark policy requires operator-reviewed representative workloads for
  the authenticated client and exact model/task/revision/engine. Both splits need enough
  samples, no validation gaps or observed false accepts, and conservative scores meeting
  the selected quality level. The thresholds now distinguish benchmark-qualified routes.
- Qualification is checked at selection, dispatch and final release for the producer and
  independent checker. Missing/stale/expired/mismatched ratings withhold output. High ratings
  cannot override hard answer rejection or the existing governance gates.
- Benchmark evidence seeds a bounded routing prior; live quality failures can reduce priority
  while live success cannot raise the score above the reviewed qualification bound. Audits and
  client-isolated history retain score/count/fingerprint reports without private benchmark data.
- Migration `0005` adds nullable model revisions. Engine version is `deterministic-v7` and
  benchmark runner version is `benchmark-v1`. Source-review checks remain supported.
- See [BENCHMARKS.md](BENCHMARKS.md) for operator workflow, statistical interpretation, strict
  limits, file configuration and renewal; [OFFLINE_BENCHMARK_REPORT.json](OFFLINE_BENCHMARK_REPORT.json)
  is the generated diagnostic fixture report.
- Local validation: 383 tests passed, 10 PostgreSQL/native Docker tests skipped locally,
  two upstream deprecation warnings; lint, formatting and migration/schema checks pass.
  All four GitHub jobs passed for code commit `ba02085`: core, PostgreSQL migration/audit
  checks, packaged Docker API smoke and native sandbox. Evidence:
  [run 34354917233](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34354917233).
- The Step 3 implementation is complete. Actual model qualification awaits representative
  workload recordings and independent operator review. Live collection, shadow scheduling,
  drift detection and general probabilistic confidence fitting remain future work. Live
  providers and the optional benchmark gate remain disabled by default.

## Eleventh increment: expanded code validation and sandbox recovery (Step 4)

- Both interpreted and native contracts support flat integer/boolean lists, indexing,
  concatenation, scalar augmented assignments, bounded for/while loops with break/continue/else,
  and a small allowlist of built-ins. Strict type matching extends to every returned list element.
- Bounds cover source/AST size, list length, integer width, nesting, shared operation count,
  loop iterations, total host test data and native input/output serialization. The interpreter
  admits each case before Docker and the trusted image repeats admission before native execution.
- Owner-labelled 120-second container leases support scoped recovery at startup, before native
  execution and through an authenticated, audited admin endpoint. Complete inventory validation
  precedes removal; unexpired and unrelated containers are preserved. Recovery is bounded and
  missing-container cleanup is idempotent only after daemon-confirmed absence.
- Repeated cancellation cannot cancel forced cleanup. Uncertain cleanup blocks more native work
  until reconciliation establishes cleanup. Runtime failures do not penalize model quality.
- Engine version is `deterministic-v8`; no database migration is required. Rebuild the sandbox
  image for the expanded language and rerun/review any benchmark qualifications bound to v7.
- Local validation: 457 tests passed, 28 PostgreSQL/native Docker checks skipped locally,
  two upstream deprecation warnings. Includes 74 new local regressions and 18 new real-container
  checks. All four GitHub jobs passed for code commit `610ab00`: core, PostgreSQL, packaged Docker
  API and native sandbox, including expanded-language semantics and owner-scoped crash recovery.
  Evidence: [run 34391099029](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34391099029).
- See [quality contracts](QUALITY_CONTRACTS.md) for examples and exact limits, and
  [sandbox recovery](SANDBOX_RECOVERY.md) for owner configuration and operating procedures.
  Step 4's bounded expansion is complete. Arbitrary Python, packages, production hostile-code
  isolation and Windows Docker verification remain outside the implemented scope. Next is
  Step 5: fair client scheduling. Live providers remain disabled.

## Twelfth increment: fair client scheduling (Step 5)

- Replaced the request lock with bounded waiting queues, strict P0–P4 priority and per-client
  round-robin turns within each class. A turn includes the bounded primary/verification budget;
  running work is not preempted. Higher-priority traffic can starve lower classes until timeout.
- Client identity remains bound to its API key. Server configuration controls access to urgent
  priorities; unlisted clients may request P2–P4. Total/per-client counts, queued UTF-8 payload
  bytes, request bytes and monotonic wait deadlines bound admission and waiting.
- Refusals, expiry and queued cancellation make no provider calls or quality samples. Queue
  metadata and wait time are audited without raw payloads. HTTP disconnects cancel work; active
  cancellation retains its turn through cleanup. Stop drains pending work and resume cannot
  resurrect it. Shutdown waits for active cleanup before database disposal.
- Added an admin-only aggregate scheduler endpoint and [SCHEDULING.md](SCHEDULING.md) covering
  defaults, priority authorization, failure responses, retry guidance and operational limits.
- Added 30 scheduler regressions covering contention, per-client FIFO, continuous arrivals,
  priority order, admission bounds, timeout/dispatch races, cancellation, cleanup, shutdown,
  client isolation and complete retry/verification turns. Packaged API smoke also verifies
  scheduler access, urgent-priority denial and scheduling audit events.
- Queue payloads remain volatile and process-local. Hard-crash queue replay/reconciliation,
  distributed coordination and equal compute-time allocation remain outside this increment.
  No database migration or quality-engine version change is required (`deterministic-v8`).
- Local validation: 487 tests passed, 28 PostgreSQL/native Docker checks skipped locally,
  two upstream deprecation warnings. Lint, formatting and SQLite migration/schema checks pass.
  All four GitHub jobs passed for code commit `b873cb5`: core, PostgreSQL, packaged Docker API
  and native sandbox. Evidence:
  [run 34392674807](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34392674807).
  Step 5 is complete within the single-process scope. Next is Step 6: operational security and
  recovery. Live providers remain disabled.

## Thirteenth increment: operational security and recovery (Step 6)

- API authentication keeps key digests, rejects overlapping client/admin credentials and
  duplicate headers, supports bounded mounted-file sources, and masks configuration errors.
  Generic validation failures no longer echo submitted input. Rotation takes effect on restart.
- Added a scoped provider credential resolver and guarded adapter registration for later live
  integration. Exact credential reflection is blocked across inference/discovery, typed errors
  are normalized, and tests scan normalized requests, serialized results, ORM rows and logs.
- Added stopped-and-idle admin recovery with state fingerprints and atomic before/after audits.
  Authentication recovery preserves quota; circuit recovery permits one existing probe; quota
  resets require an operator-observed boundary newer than reservations and prior reset evidence.
  Provider eligibility and the global stop remain unchanged by recovery.
- Manual bounded request reconciliation marks crash-interrupted metadata failed without replay,
  output release, quota refunds or changes to existing terminal results. History stays isolated.
- Migration `0006` persists quota observation boundaries, conservatively backfilling legacy usage.
  The [operator guide](OPERATIONS_SECURITY.md) documents credential sources, recovery actions,
  evidence requirements and the single-process/trusted-adapter limitations.
- Local validation: 531 tests passed, 28 PostgreSQL/native Docker checks skipped locally,
  two upstream deprecation warnings. Lint, formatting and SQLite migration/recovery checks pass.
  All four GitHub jobs passed for code commit `a17b91a`: core, PostgreSQL, packaged Docker API
  and native sandbox. Evidence:
  [run 34419489805](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34419489805).
  Step 6 is complete within its documented scope. Live providers remain disabled. Step 7
  follows with feedback, rolling performance and drift tracking.

## Fourteenth increment: feedback, performance learning and shadow checks (Step 7)

- Added owned, idempotent feedback for accepted foreground results, atomically audit-logged.
  Optional correction/reason text is hashed, never stored raw or executed. A finite window of
  weighted feedback influences only that client's model/task preference, not another client's
  ranking, answer verification, measured quality samples or reliability.
- Kept lifetime metrics and added recent quality/operational windows, freshness-based sample
  confidence and a baseline-vs-recent mean-drop heuristic. Material drift reduces route ranking
  and marks the model/task for benchmark review. Sparse samples remain explicitly insufficient.
- Added disabled-by-default PUBLIC-only P4 shadow checks, a persistent per-client rate budget,
  known quota-headroom gates and one-attempt execution through existing governance/validation.
  Aliases, sensitive/evidence-bearing and high-impact tasks are excluded. Shadow output is
  withheld from responses and storage; comparisons and parent lineage remain audited.
- Migration `0007` adds feedback, operational freshness and shadow lineage, preserving existing
  history and foreign keys. The quality engine remains `deterministic-v8`. See
  [LEARNING.md](LEARNING.md) for formulas, defaults, API contracts and limits.
- Local validation: 557 tests passed, 28 PostgreSQL/native Docker checks skipped locally,
  two upstream deprecation warnings. Includes 25 learning regressions plus a migration with
  existing foreign-key-linked history. Lint, formatting and SQLite schema checks pass.
  All four GitHub jobs passed for code commit `d5642b0`: core, PostgreSQL (including existing
  request/audit preservation), packaged Docker API and native sandbox. Evidence:
  [run 34420585891](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34420585891).
  Step 7 is complete within the documented scope. Live providers and shadow collection remain
  disabled. Next is Step 8: implement and verify the first live provider adapters.

## Fifteenth increment: first live text adapters (Step 8)

- Implemented opt-in Groq, OpenRouter explicit free models and local Ollama adapters. Separate
  account attestations, current cloud reviews, fixed endpoints, model allowlists, PUBLIC-only
  cloud transport and text capability limits preserve admission boundaries. All shipped
  provider statuses and live switches remain inactive.
- Added current catalog/account preflight, zero-price OpenRouter routing constraints, local
  GGUF/remote-marker/context/digest checks, bounded JSON transport and cancellation cleanup.
  No paid fallback, cloud alias, proxy, redirect, tool execution or model pull is enabled.
- Normalized request quota observations and Retry-After into durable governance. Nonzero,
  invalid or missing OpenRouter cost reports stop dispatch, block the provider and withhold
  the answer without a false zero-spend assertion. Existing quality evaluation remains intact.
- Added an isolated `python -m fair.providers.smoke` command and dated
  [provider review and activation guide](LIVE_ADAPTERS.md). A real installed Ollama 0.33.3 /
  llama3.2:3b request was accepted in one attempt with deterministic arithmetic validation.
  No model download or cloud inference was used. Groq/OpenRouter keys were absent, so real
  cloud transport/account compatibility and representative workload qualification remain pending.
- Added 77 adapter/integration regressions. Local validation: 634 passed, 28 PostgreSQL/native
  Docker checks skipped locally, two upstream warnings. Lint, formatting and SQLite migration/
  schema checks pass. No new migration or quality-engine version is required.
- Step 8 implementation and local inference verification are complete within this scope;
  cloud activation remains gated on operator credentials and account/model review. Step 9
  follows with SDK and cache work. All four GitHub jobs passed for code commit `636186d`:
  core, PostgreSQL, packaged Docker API and native sandbox. Evidence:
  [run 34426623785](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/34426623785).
