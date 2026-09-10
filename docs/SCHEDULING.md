# Fair client scheduling

Run one API worker and one router on one asyncio event loop. The scheduler admits a bounded
number of requests and executes one complete solve at a time. Provider admission, quota
reservation, quality checks and independent verification still run through the existing router.

## Priorities and fairness

| Class | Intended use |
| --- | --- |
| P0 | Security or system emergency |
| P1 | Critical interactive work |
| P2 | Normal user-blocking work; request default |
| P3 | Batch and background work |
| P4 | Benchmark and shadow work |

The next turn goes to the most urgent nonempty class. Within that class, clients take turns
round-robin, with FIFO order for each client's requests. A client that adds work while its
request runs goes behind other clients waiting in that class. Clients cannot gain more turns
by opening additional connections with the same identity. Global and per-client queue caps
apply across all classes.

A turn is a whole request, including its configured primary attempts and verification budget.
Priorities do not interrupt a running request. This gives fairness in request turns, not equal
token usage, compute time or response latency. An admitted, continuously waiting client cannot
be overtaken indefinitely by another client at the same priority. Sustained higher-priority
traffic can starve lower classes; their queue deadlines return a failure instead of waiting
forever. No aging or priority promotion overrides strict priority.

## Configuration and access

`config/routing.yaml` contains these defaults:

```yaml
scheduler:
  max_queued: 128
  max_queued_per_client: 16
  max_queued_bytes: 8000000
  max_request_bytes: 1000000
  queue_timeout_seconds: 60
  client_priority_ceiling: {}
```

The authenticated `client_id` determines its most urgent permitted class. Unlisted clients
may request P2, P3 or P4. To allow a specific authenticated client to use P1, configure:

```yaml
  client_priority_ceiling:
    interactive-service: P1
    offline-benchmark: P4
```

P0 requires explicit operator authorization through this mapping. A caller cannot grant itself
priority by changing its body identity: the API requires that identity to match its client key.
Clients restricted to P3/P4 must explicitly request that class because requests default to P2.
Restart the service after changing configuration. The mapping grants scheduling urgency only;
it does not bypass provider governance, privacy, quota, source or benchmark gates.

An example body for an ordinary client:

```json
{
  "client_id": "batch-service",
  "priority": "P3",
  "task": "Calculate the expression",
  "validation": {"kind": "arithmetic", "expression": "2 + 2"}
}
```

Queue count and byte limits exclude the one active request. The per-request byte limit applies
even when the router is idle. Sizes count the normalized request's serialized UTF-8 JSON,
including evidence and validation data. They bound retained queued payloads, not total Python
heap usage or pre-admission HTTP parsing; apply inbound body and connection limits at the
deployment boundary. The router freezes queued input so later SDK-side mutation cannot change
identity or validation data. Queue deadlines use monotonic time and cover waiting only.

## Results and cancellation

Scheduling refusals retain the solve API's HTTP 200 response envelope with `status: "FAILED"`,
an empty `attempts` list and no output. Clients must inspect `status` and `reason_code`:

| Reason | Meaning and response |
| --- | --- |
| `PRIORITY_NOT_ALLOWED` | Use a permitted class or request an operator configuration change. |
| `REQUEST_TOO_LARGE` | Reduce the serialized request size. |
| `CLIENT_QUEUE_FULL` | This client has reached its waiting-request cap. |
| `QUEUE_FULL` | The router has reached its total waiting-request cap. |
| `QUEUE_BYTES_FULL` | The queued payload byte budget is exhausted. |
| `QUEUE_TIMEOUT` | No execution turn was obtained before the waiting deadline. |
| `SYSTEM_STOPPED` | Admission was stopped or pending work was drained by the admin stop. |
| `SCHEDULER_CLOSED` | The router is shutting down and accepts no further work. |

Queue pressure and timeouts consume no provider quota and generate no model-quality samples.
Retry with bounded backoff and jitter, reducing outstanding work when the client cap is reached.
There is no automatic retry, durable replay or idempotency key in this increment.

Cancellation of a queued coroutine, or an observed HTTP disconnect, removes its ticket and
persists `CANCELLED`. Cancelling an active solve keeps its turn until provider/native cleanup
finishes; the next client cannot overlap that cleanup. Provider reservations already taken
remain conservative. HTTP disconnect notification depends on the ASGI server and any proxy.

The admin stop bypasses the solve queue, persists the existing global kill switch, and drains
this process's waiting tickets. Resume does not resurrect drained work. Running attempts are
not interrupted by stop; existing checks prevent subsequent dispatch while stopped and withhold
results as previously implemented. A switch changed through another process is checked by the
router before dispatch, but does not proactively drain this process's queue.

Lifespan shutdown closes admission, rejects waiting requests and cancels/awaits active cleanup
before disposing the database engine. An injected router is closed when its app lifespan ends;
construct a fresh router for a new lifespan. Arbitrary adapters must cooperate with asyncio
cancellation; a stuck adapter cannot be safely bypassed to free its slot.

## Operations and persistence

`GET /v1/system/scheduler` requires the admin key. It reports aggregate active/queued counts,
queued bytes, waiting-client count, per-priority counts and whether admission is closed. It
contains no client identities, request IDs, prompts, evidence or credentials.

Request history records `QUEUED`, `SCHEDULED` (priority and elapsed wait), and the usual terminal
events. Rejections and cancellations remain client-isolated. Raw queued input is held only in
memory; the database retains profile metadata and audit events, not a replayable queue. No
schema migration or quality-engine version change is needed.

The queue is local and volatile. A hard process crash loses queued payloads and may leave
nonterminal request rows; startup does not reconcile those rows or replay work. Step 6 adds
[manual stopped-and-idle reconciliation](OPERATIONS_SECURITY.md) without replay. This increment
does not implement a distributed scheduler, durable queue recovery or production resource
isolation. Run one worker. Live providers remain
disabled.
