# FAIR Free AI Router

FAIR is a governed routing service for zero-cost AI inference. This repository contains the
Sprint A routing core and the developing Sprint B quality engine. It is not yet a production AI service.

The core enforces free-only provider admission at registration, selection and execution;
profiles tasks; filters models by capability, context and privacy; reserves request quotas;
and retries a bounded number of distinct free routes. It persists request, attempt and audit
lineage and returns structured escalation when no eligible, verifiable answer exists.

**No live adapters are included or enabled.** Demo mode uses offline fixtures. A non-empty
or schema-valid response is not evidence of factual correctness. Arithmetic, host-reference JSON,
source-bound JSON extraction, and bounded Python-function test contracts can produce `ACCEPTED`.
Unsupported tasks still escalate. Failed answers are withheld. Paid inference is never executed.

Set `cross_check_required: true` to require a second provider/model to solve and validate the
same task independently. This is mandatory for `high_impact_support`. Both answers must pass
the selected contract and agree within its scope; otherwise FAIR withholds the provisional
answer and returns escalation or a service failure. Agreement does not establish general truth.

See [quality contracts](docs/QUALITY_CONTRACTS.md) for the supported checks, verification
labels, examples and limitations. These deterministic checks do not establish general
factual correctness or validate arbitrary generated code. The code validator interprets only a
small numeric subset; it never passes generated code to Python `exec`/`eval` or a subprocess.

## Development

Python 3.12+ is required. From PowerShell:

```powershell
Set-Location 'C:\FAIR Free AI Router'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

The test suite uses in-memory SQLite and offline mocks. It exercises paid-route rejection,
execution-boundary checks, kill switches, capability/privacy/context filters, task-specific
selection, quota scarcity, 429/quota/outage/timeout failover, cooldown recovery, bounded
attempts, conservative concurrent reservations, schema validation, authenticated client
isolation and persisted audit lineage.

Quality tests exercise exact rational arithmetic, strict JSON reference matching, hard-reject
precedence, citation-reference checks, structured assertion conflicts, model switching after
rejection, private accepted output and persistent task-performance learning. A validator
service error returns `FAILED` without penalizing the provider.
Further tests cover source-path provenance, unsupported claims, hostile code constructs,
resource limits and code test failures that trigger another model attempt.
Cross-check tests cover route diversity, alias groups, bounded failover, disagreement,
answer isolation, high-impact enforcement and durable cancellation lineage.

Restart regressions also cover durable stop/resume, reservations, exhaustion, authentication
blocks, throttle deadlines, circuit history, single recovery probes and abandoned probes.
The migration test verifies that existing audit history and the last stop command survive
an upgrade. A separate PostgreSQL migration/audit-trigger test runs when
`FAIR_TEST_POSTGRES_URL` is set; GitHub Actions includes a PostgreSQL service for that check.

GitHub Actions also builds and starts the actual Docker Compose package and runs
`scripts/docker_smoke.py` against its HTTP API. This checks offline routing, authentication,
request/audit persistence, and stop/resume across an API restart. The job uses disposable CI
credentials and removes its database volume afterward; no live providers are enabled.

## Local service with PostgreSQL

The Compose file is a development configuration with local database credentials. Docker
must be installed and running. Set distinct random keys before starting:

```powershell
$env:FAIR_CLIENT_KEYS = '{"my-app":"YOUR_RANDOM_CLIENT_KEY"}'
$env:FAIR_ADMIN_KEY = 'YOUR_SEPARATE_RANDOM_ADMIN_KEY'
$env:FAIR_DEMO_MODE = '1'
docker compose -f infra/docker/docker-compose.yml up --build -d
```

Compose runs Alembic before starting a single API worker and binds the API to localhost.
Open http://127.0.0.1:8000/docs for the API reference. The application does not load `.env`
automatically; shell environment variables are used. Compose supports its normal `.env`
substitution rules.

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/solve' `
  -Headers @{ 'X-API-Key' = 'YOUR_RANDOM_CLIENT_KEY' } `
  -ContentType 'application/json' `
  -Body '{"client_id":"my-app","task":"Write a short greeting"}'
```

Demo mode returns two mock attempts and `QUALITY_VERIFICATION_UNAVAILABLE`. With demo mode
off, the inactive registry returns `NO_ELIGIBLE_FREE_MODELS` without making a model call.

For an existing local PostgreSQL instance, set `FAIR_DATABASE_URL`, run
`.\.venv\Scripts\alembic.exe upgrade head`, then
`.\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 --workers 1`.

## Implemented endpoints

| Endpoint | Access |
| --- | --- |
| `GET /healthz` | Public process health |
| `POST /v1/solve` | Client key; body identity must match key |
| `GET /v1/providers` | Client key; safe registry summary |
| `GET /v1/providers/{id}/health` | Client key; persisted quota and circuit observations |
| `GET /v1/models/performance` | Admin key; up to 1,000 aggregate model/task records |
| `GET /v1/requests/{id}` | Owning client only |
| `GET /v1/requests/{id}/audit` | Owning client only |
| `POST /v1/system/stop` | Separate admin key |
| `POST /v1/system/resume` | Separate admin key |

Stop prevents subsequent model dispatches, including retries; it does not cancel an
already-running attempt. The switch, request counters, failure windows, quota exhaustion,
authentication blocks and recovery deadlines persist in the database. Use one worker;
multi-worker scheduling and distributed dispatch coordination are not implemented.
API keys are held only in application authentication closures; tasks and keys are not stored
in request/audit rows. Audit ORM updates/deletes are rejected, and the PostgreSQL migration
adds a database trigger rejecting update/delete/truncate. Database-owner DDL is outside that
boundary. SQLite is for tests only.

Provider YAML remains the authority for configured eligibility and models. Startup records
relational snapshots without clearing runtime blocks or quota consumption. A quota reset
must be explicitly observed by the adapter; no daily/monthly reset is invented. Exhaustion
without a known reset, and authentication blocks, remain blocked across restarts. An operator
recovery workflow for those states is still pending. Health reads report stored observations;
they do not make live provider calls. Deadline fields use UTC Unix seconds.

After pulling a schema change, run `alembic upgrade head` before starting the service.
Existing profile JSON remains readable; new requests also receive relational profile rows.
Migration `0003` adds quality reports, escalation records and model/task statistics. Only new
attempts contribute to these statistics. Accepted response text is stored with the request
result and is readable only by its owning client. Rejected response text, host reference
answers and raw evidence are not stored in quality/audit rows; a contract fingerprint records
which validation inputs were used. Integrators must retain their input for exact replay.
Migration `0004` adds optional model `independence_group` metadata. Known aliases of the same
underlying model/family should share a group; FAIR then excludes them from checking each other.
Provider and model IDs must also differ. Unknown aliases cannot be automatically detected.

## Build roadmap and limitations

See [BUILD_STATUS.md](docs/BUILD_STATUS.md) for the exact implemented scope and remaining work.
The supplied PDR, handoff, specification and seed are preserved under `docs/source` as
requirements references. ASVS archives were not present, so no donor code was copied.

Implementation references: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
and [SQLAlchemy ORM](https://docs.sqlalchemy.org/en/20/orm/quickstart.html).
