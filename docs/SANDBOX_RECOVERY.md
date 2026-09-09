# Native sandbox recovery

The native executor still runs only admitted numeric/list functions in isolated local Docker
containers. Each Docker operation has a ten-second deadline; the generated process has a
two-second CPU limit and the existing memory/process/filesystem/network restrictions. Recovery
adds bounded cleanup after interruption or process restart. It does not enable providers or
grant generated code access to the Docker daemon.

## Configure an owner

Build the current trusted sandbox image as described in [QUALITY_CONTRACTS.md](QUALITY_CONTRACTS.md),
then set these variables for the host-run API:

```powershell
$env:FAIR_SANDBOX_IMAGE = docker image inspect fair-native-sandbox --format '{{.Id}}'
$env:FAIR_SANDBOX_OWNER = 'fair-development'
```

Use a stable, nonsecret owner unique to this deployment. It accepts one to 64 letters, digits,
dots, underscores or hyphens, starting with a letter or digit. Keep the same owner across process
and image restarts. Do not reuse another application's owner. No owner means automatic recovery
is disabled; ordinary per-candidate cleanup still runs.

The executor labels each new container with `io.fair.sandbox=1`, its `io.fair.owner`, and a UTC
Unix `io.fair.lease-until` timestamp 120 seconds after creation is requested. Generated code
cannot edit these daemon-managed labels and never receives the Docker socket. The lease exceeds
the normal bounded create/start/cleanup operations. Keep the host clock accurate: a forward
clock jump can expire a lease early, and a backward jump can delay collection.

## Automatic reconciliation

With an owner configured, reconciliation runs at API startup and before each admitted native
candidate. Both reconciliation and candidate execution use the executor lock. A restart lists
containers using exact manager and owner label filters, then independently validates the complete
returned inventory: full container IDs, FAIR-generated names, ownership labels and lease values.
Malformed or oversized inventories block recovery before any deletion.

Only expired leases are removed, by exact container ID. Unexpired containers are preserved,
including work from another executor using that owner. Different owners, unlabelled historical
containers, unrelated services, volumes and images are untouched. No daemon-wide prune runs.
An expired abandoned container may be removed even if its process is still running.

The inventory budget is 64 containers, the reconciliation deadline is 30 seconds, and each
Docker command retains its own deadline and output limit. Missing containers are handled
idempotently only when a successful daemon listing confirms absence. An unavailable daemon
does not count as proof that cleanup succeeded.

An executor remembers uncertain cleanup targets. If one still has an active lease, recovery
preserves it and leaves the executor unhealthy. After expiry, successful cleanup or confirmed
absence clears that state. A failed startup reconciliation prevents the API from serving. A
runtime recovery failure withholds the candidate and remains a validator-service failure,
without lowering measured model quality or provider availability.

Normal cancellation protects the forced-removal task even if more cancellation requests arrive.
Abrupt process termination cannot finish that task; a later owner-scoped reconciliation handles
the labelled residue. Recovery runs on startup, native use or the explicit endpoint below. It
is not a background scheduler: an abandoned lease can remain after expiry until one of those
triggers occurs. If the owner changes, the old owner's residue needs an explicit operator review.

## Recover through the admin API

To reconcile without running inference:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/system/sandbox/recover' `
  -Headers @{ 'X-API-Key' = $env:FAIR_ADMIN_KEY }
```

The admin key is required; client keys receive HTTP 403. Success returns only counts and health:

```json
{"removed": 1, "preserved": 0, "healthy": true}
```

HTTP 409 means an owned native sandbox is not configured. HTTP 503 means reconciliation failed
or known cleanup remains uncertain. Attempts reaching recovery are recorded as append-only
`SANDBOX_RECOVERY` audit events with the result, without container IDs, input values, code or
private Docker error text. Ordinary authentication failures do not invoke recovery.

The endpoint neither bypasses leases nor resumes inference after unrelated stop/security blocks.
For an active lease left after failed cleanup, retry once it has expired. Investigate unavailable
Docker or malformed ownership metadata before retrying. The service never guesses that an
unrelated container is disposable.

The default development Compose API has no Docker-daemon access and does not forward these
variables. This remains an opt-in, host-run, single-worker development executor. Linux container
behavior is exercised in GitHub CI; Windows Docker behavior remains unverified. Shared-kernel
containers and a privileged host Docker daemon are not a production hostile-code isolation claim.

Docker's [label filtering](https://docs.docker.com/reference/cli/docker/container/ls/) provides
the initial inventory; FAIR's additional validation defines what this executor may remove.
