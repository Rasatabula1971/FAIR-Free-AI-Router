# FAIR-Free-AI-Router — Adversarial Re-Audit

**Audit date:** 30 September 2026 (America/Port_of_Spain)  
**Repository:** https://github.com/Rasatabula1971/FAIR-Free-AI-Router  
**Revision:** `main` at `cb309f557e58bc9141701e71364d75ebb51d36c8`  
**Method:** saved Code Evaluation Skill + Comprehensive Adversarial Software Bug Audit.  
**Audit type:** source review, cross-file failure tracing, existing-test inspection, and inspection of recorded GitHub Actions results.  
**Execution limit:** No shell/Python runner was available in this session. No new Python reproductions, live provider calls, Windows runs, or scanner runs were executed by this auditor. Source-proven behavior is identified separately from recorded CI results.  
**Change scope:** Report only. Proposed patches below were not applied or tested.

## 1. Executive assessment

FAIR is not yet ready to treat as dependable shared infrastructure for all of your applications.

The existing CI is green, but the reviewed revision contains a Windows startup dependency failure, an unbounded synchronous schema-validation path, an unreachable documented public option, and defects in timeout, quota-recovery, and resource handling. These are executable behavior problems rather than formatting complaints.

### Bug totals

Only Confirmed Bugs and Probable Bugs are counted.

| Severity | Confirmed Bug | Probable Bug | Total |
|---|---:|---:|---:|
| Critical | 0 | 0 | 0 |
| High | 2 | 0 | 2 |
| Medium | 5 | 1 | 6 |
| Low | 1 | 0 | 1 |
| **Total** | **8** | **1** | **9** |

“Confirmed” can mean demonstrated by the supplied code and established dependency semantics. It does not mean that this session executed a reproduction. No exact crash latency or memory-exhaustion threshold is claimed.

### Findings at a glance

| ID | Severity | Classification | Finding |
|---|---|---|---|
| BUG-01 | High | Confirmed Bug | Clean Windows installation can fail while importing FAIR |
| BUG-02 | High | Confirmed Bug | Caller-supplied schema regex can block the shared event loop |
| BUG-03 | Medium | Confirmed Bug | Documented `accept_unverified` option is missing from `FAIR.solve()` |
| BUG-04 | Medium | Confirmed Bug | Ollama completion retains a fixed 25-second read timeout |
| BUG-05 | Medium | Confirmed Bug | Half-open probe expires before the actual completion deadline |
| BUG-06 | Medium | Confirmed Bug | Delayed positive quota observation clears shared exhaustion |
| BUG-07 | Medium | Confirmed Bug | Streaming HTTP error bodies are read without a byte limit |
| BUG-08 | Medium | Probable Bug | SQLite connections depend on garbage collection for closure |
| BUG-09 | Low | Confirmed Bug | Default cache clearing targets the wrong application identity |

## 2. Repository and verification snapshot

Python 3.12+; setuptools; Pydantic; HTTPX; JSON Schema; optional FastAPI/Uvicorn service; SQLite shared request ledger. Cache, performance history, throttles, and circuit state remain process-local. There is no browser frontend in the reviewed application.

The main paths are:

1. `FAIR.solve()` → `SolveRequest` validation → profiling → selector → quota reservation → credential guard/provider adapter → local quality evaluation → optional independent cross-check → response/cache.
2. HTTP bearer authentication → strict service payload → `FAIR.solve(client_id=authenticated_identity)` → native or OpenAI-style response.
3. Quota selection/reservation/observation → asynchronous thread offload → SQLite transaction → shared pool state.

### Recorded CI evidence

[Code Quality run](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/36756828469) is for the exact audited SHA. Its test and static-analysis logs were read.

