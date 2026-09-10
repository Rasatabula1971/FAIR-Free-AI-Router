# Deployment and recovery runbook (Step 10)

This runbook covers one FAIR API process and PostgreSQL 17 in the existing local Docker Compose
deployment. Live providers, shadow collection and exact caching remain disabled unless the
operator explicitly configures them. This is not a public production deployment or evidence that
three cloud routes have been qualified. Provider activation still follows [LIVE_ADAPTERS.md](LIVE_ADAPTERS.md).

## Before an upgrade

Record the tested application commit/image, current Alembic revision and the private configuration
version. Keep client/admin/provider keys and source/benchmark reviews outside Git and the image.
`secrets/`, backups and environment files are now excluded from the Docker build context as well
as appropriate Git ignores. These excludes do not remove secrets from previously built images;
review any existing image separately before distributing it.

Run the read-only preflight from the checkout or API image using the deployment's environment:

```powershell
python -m fair.operations.preflight --config-directory config --check-database
```

It validates credential separation/presence, routing thresholds and policies, review-file parsing,
provider admission, opt-in adapter construction and the database's revision marker. It performs
zero provider calls and never migrates, resumes, resets quotas or creates a missing SQLite DB.
It reports only check names and booleans, and exits nonzero on failure. Omit `--check-database`
for configuration-only inspection; `database_checked: false` is not deployment readiness.
Use `--allow-demo` only for an intentional offline demo. Live and demo modes cannot be combined.
PASS does not prove remote availability, account eligibility beyond supplied attestations,
write privileges, full schema equivalence, sandbox readiness or model quality.

## Health and monitoring

| Endpoint | Meaning |
| --- | --- |
| `GET /livez` | Process responds, independently of the database, global stop and provider availability. |
| `GET /readyz` | 200 only when schema marker is current, database/system state is readable, credentials are configured, global stop is clear and scheduler is open; otherwise generic 503. |
| `GET /healthz` | Existing compatibility metadata. Use the dedicated probes above for new monitoring. |
| `GET /v1/system/status` | Admin-only operational counts and queue snapshot; generic 503 if unavailable. |

Readiness means the API can admit a request for normal routing or escalation. It does not require
an available live model, free quota or a passing benchmark. The public response exposes only
`ready` / `not_ready`, without database URLs, credentials, provider inventories or client data.
Database connection/query deadlines depend on deployment database settings; configure appropriate
PostgreSQL `connect_timeout` and statement timeouts for the host's probe deadlines.

The admin status reports schema/credential state, global stop, scheduler load, configured live
adapter count, durable security/exhaustion counts, active cache entries and trailing-24-hour
request/cache-hit counts. Counts include foreground and shadow request statuses. It does not
return task text, answers, client identities, key material or raw database diagnostics. These
are operational observations, not availability/quality guarantees or a new alert scheduler.
Monitor meaningful changes: not-ready, growing queues/failures, blocked providers and exhausted
quotas. Use existing provider health, performance and owned audits to investigate.

