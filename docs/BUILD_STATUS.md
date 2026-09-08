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

## Remaining Sprint A work

- Dedicated relational provider/model/profile/quota/health tables and corresponding migrations;
  current registry and quotas live in memory and profile/attempt detail uses JSON columns.
- Complete normalized health/quota/model-discovery adapter contract.
- Durable quota/reset/health and kill-switch state across restarts.
- Explicit half-open probe state and recovery telemetry; current serial execution permits only
  one request at a time and uses cooldown re-entry.
- Live PostgreSQL migration, audit-trigger and Docker verification.

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
exhaustion without reset information stays blocked for the process lifetime. No provider
credential is needed for this increment. JSON schema references are disabled to prevent
schema-driven network retrieval. Schema validation is structural only. Requests are serialized,
and there is no multi-client fairness guarantee yet. Do not use this increment as a shared
production deployment or enable live inference before the subsequent gates are met.

The HTTP test dependencies currently emit upstream deprecation warnings; tests still pass.
The GitHub workflow is prepared locally and has not been executed on GitHub.

## Validation of this increment

- `pytest -q`: 34 passed, two upstream dependency deprecation warnings.
- `ruff check .`: passed.
- Alembic SQLite upgrade, schema-drift check, downgrade and re-upgrade: passed.
- Application lifespan smoke test against migrated SQLite: healthy startup, nine inactive
  candidates plus two offline demo fixtures; solve produced two attempts and explicit
  escalation with `paid_inference_executed: false`.
- Docker was not available on the build machine; PostgreSQL/container execution is unverified.