| Check | Evidence observed | Limit |
|---|---|---|
| Pytest | **818 passed, 1 warning, 7.94 seconds** | Recorded Linux CI execution; not rerun here |
| Coverage | **91.25%**, above the configured 90% floor | Line coverage, not proof of these failure paths |
| Ruff | “All checks passed” | Recorded CI log |
| Ruff formatting | 59 files already formatted | Recorded CI log |
| Mypy | No issues found in 46 source files | Default command; many internal functions are untyped |
| Dependency consistency | No broken requirements | Recorded CI log |
| pip-audit | Successful job/step | Outcome inspected; detailed vulnerability output not independently reproduced |
| Semgrep | Successful job/step | Outcome inspected; no new scan here |
| CodeQL | [Successful workflow](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/36756828365) | Workflow outcome inspected |
| MegaLinter | [Successful workflow](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/actions/runs/36756828401) | Selected linters only; copy/paste and Markdown errors are configured as nonblocking |
| Windows startup | Not executed | Current quality workflow runs on Ubuntu |
| Live billing/provider compatibility | Not executed | No keys, account access, or billable calls used |

The test job used Python 3.12.14, jsonschema 4.26.0, HTTPX 0.28.1, Pydantic 2.13.5, FastAPI 0.142.2, and Uvicorn 0.54.0. These values came from its installation log.

No fresh `compileall`, runtime security reproduction, or local lint command was executed.

## 3. Detailed findings

### BUG-01 — Clean Windows installation can fail while importing FAIR