The API container now uses a read-only root, bounded temporary storage, dropped Linux
capabilities, no privilege escalation, PID/memory limits and a liveness healthcheck. Compose
health uses `/livez`, so an intentional stop does not look like process failure. API/database
ports remain loopback-bound, and the API runs one worker. Native sandbox execution is still a
separate opt-in capability; no Docker socket is mounted into this API container. These controls
follow [Docker Compose service configuration](https://docs.docker.com/reference/compose-file/services/).

## Stop, back up, migrate and verify

1. Stop new client submissions at the caller boundary. POST `/v1/system/stop` with the admin key.
   Wait until both scheduler `active` and `inflight` are zero. Stop drains queued work and allows
   active cleanup; do not assume it cancels the current provider request. Readiness should now
   be 503 and liveness 200.
2. Stop the API container while preserving the database volume. Keep the maintenance stop in
   the backup. Save the tested code/image and private configuration alongside the backup inventory.
3. Create a PostgreSQL custom-format backup into a new, protected destination. In PowerShell,
   use a container file and `docker compose cp`; do not pipe binary dump bytes through text output.
   The following filenames are examples: choose a fresh name for each backup and check success
   after every command.

```powershell
docker compose -f infra/docker/docker-compose.yml stop api
docker compose -f infra/docker/docker-compose.yml exec -T db pg_dump -U fair -d fair -Fc --file=/tmp/fair-stopped.dump
New-Item -ItemType Directory -Force secrets/backups
docker compose -f infra/docker/docker-compose.yml cp db:/tmp/fair-stopped.dump secrets/backups/fair-stopped.dump
Get-FileHash -Algorithm SHA256 secrets/backups/fair-stopped.dump
```

The development database role/name above match the supplied Compose file. Use your actual
deployment credentials through protected configuration. A dump contains accepted outputs and
other private history: encrypt/restrict backups, retain an off-host copy, record the checksum
and test a restore. `pg_dump` gives a consistent database snapshot; a custom dump is restored
with `pg_restore`. Per-database dumps do not include cluster roles/tablespaces; retain their
definitions separately. See [PostgreSQL backup guidance](https://www.postgresql.org/docs/17/backup-dump.html).

4. Restore and verify the backup in a **new disposable database** before relying on it. Never
   restore over the only working database. For a same-role local verification, choose an unused
   database name and use the existing container dump:

```powershell
docker compose -f infra/docker/docker-compose.yml exec -T db createdb -U fair -T template0 fair_restore_check
docker compose -f infra/docker/docker-compose.yml exec -T db pg_restore -U fair -d fair_restore_check --exit-on-error --single-transaction /tmp/fair-stopped.dump
docker compose -f infra/docker/docker-compose.yml exec -T db psql -X -U fair -d fair_restore_check -c "SELECT version_num FROM alembic_version; SELECT stopped FROM system_state; SELECT count(*) FROM task_requests; SELECT count(*) FROM audit_events;"
```

If restoring an off-host copy, copy it into the database container first. Check revision, request
and audit counts, quota/security state, accepted history and append-only protection. Keep restored
instances stopped and without provider credentials until the operator chooses a cutover. Restoring
older state also restores older quota observations: reconcile against current account limits before
resuming. A backup does not undo provider calls made after its snapshot.

5. Build the selected application image and apply `alembic upgrade head` through the migration
   service while the API is stopped. Then run `alembic check` against the same database. Step 10
   introduces no new migration; the current revision remains `0008`. Never use `alembic stamp`
   as a substitute for migration. With Compose:

```powershell
docker compose -f infra/docker/docker-compose.yml build api migrate
docker compose -f infra/docker/docker-compose.yml run --rm migrate
docker compose -f infra/docker/docker-compose.yml run --rm migrate alembic check
docker compose -f infra/docker/docker-compose.yml up -d api
```

6. Verify liveness, admin status, expected schema and preflight. Readiness stays 503 while the
   persisted maintenance stop is set. Inspect recovery state and history before explicitly
   resuming. After resume, verify readiness and a reviewed smoke request. Do not enable live
   providers merely to make readiness pass.

## Failure and rollback

If migration/preflight fails, leave the API stopped. Capture the safe check codes and inspect
private diagnostics locally; do not paste keys, raw responses or database URLs into incident
reports. Prefer a forward fix compatible with the preserved database. If rollback is necessary,
validate the previous image against a restored copy and plan a deliberate cutover. Do not
blindly downgrade a database containing later data or reset durable quota/security state.

Use [operational recovery](OPERATIONS_SECURITY.md) for authentication blocks, interrupted
requests, half-open circuit probes and independently observed quota resets. A billing-policy
violation requires investigation before clearing the security block or resuming. Missing keys,
stale reviews and unavailable cloud accounts remain operator configuration issues, not permission
to switch to a paid route. Cache clearing affects lookup references, not private history.

## Verification evidence

Unit/API tests cover schema mismatch, missing credentials, stop/close, database failure,
sanitized status, aggregate counts, preflight parsing and read-only behavior. The packaged CI
smoke runs preflight and probes, proves a private build-context marker is absent, performs
`pg_dump` / `pg_restore` into a separate database, checks preserved request/audit/quota/stop state,
verifies restored append-only protection, then checks restart and explicit resume. Its entire
stack uses disposable credentials and offline adapters. The source database is never overwritten.

These checks do not establish high availability, disaster-recovery timing, remote network/TLS
deployment, managed secrets, distributed scheduling or real cloud provider qualification.
