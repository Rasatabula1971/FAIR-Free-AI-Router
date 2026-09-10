# Credentials and operational recovery

Step 6 adds credential boundaries and explicit, audited maintenance. Continue running one
API worker. These controls do not establish process sandboxing, distributed coordination,
production network security, or a managed secret service. Live providers remain disabled.

## API credentials

Client keys must be unique and distinct from the administrator key. Ambiguous, malformed,
empty client keys and whitespace-containing keys fail application construction with a generic
configuration error. No configured clients or an empty admin key disables that role. Use
independently generated high-entropy credentials; the parser does not estimate key entropy.

The authentication closure keeps SHA-256 digests and client identities. It does not retain raw
keys. Original environment variables and caller-owned configuration still exist outside this
closure. Exactly one `X-API-Key` header is accepted. Duplicate headers, role substitution and
body identity spoofing fail authentication/authorization. Key comparisons use fixed-size digests.

Either inject the existing `FAIR_CLIENT_KEYS` JSON object and `FAIR_ADMIN_KEY` raw string, or
use `FAIR_CLIENT_KEYS_FILE` and `FAIR_ADMIN_KEY_FILE`. The client file contains a JSON object;
the admin file contains a JSON string. Files are capped at 64 KiB; duplicate JSON keys, missing
files, invalid JSON and simultaneous raw/file configuration fail closed without echoing values
or paths. Constructor overrides used by tests take precedence over environment sources.

Mount files read-only with restrictive OS permissions. The application does not provision file
ACLs or mount secrets. The supplied Compose development stack still expects environment keys;
customize its mounts/environment to use file secrets. Never commit credentials. The repository
ignores `secrets/` as a convenience, not a secret-scanning guarantee.

Rotation requires restart: replace the configured value/file, stop and drain the old instance,
then start a fresh instance. There is no overlapping-key or hot-reload window. TLS termination,
request/body/connection limits, database account privileges and log access remain deployment
responsibilities. Keep upstream SDK logging and error debugging from exposing authentication.

Validation failures return a generic HTTP 422 response without echoing submitted fields.
Private request/history/audit access remains bound to the owning client's key. Recovery and
aggregate performance/scheduler views require the administrator role.

## Provider credential boundary

`ProviderCredentials` resolves only an operator-supplied mapping from provider IDs to exact
environment variable names. Missing/unmapped credentials fail closed with generic messages.
`Registry.register_credentialed(spec, factory, credentials)` checks provider admission before
calling a trusted factory with that provider's `SecretStr`. Credentials never enter provider
specifications, normalized model requests, routing DTOs or database snapshots.

The returned adapter wrapper checks request, response and discovery string fields for accidental
reflection of its exact credential. It blocks reflection, normalizes exception text and preserves
typed quota/rate/authentication failures. Credential reflection produces a security block through
the existing router; it is not a model-quality measurement. Normal cancellation propagates.

Step 8 uses this boundary for the opt-in Groq and OpenRouter factories; see
[live adapter setup](LIVE_ADAPTERS.md). Factories remain disabled by default.
Adapters remain trusted code: they can read process memory/environment,
must use the key only for the intended provider transport, and must not log it. Exact-string
checks cannot detect every encoding, transformation or independently obtained secret. `SecretStr`
masks ordinary representations; it does not encrypt memory or prevent explicit extraction.

## Provider recovery workflow

1. Stop dispatch with `POST /v1/system/stop` using the admin key. Wait for `inflight: 0` and
   `active: 0` from `GET /v1/system/scheduler`. Stop drains queued work; active cleanup must finish.
2. Correct the underlying issue outside FAIR: rotate the credential, investigate the outage or
   independently confirm a new quota window. Restart after credential changes; the stop persists.
3. Read `GET /v1/system/providers/{provider_id}/recovery`. It returns stored runtime state and
   an `expected_state` fingerprint. This inspection does not reset counters or probe providers.
4. POST the chosen action to `/v1/system/providers/{provider_id}/recover`, including the exact
   fingerprint and a nonempty `review_reference` pointing to the operator's incident/evidence.
5. Inspect the result and provider health; explicitly resume when ready. Recovery never resumes
   dispatch or changes configured costs, terms, privacy, provider status, models or quality gates.

Example body after credential rotation:

```json
{
  "action": "clear_authentication",
  "expected_state": "<64-character fingerprint from the inspection endpoint>",
  "review_reference": "incident-2026-09-09-provider-a"
}
```

| Action | Effect |
| --- | --- |
| `clear_authentication` | Clears only the persisted authentication/security block. |
| `rearm_circuit` | Allows the existing governor to take one half-open probe after resume; does not assert health or clear quota/security blocks. |
| `confirm_quota_reset` | Clears usage/exhaustion only after explicit operator attestation of a new quota-window boundary. Other blocks remain. |

For `confirm_quota_reset`, include a timezone-aware `observed_reset_at` timestamp. It must be
within the preceding 24 hours, not in the future, and strictly newer than both the last reserved
request and the last accounted quota reset. This prevents forgiving requests already made in
the new window or replaying an old reset. The endpoint trusts the authorized operator's external
observation; it does not contact the provider or independently verify the evidence. A quota
top-up or wish to retry is not a confirmed new window. Other actions reject reset timestamps.

Successful changes write before/after state, action and a SHA-256 hash of the review reference
to append-only audit history in the same transaction. The hash links to an externally retained
reference without storing arbitrary private text. Audit failure rolls back recovery. Successful
replays with the old fingerprint return `RECOVERY_STATE_CHANGED`; fetch state and review again.
There is no automatic retry or blanket reset endpoint.

HTTP 409 also covers `SYSTEM_MUST_BE_STOPPED`, `REQUESTS_STILL_IN_FLIGHT`, `NO_RECOVERY_NEEDED`,
`INVALID_RESET_OBSERVATION`, and `RESET_OBSERVATION_ALREADY_ACCOUNTED_FOR`. Unknown providers
return 404, invalid bodies 422 and unauthorized callers 403. Denied operations do not modify
runtime state. Administrator audit attribution currently uses the single role identity `admin`.

Run `alembic upgrade head` before starting this version. Migration `0006` adds durable last
reservation/reset timestamps. Legacy nonzero usage receives a conservative migration-time
reservation boundary; recovery requires a confirmed reset after that boundary. Normal trusted
adapter reset observations continue through the governor and update the recorded reset boundary.

## Interrupted request reconciliation

A crash can leave nonterminal request metadata after the in-memory queue is lost. While stopped
and idle, POST `/v1/system/requests/recover` with a timezone-aware `created_before`, a
`review_reference`, and optional `limit` (default 100, maximum 1,000). Use a cutoff before the
crash/restart. The cutoff cannot be in the future.

The operation marks eligible nonterminal rows without results as `FAILED` with reason
`PROCESS_INTERRUPTED`. It preserves existing attempts, audit history, accepted results, terminal
failures/cancellations and provider reservations. It neither replays prompts nor invents output.
Each changed request gets a client-isolated audit event; the admin response contains counts only,
the cutoff, limit and hashed review reference. Repeat the same cutoff until the count is zero.

Recovered history uses a compact failure object; original attempts remain in the request audit.
Clients must explicitly decide whether to submit again because a crashed request may already
have consumed provider quota. Recovery is manual, limited to this single-worker deployment, and
cannot establish that a separate worker is inactive. Database-owner DDL and out-of-process
maintenance remain outside the application audit and locking boundary.
