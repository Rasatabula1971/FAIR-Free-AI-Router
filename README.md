# FAIR Free AI Router

FAIR is a governed routing service for zero-cost AI inference. This repository contains the
first runnable core increment of Sprint A. It is not yet a production AI service.

The core enforces free-only provider admission at registration, selection and execution;
profiles tasks; filters models by capability, context and privacy; reserves request quotas;
and retries a bounded number of distinct free routes. It persists request, attempt and audit
lineage and returns structured escalation when no eligible, verifiable answer exists.

**No live adapters are included or enabled.** Demo mode uses offline fixtures. A non-empty
or schema-valid response is not evidence of factual correctness. Until Sprint B implements
task-specific quality validation, every solve deliberately returns `ESCALATION_REQUIRED`.
No response text, invented quality score, or paid inference is returned.

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
| `GET /v1/requests/{id}` | Owning client only |
| `GET /v1/requests/{id}/audit` | Owning client only |
| `POST /v1/system/stop` | Separate admin key |
| `POST /v1/system/resume` | Separate admin key |

Stop prevents subsequent model dispatches, including retries; it does not cancel an
already-running attempt. The switch and quota counters are process-local. Use one worker.
API keys are held only in application authentication closures; tasks and keys are not stored
in request/audit rows. Audit ORM updates/deletes are rejected, and the PostgreSQL migration
adds a database trigger rejecting update/delete/truncate. Database-owner DDL is outside that
boundary. SQLite is for tests only.

## Build roadmap and limitations

See [BUILD_STATUS.md](docs/BUILD_STATUS.md) for the exact implemented scope and remaining work.
The supplied PDR, handoff, specification and seed are preserved under `docs/source` as
requirements references. ASVS archives were not present, so no donor code was copied.

Implementation references: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
and [SQLAlchemy ORM](https://docs.sqlalchemy.org/en/20/orm/quickstart.html).
