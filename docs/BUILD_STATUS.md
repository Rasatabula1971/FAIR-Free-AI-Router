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
  source credibility evaluation and quality calibration. Scoped independent cross-checks are
  implemented in the fifth increment.
  Opt-in native execution of the bounded numeric subset is implemented in the seventh increment.
- C: fair scheduler, retrieval capability boundaries and stronger operational isolation.
- D: feedback, rolling performance, shadow benchmarking and drift detection.
- E: two or three live adapters after current provider terms/quota/privacy verification.
- F: remaining verified adapters, Python/JavaScript SDKs, cache and operating runbooks.

## Known constraints

All live providers are inactive; even local Ollama remains disabled until its adapter exists.
Unknown provider costs fail admission. Unknown request limits have no invented allowance;
exhaustion without reset information stays blocked across restarts. No provider
credential is needed for this increment. JSON schema references are disabled to prevent
schema-driven network retrieval. Schema validation is structural only. Requests are serialized,
and there is no multi-client fairness guarantee yet. Do not use this increment as a shared
production deployment or enable live inference before the subsequent gates are met.

Provider configuration remains file-backed, with relational snapshots for inspection. Runtime
state is durable. Unknown exhaustion and authentication blocks currently require operator
database maintenance to clear; a scoped, audited recovery API is not implemented. Normalized
adapter observations cover request quotas; token/compute quota tracking is still future work.

The HTTP test dependencies currently emit upstream deprecation warnings; tests still pass.
GitHub Actions now passes core, PostgreSQL and Docker jobs. Earlier increment validation notes
below describe the results available at that time; current integration evidence is in increment six.

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
