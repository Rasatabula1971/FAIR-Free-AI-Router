# SDKs and exact caching (Step 9)

The Python sync/async and Node.js clients use the existing authenticated FAIR API. They never
receive provider credentials. Exact caching is server-side, opt-in, persistent and isolated by
authenticated client, including PUBLIC requests. Apply migration `0008` before starting this
version against an existing database: `python -m alembic upgrade head`.

## Python

Install the repository package (`python -m pip install -e .`) with Python 3.12+. Supply a FAIR
client key from your application's secret environment; this is not a Groq/OpenRouter key.

```python
import os
from fair.sdk import Client, FAIRClientError

with Client(
    "http://127.0.0.1:8000", client_id="my-app", api_key=os.environ["FAIR_API_KEY"]
) as fair:
    result = fair.solve("Compute 2 + 2", validation={"kind": "arithmetic", "expression": "2 + 2"})
    if result.status == "ACCEPTED":
        print(result.output, result.verification_state, result.cache_hit)
    else:
        print(result.status, result.reason_code)
```

Use `async with AsyncClient(...) as fair` and `await fair.solve(...)` for async code.
All other methods are also awaitable on `AsyncClient`. Sync `close()` and async `aclose()`
are available when not using a context manager. Solve returns a validated `SolveResponse`;
other methods return decoded API objects or lists. Validation options match `SolveRequest`.
The client supplies its configured `client_id` and rejects identity overrides.

## JavaScript / TypeScript

Node.js 22+ is required. The local ESM package has no npm dependencies and includes TypeScript
declarations. Install it from the checkout (`npm install /path/to/FAIR/sdk/javascript`) or import
`sdk/javascript/index.js` directly. It is marked private and has not been published to npm.

```javascript
import { FAIRClient, FAIRClientError } from '@fair-router/client';

const fair = new FAIRClient({
  baseUrl: 'http://127.0.0.1:8000',
  clientId: 'my-app',
  apiKey: process.env.FAIR_API_KEY,
});
const result = await fair.solve('Compute 2 + 2', {
  validation: { kind: 'arithmetic', expression: '2 + 2' },
});
if (result.status === 'ACCEPTED') console.log(result.output, result.cache_hit);
else console.log(result.status, result.reason_code);
```

Pass `{ signal: controller.signal }` as the third argument to `solve`, or the last call-options
argument to other methods, for cancellation. This is a server-side SDK: do not embed durable
FAIR keys in public browser bundles. The declarations describe the envelope; the server remains
the authoritative validator for detailed contracts and policies.

| Operation | Python | JavaScript |
| --- | --- | --- |
| Solve | `solve(task, **options)` | `solve(task, options, callOptions)` |
| List providers | `providers()` | `providers()` |
| Owned request/history | `request(id)` | `request(id)` |
| Owned audit | `audit(id)` | `audit(id)` |
| Submit feedback | `feedback(id, accepted=True)` | `feedback(id, {accepted: true})` |
| Read feedback | `request_feedback(id)` | `requestFeedback(id)` |
| Clear own cache | `clear_cache()` | `clearCache()` |

Neither SDK retries requests automatically. A timeout or disconnected response does not prove
that inference never started; a caller retry can consume another quota unit. Exact caching is
not idempotency or crash replay. Ordinary escalation and service-failure envelopes are returned
as results; inspect `status` before using output. HTTP failures raise `FAIRClientError` with
`code` and Python `status_code` / JavaScript `statusCode`. Raw error bodies, upstream exception
text and credentials are excluded from these errors. A cost-policy HTTP 503 is an error, never
a normal response asserting free execution.