**Status:** Confirmed Bug  
**Category:** Dependency / Deployment / Date-Time  
**Severity:** High  
**Confidence:** Confirmed for a Windows environment without separately installed IANA data  
**Locations:** [quota.py:16](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/quota.py#L16), [live.py:1056](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/providers/live.py#L1056), [pyproject.toml:10](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/pyproject.toml#L10).

**Trigger:** Install FAIR in a fresh conventional Windows Python environment with no `tzdata` package or manually configured IANA database. Then run `from fair import FAIR`, either launcher, or the service.

**Observed from code / expected:** Import immediately constructs `ZoneInfo("America/Los_Angeles")` in quota.py and again in the Gemini class body. The dependencies do not install `tzdata`. Import can raise `ZoneInfoNotFoundError` before any provider is selected. Expected: the advertised Windows installation imports and starts using its declared dependencies.

**Evidence and root cause:** `fair.__init__` imports the embedded module, which imports quota.py and live.py. These timezone constructors are unconditional, including for offline tests and non-Gemini usage. Python documents that Windows commonly lacks the required IANA database and that absent system data and `tzdata` cause `ZoneInfoNotFoundError`: https://docs.python.org/3/library/zoneinfo.html#data-sources

**Impact / affected components:** Entire embedded API, test console, and service startup on affected Windows installs. Existing machines that already have `tzdata` can conceal the defect.

**Minimum safe fix / proposed patch:**

```diff
 dependencies = [
+    "tzdata",
     "pydantic>=2.10,<3",
```

Add a Windows CI installation/import job. An unconditional dependency also covers stripped Unix installations without system timezone data.

**Regression risk:** Additional dependency; no change to quota reset semantics.

**Exact verification:** In a fresh Windows Python 3.12 venv, install `.[dev,service]` and run `python -c "from fair import FAIR; from zoneinfo import ZoneInfo; print(ZoneInfo('America/Los_Angeles'))"`. Also verify Pacific midnight resets across DST transitions. **Not executed here.**

### BUG-02 — Caller-supplied schema regex can block every service client

**Status:** Confirmed Bug  
**Category:** Security / Performance / Async  
**Severity:** High  
**Confidence:** High; failure mechanism source-proven, elapsed runtime not measured  
**Locations:** [schemas/api.py:81](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/schemas/api.py#L81), [quality/engine.py:27](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/quality/engine.py#L27), [embedded/router.py:189](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/router.py#L189), service solve/chat handlers.

**Trigger:** An authenticated caller supplies a valid object schema whose required string property has an expensive regex: `{"type":"object","properties":{"value":{"type":"string","pattern":"^(a+)+$"}},"required":["value"],"additionalProperties":false}`. A provider returns `{"value":"aaaa...!"}` with a sufficiently long run of `a` followed by `!`.

**Observed from code / expected:** The schema passes syntax and size checks. `Draft202012Validator(...).validate(data)` runs synchronously inside the async routing task. jsonschema 4.26.0 implements `pattern` and `patternProperties` with Python `re.search`, without a matching-time budget. Catastrophic backtracking can occupy the event-loop thread. Expected: untrusted validation work has an enforced resource budget and cannot stall unrelated requests or health checks.

**Evidence:** Exact dependency source inspected: https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/_keywords.py (patternProperties near line 16; pattern near line 215). The schema's 20,000-character bound and response's 100,000-character bound do not bound regex computation. The router's `asyncio.wait_for` wraps provider completion, not quality evaluation; even an outer async timeout cannot preempt synchronous matching on the same loop.

**Root cause / impact:** Arbitrary validation code paths are treated as computationally safe because their inputs are length-bounded. A compromised app, accidental pathological schema, or matching provider response can freeze the single shared service process. Authentication reduces who can trigger this; it does not protect other authenticated applications.

**Minimum safe fix:** Run arbitrary schema validation in a supervised, bounded worker process that can be terminated on budget expiry. Until that exists, reject dynamically supplied regex-bearing schemas or accept only explicitly reviewed patterns. A thread plus `wait_for` is not a complete kill mechanism for Python regex execution.

**Illustrative patch placement, not a complete implementation:**

```diff
 # SolveRequest schema admission
+enforce_schema_execution_policy(self.expected_schema)
 Draft202012Validator.check_schema(self.expected_schema)
```

The admission helper must walk actual schema nodes, including `patternProperties`, `propertyNames`, combinators, definitions, and nested schemas. It must not mistake a JSON property literally named `pattern` or enum data for a regex keyword.

**Regression risk:** Rejecting patterns narrows the previously accepted schema contract; a killable worker can preserve schema semantics. Document any temporary restriction rather than silently dropping validation.

**Exact verification:** Use a mock provider to return the JSON object containing the near-match string and invoke the public service with the schema above in a disposable subprocess with a hard external timeout. Verify that validation is refused/timed out under the configured budget, and a concurrent health request remains responsive. Test both `pattern` and `patternProperties`, plus ordinary permitted patterns. **Not executed here.**

### BUG-03 — The documented unverified-answer option is unreachable through the public API

**Status:** Confirmed Bug  
**Category:** API / Logic / Incomplete Implementation  
**Severity:** Medium  
**Confidence:** Confirmed  
**Locations:** [embedded/module.py:656](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/module.py#L656), [schemas/api.py:45](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/schemas/api.py#L45), README “Work nothing can verify”; service payload models.

**Trigger:** Execute the README example `await fair.solve("Explain the trade-offs between X and Y", accept_unverified=True)`.

**Observed from code / expected:** Python argument binding raises `TypeError` because `FAIR.solve()` has no `accept_unverified` parameter. Without the flag, open-ended work still escalates. Expected: opt-in returns the router's distinct `ACCEPTED_UNVERIFIED` state while preserving all existing rejection checks.

**Evidence / root cause:** `SolveRequest`, `SolveResponse`, and `EmbeddedRouter` implement the feature, but the public wrapper never exposes or forwards it. Native and chat payloads also omit the field and forbid extras. `TestAcceptUnverified` tests `EmbeddedRouter.solve(SolveRequest(...))`, bypassing the documented wrapper.

**Impact / affected components:** Python applications cannot use the advertised feature. Planning, creative analysis, and other open-ended requests continue to escalate through ordinary public entry points.

**Minimum safe fix / proposed patch:**

```diff
 # FAIR.solve signature
+        accept_unverified: bool = False,
 # SolveRequest.model_validate payload
+                "accept_unverified": accept_unverified,
```

Expose it in the native service separately if that interface is intended to support opt-in. Keep OpenAI-style chat strict unless its distinct unverified status is deliberately represented in response metadata; merely treating that status as ordinary verified success would introduce a new defect.

**Regression risk:** Preserve the default `False`, rejection of cross-check/high-impact combinations, no caching of unverified output, and distinction from `ACCEPTED`.

**Exact verification:** Test the public `FAIR.solve()` call with a mock provider, default refusal, opt-in response, hard-rejected/truncated response, and conflicting assurance options. If native HTTP support is added, test that boundary as well. **Not executed here.**

### BUG-04 — Ollama still times out after 25 seconds while generating a buffered answer

**Status:** Confirmed Bug  
**Category:** Runtime / API / Timeout  
**Severity:** Medium  
**Confidence:** Confirmed  
**Location:** [live.py:1393–1404](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/providers/live.py#L1393), TextAdapter._json and _completion_timeout.

**Trigger:** A legitimate local Ollama `/api/chat` request takes more than 25 seconds before returning its buffered response, while remaining within FAIR's configured output-based completion deadline.

**Observed from code / expected:** Ollama sends `stream: False`, then calls `_json` without a timeout override. That falls back to the client's fixed 25-second read timeout. A valid generation becomes `PROVIDER_TRANSPORT_FAILED`; the router can penalize the provider and retry elsewhere. Expected: a buffered completion uses the output-based read budget already implemented for other adapters.

**Root cause / impact:** Ollama overrides `complete()` and bypasses the new common completion timeout. This is particularly relevant to small local models on the user's laptop: a 4,096-token request has a default router deadline of 117.4 seconds, but the inner read still stops at 25.

**Minimum safe fix / proposed patch:**

```diff
-        data = await self._json("POST", "/api/chat", payload)
+        data = await self._json(
+            "POST", "/api/chat", payload,
+            timeout=self._completion_timeout(request, streaming=False),
+        )
```

Keep metadata/catalog reads on their existing shorter budgets.

**Regression risk:** Longer occupied local-generation slots; the router deadline still bounds the attempt. Do not accidentally change catalog timeout behavior.

**Exact verification:** Capture the supplied timeout in an adapter test for a 4,096-token request. Also use a real local delayed HTTP server, since HTTPX MockTransport does not itself enforce real socket timeouts: verify a response after 25 seconds but before the completion deadline succeeds, and one beyond the overall deadline fails. **Not executed here.**

### BUG-05 — Half-open circuit probe expires before the permitted request finishes

**Status:** Confirmed Bug  
**Category:** Concurrency / Logic / Date-Time  
**Severity:** Medium  
**Confidence:** Confirmed  
**Locations:** [quota.py:248](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/quota.py#L248), [quota.py:395](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/quota.py#L395), router._attempt.

**Trigger:** A recovering provider has a half-open probe performing a large request. Another solve or provider-status poll reads the governor after the probe's short lease expires while the actual request is still running.

**Observed from code / expected:** `_claim_probe()` sets its lease to `timeout_seconds + 5`: 20 seconds by default. The actual completion gets `attempt_deadline(max_output_tokens)`: up to 900 seconds. `_recover()` reopens the circuit and starts cooldown despite a live authorized probe. With a sufficiently long request, cooldown can expire and another probe can start while the first is still running. Expected: one half-open probe stays exclusively owned for its actual attempt deadline.

**Root cause / impact:** Probe lease still assumes the earlier fixed timeout. Shared clients receive false OUTAGE status, delayed recovery, and potentially overlapping recovery calls. Existing tests establish exclusivity only during initial reservation.

**Minimum safe fix / proposed patch shape:**

```diff
 # Router reservation
-        if not await self.quota.reserve_async(spec, request.client_id):
+        if not await self.quota.reserve_async(
+            spec, request.client_id,
+            probe_timeout=self.settings.attempt_deadline(request.max_output_tokens),
+        ):
 # Governor, after forwarding this value through reservation
-        state.probe_until = self.clock() + self.settings.timeout_seconds + 5
+        state.probe_until = self.clock() + probe_timeout + 5
```

Use a default for existing direct governor callers. Ownership/generation checks should prevent a late old completion from changing a newer probe's state.

**Regression risk:** Recovery remains blocked longer when a probe stalls; cancellation must explicitly release/finalize its slot, while genuinely abandoned probes still expire.

**Exact verification:** With an injected clock and held completion, advance beyond 20 seconds and the old cooldown boundary while staying below the allowed generation deadline. Concurrent reserve/status calls must not open the circuit or dispatch a second probe. Test cancellation and stale completion separately. **Not executed here.**

### BUG-06 — A delayed positive quota observation clears shared exhaustion

**Status:** Confirmed Bug  
**Category:** Data Integrity / Concurrency / Quota  
**Severity:** Medium  
**Confidence:** Confirmed  
**Location:** [quota.py:142–160](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/quota.py#L142).

**Trigger:** Application A observes exhaustion or records an explicit future exhaustion reset. An earlier request from application B subsequently finishes and reports positive remaining quota before that reset.

**Observed from code / expected:** `SharedQuotaLedger.observe()` discards the existing exhausted flag and computes the replacement only from `remaining == 0`. The stale positive observation clears exhaustion. Its reset timestamp can replace a later exhaustion reset. Expected: an older observation cannot undo a still-active shared block.

**Exact source-trace sequence:**

```python
ledger.exhaust("pool", 2000.0, 1000.0)
ledger.observe("pool", 100, 99, 1500.0, 1001.0)
ledger.available("pool", None, 1002.0)  # current implementation returns True
```

The same problem applies with a configured limit of 100 because used becomes 1. This sequence is specified for reproduction; it was not executed here. Mistral has no configured local request limit, so stale positive observations can undermine its shared monthly-exhaustion block.

**Root cause / impact:** A transaction protects atomic updates, but observations have no ordering/window identity and are not monotonic. Other applications can dispatch into known exhaustion, causing unnecessary refusals and retry pressure. No paid-charge bypass is asserted.

**Minimum safe fix / proposed patch:**

```diff
-            used, _, current_reset = self._recover(database, pool_id, now)
+            used, exhausted, current_reset = self._recover(database, pool_id, now)
             used = max(used, observed_limit - remaining)
             if reset_at is not None and now < reset_at <= now + 86400:
-                current_reset = reset_at
+                current_reset = (
+                    max(current_reset, reset_at)
+                    if exhausted and current_reset is not None else reset_at
+                )
-            shared_exhausted = remaining == 0 and current_reset is not None
+            shared_exhausted = exhausted or (remaining == 0 and current_reset is not None)
```

This is a conservative correction; window/observation ordering would support more precise reconciliation.

**Regression risk:** Positive observations cannot prematurely recover an exhausted pool. Legitimate recovery must occur through its trusted reset; test that recovery remains functional.

**Exact verification:** Use two ledger/governor instances sharing one SQLite file. Deliver zero/explicit-exhaustion and stale-positive observations in reverse order, including a month-long reset and a shorter stale reset. A third instance must remain blocked until the established reset, then recover. **Not executed here.**

### BUG-07 — Streaming HTTP error bodies bypass the response byte limit

**Status:** Confirmed Bug  
**Category:** Resource Management / API / Performance  
**Severity:** Medium  
**Confidence:** Confirmed for missing bound; actual memory impact depends on upstream body size  
**Location:** [live.py:416–422](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/providers/live.py#L416).

**Trigger:** Any streaming completion receives a non-200 response with a very large or compressed error body.

**Observed from code / expected:** `_stream_chunks()` calls `await response.aread()` before status-specific handling. It does not enforce the 4,000,000-byte cap or reject compression there. The later 512-character diagnostic truncation happens after the whole body is buffered. Expected: errors obey the same bounded body policy as ordinary JSON responses.

**Evidence / root cause:** `_json()` rejects unsupported compression and reads incrementally with a byte cap; the newer streaming error path duplicates transport handling without those controls. A time budget limits waiting, not bytes received. HTTPX decoding can amplify compressed-body allocation.

**Impact / affected components:** All streaming cloud adapters use this path. Oversized provider/proxy error bodies can consume substantial memory or kill the single shared process. No actual out-of-memory event was observed.

**Minimum safe fix / proposed patch outline:**

```diff
-                    raw = await response.aread()
+                    if response.headers.get("content-encoding", "identity") != "identity":
+                        raise MalformedResponse("COMPRESSED_PROVIDER_RESPONSE_UNSUPPORTED")
+                    parts, byte_count = [], 0
+                    async for part in response.aiter_bytes():
+                        byte_count += len(part)
+                        if byte_count > 4_000_000:
+                            raise MalformedResponse("PROVIDER_RESPONSE_TOO_LARGE")
+                        parts.append(part)
+                    raw = b"".join(parts)
```

Prefer sharing the bounded reader with `_json()`. Separately review success SSE line-buffering, whose length check occurs after a complete line is yielded.

**Regression risk:** Oversized errors become a bounded malformed-response failure rather than a detailed provider-specific quota error; ordinary small 401/429 bodies must retain their classifications.

**Exact verification:** Supply an AsyncByteStream returning non-200 and incrementally more than the cap. Assert it stops reading once the cap is exceeded. Repeat with compression and diagnostics both enabled and disabled, and retain ordinary authentication/quota tests. **Not executed here.**

### BUG-08 — SQLite connections are not explicitly closed

**Status:** Probable Bug  
**Category:** Resource Management / Database / Deployment  
**Severity:** Medium  
**Confidence:** High for unclosed connections; workload-dependent for outage  
**Locations:** [quota.py:45–73](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/quota.py#L45), all SharedQuotaLedger methods using `with self._connect()`.

**Trigger:** Sustained shared-ledger requests create fresh connections, particularly under delayed/disabled cyclic collection, bursts of thread work, or hosts with tight handle limits.

**Observed from code / expected:** The connection context manager commits or rolls back but does not close the connection. Each operation opens a fresh one with no explicit close. Connection disposal is left to object lifetime/garbage collection. Expected: every operation deterministically closes its owned connection on success, early return, and exception.

**Evidence:** Python's documented context-manager semantics: https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager

**Root cause / impact:** Transaction lifetime is mistaken for connection lifetime. Retained connections can accumulate handles and memory or keep files open longer than intended. This is classified Probable because a normal host may collect them frequently enough to avoid an outage; no permanent leak rate or file-lock failure was measured.

**Minimum safe fix / proposed helper:**

```python
from contextlib import contextmanager

@contextmanager
def _transaction(self):
    database = self._connect()
    try:
        with database:
            yield database
    finally:
        database.close()
```

Replace every `with self._connect() as database:` with `with self._transaction() as database:`, including initialization. If connection setup itself raises after opening, close it inside `_connect()` too.

**Regression risk:** Keep commit/rollback behavior and BEGIN IMMEDIATE semantics. Do not replace the transaction context with a closing-only context.

**Exact verification:** Track opened connections using a retained test spy; after every ledger method returns or raises, assert those connections reject subsequent SQL because they are closed. Cover denied reservation and transaction failure. Then perform a bounded concurrent stress run and monitor open handles. **Not executed here.**

### BUG-09 — Default cache clearing misses named applications

**Status:** Confirmed Bug  
**Category:** Logic / Cache  
**Severity:** Low  
**Confidence:** Confirmed  
**Location:** [module.py:784](https://github.com/Rasatabula1971/FAIR-Free-AI-Router/blob/cb309f557e58bc9141701e71364d75ebb51d36c8/fair/embedded/module.py#L784), cache._key/clear.

**Trigger:** Construct `FAIR(application_id="corp")`, cache a supported deterministic solve without overriding client_id, then call `fair.clear_cache()`.

**Observed from code / expected:** Solve defaults client_id to `corp`, while clear_cache defaults to the literal `embedded`. It returns zero removals and leaves the application's cached entry. Expected: omitting client_id clears the instance's default application identity.

**Root cause / impact:** The cache-clear wrapper retained the pre-application-id default. Operators can believe they invalidated results when they did not. Explicit `clear_cache("corp")` remains a workaround.

**Minimum safe fix / proposed patch:**

```diff
-    def clear_cache(self, client_id: str = "embedded") -> dict:
-        return self._router.cache.clear(client_id)
+    def clear_cache(self, client_id: str | None = None) -> dict:
+        identity = self._application_id if client_id is None else client_id
+        return self._router.cache.clear(identity)
```

**Regression risk:** Explicit identity clearing must remain supported; unnamed embedded instances still default to `embedded`.

**Exact verification:** Cache an arithmetic solve through a named FAIR instance, call clear_cache() with no argument, assert one removal, and assert the next solve dispatches to the provider. Repeat with the original default identity and an explicit other client. **Not executed here.**

## 4. Earlier concerns — current status

| Earlier concern | Current evidence | Disposition |
|---|---|---|
| No reusable HTTP service | Native and non-streaming OpenAI-style endpoints now exist; separate client bearer keys and optional admin key | Addressed in implementation |
| Quota is process-local | Optional SQLite ledger and application attribution now exist; reservation uses BEGIN IMMEDIATE | Addressed when configured; BUG-06 and BUG-08 remain |
| Cache/performance/circuit state are process-local | Still true; README limits central service to one worker | Documented limitation, not counted again as a bug |
| 4,096-token universal cap | Reviewed Groq, Mistral, and selected Cloudflare descriptors support larger budgets; Gemini has separate controls | No longer a universal cap; local/unreviewed and individual route limits still apply |
| Large-output timeout | Common cloud completion logic scales budget and streams | Partly addressed; Ollama and half-open leases were missed |
| Provider failure consumes all answer attempts | Separate answered/unanswered budgets and per-route tried set exist | Improved; finite route/budget bounds reviewed |
| Stale provider reviews renewed on restart | Built-in review date is pinned; admission rejects expired/future evidence | Source path addresses the concern |
| Unsupported schema keywords | Provider transport dialect conversion plus full local validation exists | Shape constraints remain locally checked; BUG-02 remains |
| Open-ended work always escalates | Inner router opt-in exists, but public wrapper does not expose it | Unresolved public behavior: BUG-03 |

The router allows one final candidate when the unanswered count equals the configured budget, stopping after the next unanswered failure. This permits a successful candidate after earlier outages; it is not counted as an off-by-one defect without a stricter total-attempt contract.

## 5. Controls actually observed and false positives removed

- Bearer authentication derives service client_id; request JSON cannot override it. Admin resume uses a distinct admin key. Existing service tests cover spoofed client_id and admin-key separation.
- Cache keys include client identity and request/profile details. Grounded/freshness/cross-check/high-impact requests are excluded from deterministic cache reuse.
- Remote built-in adapters are PUBLIC-only; local Ollama requires literal loopback HTTP and local GGUF evidence.
- Credential guards check inputs/results and reduce many upstream errors to secret-safe codes. No actual credential exposure was demonstrated in the reviewed paths.
- Provider admission runs during selection and dispatch; remote reviews expire and dynamic free routes inspect catalog pricing/cost evidence.
- Generated Python is interpreted in a bounded subset; no arbitrary model-authored eval/exec path was found in the reviewed validator.
- Arithmetic uses bounded exact rational evaluation; grounding verifies host-selected JSON pointers and structured fact provenance.
- Failure/capability paths are separated, and cancellation during provider completion does not count as provider failure.

These are source/test observations, not a fresh live attestation of provider availability, pricing, privacy terms, or account billing status.

## 6. Root-cause groups and most dangerous defects

1. **Host and execution safety assumptions:** BUG-01, BUG-02. Required timezone data is assumed present; arbitrary schema matching is assumed computationally bounded. These can stop an entire supported deployment.
2. **Feature implemented below its public boundary:** BUG-03. Tests exercise the internal router and miss the advertised application interface.
3. **Timeout change incompletely propagated:** BUG-04, BUG-05. A fixed inner Ollama timeout and short probe lease remain after generation deadlines were enlarged.
4. **Concurrent shared-state reconciliation:** BUG-06. Atomic persistence does not prevent stale observations from undoing newer exhaustion.
5. **Resource lifetime/bounds lost on separate paths:** BUG-07, BUG-08. Streaming errors bypass body limits; SQLite transaction contexts do not close connections.
6. **Default identity drift:** BUG-09. Solve and invalidation disagree about the application's implicit client.

BUG-02 is the most dangerous shared-service defect: one application's validation request can block unrelated clients and health handling. BUG-01 is the strongest deployment blocker for the user's Windows environment. BUG-07 also threatens process memory under malformed upstream responses.

## 7. Adversarial coverage and remaining limits

| Scenario | What was examined | What was not executed / remaining gap |
|---|---|---|
| Malformed requests | Strict payloads, Pydantic contracts, schema admission, JSON parser | No live malformed-request sweep; expensive valid schemas remain |
| Repeated requests | Cache gating, default identities, per-solve tried routes | No crash-safe idempotency or cross-solve deduplication demonstrated |
| Reordered responses | Quota observe/exhaust transitions | Stale-positive exhaustion failure identified; new regression not run |
| Concurrent requests | Async SQLite offload, transactional reserve, half-open state | Long in-flight probe and observation ordering not covered by reviewed tests |
| Interrupted/partial operations | Transaction contexts, cancellation handlers, stream finish checks | Process-kill/restore and full crash recovery not tested |
| Malformed third-party responses | Identity checks, response envelopes, size limits, SSE aggregation | No live fuzzing; streaming error byte-bound defect identified |
| Expired credentials/reviews | Authentication block, admin resume, date-pinned admission | No provider credential-expiry call performed |
| Authorization-ID tampering | Service client_id exclusion and source-review client ownership | Inspected existing tests; no new HTTP attack execution |
| Retry storms | Finite attempts, failed catalog caching, circuits and throttle logic | No representative shared-client load test or service admission ceiling established |
| Date/time | Pacific/UTC window logic, injected-clock tests, review expiry | No Windows/DST runtime execution; timezone dependency defect identified |
| Financial correctness | Zero-cost parsing, dynamic-route cost rejection, free-account attestations | Actual bills, free-plan configuration and current external prices not independently checked |
| AI/copy-paste artifacts | Public/internal feature mismatch and copied transport paths | Classified executable consequences; no claim about code authorship |
| Frontend | No browser UI in reviewed repo | Not applicable to this application |

Material limits, not inflated bug counts: no durable request replay/idempotency guarantee was established; quota is shared only when all relevant applications use the configured ledger/pool identities; cache/performance/circuit state require the documented single-service-worker deployment. The aggregate quota endpoint intentionally exposes per-application usage to authenticated clients in current tests, so that is treated as the existing operator-service contract, not an inferred authorization bug.

The streaming adapters keep diagnostics and latest quota data on adapter objects. Concurrent response ownership of those observations merits an additional targeted test; no separate defect is counted without isolating its consequence from BUG-06.

## 8. Areas reviewed

**Detailed source tracing:** embedded module/router/selector/quota/cache/performance; config/constants; task profiling; live cloud and local adapters; schema dialect transport; request/response/domain/qualification schemas; admission and qualification; credential guards/resolution; native/OpenAI service and launcher; quality engine, contracts, arithmetic, bounded code validator, grounding, claims, JSON parsing, consensus, and source reviews.

**Supporting configuration inspected:** README, pyproject.toml, Windows launchers, Code Quality/MegaLinter workflows and MegaLinter configuration. Service .env example and schema compatibility tool were inspected for interface context.

**Test inspection:** selected quota, half-open, public-wrapper, unverified-answer, timeout, streaming, local-adapter, and routing paths in test_embedded.py, test_service.py, test_live_adapters.py, test_gemini_and_local_adapters.py, and test_routing_paths.py. Test files were not all independently re-audited line by line. The console was inspected selectively for provider setup/results and execution entry points.

**External evidence:** exact-commit GitHub Actions metadata, pytest/coverage and static-analysis job logs, Python timezone/SQLite documentation, and jsonschema 4.26.0 pattern implementation.

**Not verified:** user's installed Windows environment and current .env; live provider credentials/accounts, model availability or billing; deployment memory/handle limits; real network/proxy behavior; crash recovery; every console/tool/migration/test branch; full CodeQL, pip-audit, Semgrep, or MegaLinter finding logs. The unrelated migration file was not assessed as an active embedded/service path.

## 9. Fix order and release verification

1. Add timezone data and a clean Windows import/startup check.
2. Bound/isolate schema computation; close the streaming error-body limit gap.
3. Preserve shared exhaustion across reordered observations.
4. Wire the documented public opt-in and test through FAIR.solve(), preserving assurance states.
5. Propagate completion budgets to Ollama and half-open probe ownership.
6. Close ledger connections deterministically; fix default cache identity.
7. Run the new targeted regressions, then rerun the current project gates.

Suggested commands after fixes:

```text
python -m pip install -e ".[dev,service]"
python -m pip check
python -m compileall -q fair
ruff check fair tests
ruff format --check fair tests
mypy fair
pytest -ra --cov=fair --cov-report=term-missing --cov-fail-under=90
```

Also run the existing pip-audit, Semgrep, CodeQL, and MegaLinter workflows on the corrected commit. Execute Windows startup and the real delayed-socket Ollama check separately; a Linux mock-only suite cannot substitute for them.

**Post-fix acceptance:** Re-audit caller → request model → router → adapter/ledger → response for each modified path. Confirm contract compatibility, secret-safe errors, cancellation cleanup, trusted reset behavior, and no newly accepted invalid outputs. None of these proposed fixes has been applied or post-fix verified by this audit.

**Decision:** Continue controlled development, but fix the High findings and the shared-state/timeout defects before relying on one FAIR service as common infrastructure for CORP, YouTube Production, and other applications.
