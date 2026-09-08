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

## Remaining Sprint A verification

- Live PostgreSQL migration, audit-trigger and Docker verification. Neither PostgreSQL nor Docker
  is available on the local build machine; the PostgreSQL test is prepared but skipped locally.
- Production validation remains a gate before any live-provider integration.

## Subsequent sprints

- B: task-specific validators, quality calibration, verified acceptance, hallucination controls,
  richer escalation and persistent model-task performance. No quality claims are made yet.
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
The GitHub workflow is prepared locally and has not been executed on GitHub.

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