Both clients reject credential-bearing URLs and remote plaintext HTTP, prevent redirects,
and bound decoded responses to 2 MB. Python also disables environment proxies and rejects
compressed responses. JavaScript bounds fetch's decoded stream. Configure transport at the
trusted host boundary; custom test transports/fetch implementations are trusted caller code.
Default Python HTTPX operation timeout is 180 seconds; JavaScript's total deadline is 180,000 ms.
Raise it explicitly if server queue/attempt settings require more time. Python cancellation uses
normal asyncio task cancellation; JavaScript distinguishes `CANCELLED` and `TIMEOUT`.
Connection lifecycle follows [HTTPX client guidance](https://www.python-httpx.org/advanced/clients/),
and JavaScript uses [Node AbortSignal](https://nodejs.org/api/globals.html#class-abortsignal).

## Cache policy

Set these server options in `config/routing.yaml` (the shipped `cache_enabled` is false):

```yaml
cache_enabled: true
cache_ttl_seconds: 3600
cache_max_entries: 1000
```

The initial cache supports accepted **arithmetic and host-reference JSON** contracts only.
Freshness flags, current/research/source-sensitive profiles, evidence, source policies,
benchmark policies, native/code execution, cross-checks, high-impact requests and shadows
bypass it. Fresh/current reuse remains unsupported even when a client supplies a TTL.
Semantic matching and retrieval/prompt-prefix caches are outside this increment.

Request controls are `cache_mode: default | bypass | refresh` and optional
`cache_ttl_seconds` (1–86400). `bypass` performs no cache reads or writes; `refresh` removes
the matching entry and executes a new attempt, replacing the entry only on acceptance. A
failed refresh cannot resurrect the old entry. Client TTL can shorten the server maximum but
cannot extend an existing expiry. Reads never renew expiry. A backward clock invalidates an
entry until a newly executed result is stored.

Keys hash the exact task, client identity, validation contract (including host reference),
schema, privacy, task profile, quality threshold, engine/cache version, routing policy and
configured provider/model metadata. Objects and sets are canonicalized; whitespace and array
order are preserved. Priority and cache controls do not change answer identity, but scheduler
authorization still runs on every request. Provider/model revisions or policy changes cause
misses. Hashes are internal lookup identifiers, not encryption of request content.

Every hit receives a new request ID and owned history/audit records. It references the original
accepted request through `cached_from_request_id`, reports `cache_hit: true`, has zero attempts,
and reruns the current deterministic validator before release. It consumes no provider quota
and creates no new model-quality sample or shadow dispatch. The original provider/model remains
attribution for the stored answer. A cached result may be served during quota exhaustion or an
outage because it does not call the provider; the global stop, security blocks, admission
revocation, disabled models and live account-review expiry still prevent reuse.

Feedback on a cache hit returns 409 `FEEDBACK_USE_ORIGINAL_REQUEST`; submit it against the
owned `cached_from_request_id`. This prevents repeated cache hits from multiplying one answer's
feedback weight. Negative original feedback (score below 50) or a submitted correction prevents
future reuse of that entry. Hit requests do not enlarge the shadow sampling budget.

`DELETE /v1/cache` requires a client key and deletes only that client's entries. Clearing the
cache does not delete accepted request history or audit records, and an already running
inference may later store a new entry. Global capacity evicts the oldest entries; expired
entries are pruned on writes, and expired lookups are removed. This is bounded lookup storage,
not a history-retention mechanism or per-client capacity guarantee.
An optional cache-write database failure preserves an already persisted accepted result and
records `CACHE_WRITE_FAILED` when the audit database is available. Cache-read failures fail the
request before inference; they do not authorize unchecked output release.

The cache stores only hashes, owner, expiry and a foreign-key reference to the existing result;
it adds no duplicate response text or raw task/reference payload. No external cache service is
required. Single-process scheduling serializes lookup/execute/store so simultaneous identical
requests reuse the first accepted result. Distributed locks and exactly-once processing remain
outside scope. Migration downgrade drops lookup entries and preserves request/audit history.

## Verification

Tests cover Python sync/async transport, cancellation, sanitized errors and identity binding;
Node transport and real HTTP solve/cache/history/feedback/clear operations; cache isolation,
revalidation, invalidation, expiry, bounded eviction, concurrency and feedback/shadow accounting;
and SQLite/PostgreSQL migration preservation. No cloud inference or new provider activation
is needed for SDK/cache tests. CI installs Node 24 and runs the JavaScript suite through the
actual FastAPI HTTP integration test.
