# FAIR — Free AI Router

Quality-verified AI inference through free providers, available as an embeddable Python
module or an optional authenticated local HTTP service.
Every answer is judged before it's accepted — arithmetic checks, code validation,
JSON schema matching, citation verification, and cross-checking between independent models.

## Install

Embedded-only:

```bash
pip install -e .
```

Central HTTP service:

```bash
pip install -e ".[service]"
```

Requires Python 3.12+. The service extra adds FastAPI and Uvicorn; embedded FAIR remains
available without them.

## Windows test console

On Windows, double-click `START_FAIR.bat` from the repository root. The launcher
uses `.venv` when present, creates it when missing, installs FAIR plus the development
test tools if needed, and then opens an interactive menu:

1. Quick offline validation test
2. Test one selected live provider
3. Test all configured live providers one at a time
4. Test routing/failover with deterministic offline providers
5. Test a live cross-check using exactly two selected providers
6. Run the full pytest suite
7. Show provider configuration and FAIR eligibility
8. Exit

Live tests read the existing `.env` file. API keys and raw provider responses are never
printed by the console. A one-provider test constructs FAIR with only that selected
provider, so a Gemini test cannot silently route through Kilo or OpenRouter. Cross-check
tests similarly use only the two providers selected for that test.

OpenRouter Free and Kilo Free retain FAIR's runtime zero-cost checks. Recurring
free-plan providers use the persistent `FAIR_CONFIRMED_FREE_PROVIDERS` value from
`.env`; the console does not ask for repeated Y/N confirmations. A configured
recurring provider missing from that list is skipped/fail-closed and the sweep
continues automatically. Kilo tests also expose a
secret-safe billing diagnostic state such as `ZERO`, `CATALOG_ZERO_PRICE_FALLBACK`,
`COST_FIELD_MISSING`, `USAGE_MISSING`, or `NONZERO_OR_INVALID`. Kilo accepts the
fallback only when the exact response model is still a `:free` model in FAIR's fresh
live zero-priced catalog; any other missing/unknown cost state fails closed.

## Quick start

```python
from fair import FAIR

fair = FAIR(
    gemini_api_key="...",
    confirmed_free_providers={"google_gemini_api"},
)
result = await fair.solve(
    "What is 15 * 23?",
    validation={"kind": "arithmetic", "expression": "15*23"},
)
print(result.status)  # "ACCEPTED"
print(result.output)  # "345"
```

## Supported providers

| Provider | Env var | Access class | Models |
| ---------- | --------- | ------------- | -------- |
| Google Gemini | `GEMINI_API_KEY` | Free recurring | `gemini-3.5-flash-lite`, `gemini-3.6-flash` |
| Groq | `GROQ_API_KEY` | Free recurring | `openai/gpt-oss-20b`, `openai/gpt-oss-120b` |
| Mistral | `MISTRAL_API_KEY` (+ optional `MISTRAL_ADMIN_API_KEY`) | Free recurring | `ministral-8b-latest`, `ministral-3b-latest` |
| Cloudflare Workers AI | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | Workers Free only; Cloudflare hard-stops at 10k neurons/day | `llama-3.3-70b`, `gpt-oss-20b`, `llama-4-scout` |
| OpenRouter | `OPENROUTER_API_KEY` | Free dynamic (`:free`, $0 priced, `data_collection=deny`, 50 requests/day on a free account) | `nemotron-3-ultra-550b-a55b`†, `nex-n2.5-mini`, `north-mini-code`† |
| Kilo | `KILO_API_KEY` | Free dynamic (`:free`, live $0 pricing; trial/preview/known limited-time routes excluded) | `qwen3.8-27b`, `inkling-small`, `north-mini-code`, `lfm-2.5-2.6b` |
| Ollama (local) | `OLLAMA_HOST` or `OLLAMA_URL` | Free local | auto-discovered from the daemon |

† Nemotron 3 Ultra and North Mini Code do not accept `response_format`, so they are not
advertised as `structured_output` capable and are never selected for a task that needs a JSON schema.
Capabilities are declared per model, not per provider.

Pass API keys directly to the constructor, set env vars, or point at a dotenv file with
`FAIR(env_file=".env")` (the process environment wins over the file). At least one provider
is required.

### How many providers actually come up

A key alone does not register a recurring provider. Supplying keys for all six cloud
providers and nothing else gives you **two usable providers**:

```python
fair = FAIR(**all_six_cloud_keys)       # no confirmed_free_providers
len(fair.providers())                   # 2 -> openrouter_free, kilo_free
len(fair.skipped)                       # 4 -> recurring providers need confirmation

fair = FAIR(**all_six_cloud_keys, confirmed_free_providers={
    "google_gemini_api", "groq", "mistral", "cloudflare_workers_ai",
})
len(fair.providers())                   # 6, nothing skipped
```

Only OpenRouter Free and Kilo Free are auto-confirmed, because their adapters prove zero
cost at runtime. The other four cloud providers are recurring free-plan accounts whose API
key could belong to a billable account, so FAIR will not use them until the operator
explicitly confirms them. The Windows test console reads that persistent confirmation from
`FAIR_CONFIRMED_FREE_PROVIDERS` in `.env`.
Local Ollama registers only when a daemon is reachable or `ollama_models` is passed.

`fair.providers()` lists what registered; `fair.skipped` maps every configured-but-unused
provider to why. Read both before concluding a provider is broken — a provider whose key
is absent is skipped silently and appears in neither.

For Mistral, `MISTRAL_ADMIN_API_KEY` is optional and only available to accounts with
access to Mistral's Admin API (currently an Enterprise capability). Normal Free-mode users
should leave it blank. Mistral's inference API still reports ordinary rate limits. When an
eligible admin key is configured,
FAIR checks that status after a Mistral quota/rate-limit failure; a confirmed monthly limit
uses the Admin API billing-period `end_date` as the reset time. If that exact period end is
temporarily unavailable, FAIR waits six hours and checks again rather than guessing a month
boundary. Without the admin key, FAIR never guesses that a generic 429 is monthly exhaustion.

FAIR does not treat possession of an API key as proof that a recurring provider account is
still on a free tier. For providers such as Gemini, Groq, Mistral, and Cloudflare Workers AI,
explicitly attest the account is currently free-only with
`confirmed_free_providers={...}`. This is an operator assertion that the account/provider
configuration cannot auto-bill or otherwise incur paid API usage; do not set it merely
because the provider offers a free tier. For Cloudflare specifically, only attest
`cloudflare_workers_ai` when the account is on **Workers Free**. Cloudflare documents that
Workers Free stops further inference at the 10,000-neuron daily allocation (error 3036);
FAIR treats that error as quota exhaustion until 00:00 UTC. It does not infer neuron usage
from the OpenAI-compatible response because Cloudflare does not document a per-response
neuron field there. OpenRouter Free and Kilo Free are auto-confirmed
because their adapters enforce zero-priced `:free` models and reject non-zero observed cost at
runtime. Built-in cloud-provider reviews are date-pinned: the evidence expires 29 days
after its review date, and qualification separately refuses any review older than 30 days.
Restarting FAIR does not renew them, and post-dating the review date does not extend them
— a review dated in the future fails qualification outright. An expired review fails closed until the provider
definition is deliberately re-verified and updated. Providers whose key is present but
cannot be safely registered are listed in `fair.skipped` with the reason.

Hugging Face is intentionally not supported: its router reports a nonzero `estimated_cost`
on every call, which violates the free-only policy. Ollama Cloud is also excluded: its
current Free plan is a starter usage-credit pool and cloud models have published per-token
prices, so it does not satisfy FAIR's recurring-zero-cost requirement. NVIDIA's hosted NIM
preview API is also excluded because its hosted access is credit-based for new accounts.
Local Ollama remains fully supported.

## When a reservation is given back

A reservation buys one request from a provider. Where FAIR raises the failure
itself before anything is sent — its own budget and capability checks, or a model
that has left the live catalog — no request was made and the reservation is
refunded. Without that, a model delisted upstream spends a free request on every
solve that still carries it, which on a 50-a-day allowance is not a rounding error.

A provider that was called keeps the charge however badly it answered: a 403, a
413, a 5xx and a malformed body all mean a request was made. A cancelled solve keeps it
too, since cancellation can tear down a call already in flight. Over-counting
costs a free request; under-counting exceeds a free tier, which is the thing the
governor exists to prevent.

A half-open probe is always released, cancellation included. It is claimed to test
a provider, and an attempt that never called one tested nothing — left held, it
makes a healthy provider unavailable for the probe window and then a full fresh
cooldown after it.

## Limits counted per model

Gemini and Groq count their free limits per model, so a refusal on one model says
nothing about the next. FAIR benches that model alone and keeps routing to its
siblings. Before, any rate limit benched every model at the provider for the cooldown,
and one Gemini model reaching its daily cap took the other out until midnight Pacific.

A refusal is read that way only on the provider's own word:

- **Gemini** names the quota that was violated, and a free-tier one reads
  `...PerProjectPerModel-FreeTier`. Every violation in the refusal has to be per model.
- **Groq** names the model in the refusal itself: ``Rate limit reached for model
  `openai/gpt-oss-120b` ...``.

Anything else still benches the whole provider: a refusal that names no model or quota,
and any allowance that really is account-wide, such as Cloudflare's daily neurons or
OpenRouter's daily requests. An unknown scope gets the widest block.

A benched model returns after the cooldown, or the provider's own wait if that is
longer, or when its quota resets. A limit the provider names as per-minute is held
for less; see [Token limits](#token-limits). `fair.providers()` lists them under `benched_models`
with the time each returns, and a provider whose every model is benched reports
`THROTTLED`. In the attempt log a per-model refusal is recorded as `MODEL_RATE_LIMITED`
or `MODEL_QUOTA_EXHAUSTED`. Benches are local to the process, as throttles are: another
application sharing the account learns of the limit from its own first refusal.

Groq's request headers are per model too, so a successful answer whose headers report
no requests left benches that model until the reset they give, as its refusal would
have. Before, it marked all of Groq spent until then, up to a day.

FAIR's own request count is still per provider. Groq's `request_limit` of 1000 a day is
counted across both of its models, though Groq publishes that many for each, and Groq's
headers no longer feed that count or move its window: a figure for one model is not the
provider's. This over-counts, which is the safe direction. Each answer carries the
allowance its own response reported, so two models answering at once cannot swap
reports.

## Token limits

FAIR counts requests, not tokens. Groq and Gemini also meter tokens per minute, and
with large answers that is the limit reached first: one long answer spends most of a
model's per-minute allowance, and the next request is refused until some of it comes
back. FAIR keeps no token count of its own. It acts on what the refusal says.

**A per-minute limit is held for the minute, not the cooldown.** Both providers name
the window and state a wait:

- **Groq** in the text: ``Rate limit reached for model `...` ... on tokens per minute
  (TPM): ... Please try again in 2.5s.``
- **Gemini** in the quota id and its retry delay:
  `GenerateContentInputTokensPerModelPerMinute-FreeTier`, `"retryDelay": "8s"`.

When the window is named, the bench lasts the stated wait, or 60 seconds if none is
stated. Before, the cooldown was a floor under every wait, so a limit that cleared in
seconds idled the model for six minutes. A limit whose window is not named keeps the
cooldown, and a daily one is unchanged. The attempt log records these as
`MODEL_RATE_LIMITED_PER_MINUTE` or `RATE_LIMITED_PER_MINUTE`.

The stated wait is trusted once. If the next request to that model is refused the same
way with no answer in between, the wait did not hold: another application is filling the
window, or the request can never fit it. The second refusal in a row is held for the full
60 seconds and a third for the cooldown, so a model that keeps refusing costs three
requests in six minutes rather than one every few seconds. Any answer from the model
starts the count again, so a model working at its limit keeps the short waits. Refusals
of requests that were already in flight when the window filled arrive while the first
bench is still running; they extend it if they state a longer wait but are not counted.

**A request too large for a route is not an outage.** A single request that asks for
more than a model's whole per-minute allowance is refused outright, and no wait changes
that. Groq answers HTTP 413, ``Request too large for model ...``. FAIR used to count it
as a provider failure, so three in a minute opened the circuit and took the provider's
small requests with it. It is now a capability mismatch, `REQUEST_TOO_LARGE_FOR_ROUTE`:
the next route is tried, the provider's health is untouched, and neither attempt budget
is spent. The request was made, so its charge stands.

**A refused size is not sent twice.** The adapter remembers, per model and for an hour,
the sizes of requests the provider refused. A later request at least as large as one of
them in both prompt and output budget is stopped before dispatch,
`SIZE_ALREADY_REFUSED_BY_PROVIDER`, and its reservation is given back. Anything smaller
is sent as usual. Up to eight sizes are kept per model, because a long prompt and a large
answer are different shapes and neither covers the other. `safe_diagnostics()` lists
what is held under `refused_sizes`. The comparison uses FAIR's own byte-based estimate of
the prompt, which does not order every text the way a provider's tokenizer does, so a
dense prompt can occasionally be held back by the refusal of a looser one until the hour
is up.

FAIR does not track tokens used, predict a refusal, or reserve tokens for a request in
flight. That would need a tokens-per-minute figure per model, and none has been measured
against a live account. Until one is, the first refusal is how FAIR finds out.

## Shared quota across applications

Separate API keys are useful for isolation, but they do **not** necessarily create
separate free allowances. Gemini limits are project-scoped, Groq has organization-level
ceilings, and OpenRouter's free plan is account-scoped. FAIR can therefore share one
request ledger across independent applications while still keeping authentication,
security blocks, throttles and circuit-breaker state local to each application/key.

Point every FAIR-powered application at the same SQLite file and give each application
a stable name:

```python
shared_quota = r"C:\FAIR Shared State\quota.sqlite3"

corp = FAIR(
    application_id="corp",
    shared_quota_path=shared_quota,
    openrouter_api_key="...",
)

video = FAIR(
    application_id="youtube-production",
    shared_quota_path=shared_quota,
    openrouter_api_key="...",
)
```

With the default mapping, both instances above use the same `openrouter_free` quota
pool even if their API keys are different. If two applications genuinely use different
provider accounts/projects, assign different pool IDs:

```python
fair = FAIR(
    application_id="corp",
    shared_quota_path=shared_quota,
    quota_pool_ids={
        "openrouter_free": "openrouter-account-a",
        "groq": "groq-organization-a",
    },
)
```

Inspect the current ledger without exposing credentials:

```python
print(fair.quota_usage())
```

The report includes each pool's request count, remaining configured allowance, reset
timestamp, and usage grouped by `application_id`. OpenRouter Free is conservatively
configured at 50 requests/day, matching its current Free plan. The ledger is optional:
without `shared_quota_path`, FAIR retains its original in-process quota behavior.

Official quota references used for this design:
- OpenRouter pricing: <https://openrouter.ai/pricing/>
- Gemini rate limits: <https://ai.google.dev/gemini-api/docs/rate-limits>
- Groq rate limits: <https://console.groq.com/docs/rate-limits>

## Central FAIR service

The service is additive: existing Python applications can keep using `from fair import FAIR`
while applications are migrated one at a time to HTTP.

The service defaults to `127.0.0.1:8000`, requires a separate FAIR client bearer key for
every application, and keeps provider API keys inside the FAIR process. Client identity
cannot be supplied in request JSON; it is derived from the bearer key and becomes the
request `client_id`, including shared quota attribution.

Example `.env` configuration:

```text
OPENROUTER_API_KEY=...
GROQ_API_KEY=...

FAIR_APPLICATION_ID=fair-service
FAIR_SHARED_QUOTA_PATH=C:\FAIR Shared State\quota.sqlite3
FAIR_SERVICE_CLIENTS={"corp":"replace-with-random-key","youtube-production":"replace-with-another-random-key"}
FAIR_SERVICE_ADMIN_KEY=replace-with-separate-admin-key
FAIR_CONFIRMED_FREE_PROVIDERS=groq
```

Generate a client key with Python:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

On Windows, start the service with:

```powershell
.\START_FAIR_SERVICE.bat
```

or, after installing the service extra:

```powershell
fair-service
```

### Service endpoints

- `GET /health` — local health only; no credential required
- `GET /v1/models` — exposes the virtual OpenAI-style model `fair-router`
- `POST /v1/chat/completions` — non-streaming OpenAI-style compatibility endpoint
- `POST /v1/fair/solve` — native FAIR contract with validation, privacy and cross-check options
- `GET /v1/fair/providers` — provider status without secrets
- `GET /v1/fair/quota` — shared quota usage and per-application attribution
- `POST /v1/fair/admin/providers/{provider_id}/resume` — optional admin-key-only provider recovery

Example native request:

```powershell
$headers = @{ Authorization = "Bearer YOUR_CORP_FAIR_CLIENT_KEY" }
$body = @{
    task = "What is 15*23?"
    validation = @{
        kind = "arithmetic"
        expression = "15*23"
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/fair/solve" -Headers $headers -ContentType "application/json" -Body $body
```

The OpenAI-compatible endpoint does **not** weaken FAIR's acceptance rule. Free-form model
text that FAIR cannot verify returns HTTP 422 rather than being passed through as if it were
trusted. Structured `response_format` requests can use FAIR's JSON Schema validation.
Streaming is intentionally not supported in this first service version.

One service process uses one centralized provider credential set. Application isolation is
handled by FAIR client keys, so provider credentials no longer need to be copied into every
application. Keep the service at one worker for now: quota is cross-process safe through
SQLite, but performance history, cache and circuit-breaker state are intentionally centralized
in the single service process rather than distributed across multiple Uvicorn workers.
## Validation contracts

FAIR verifies AI responses before accepting them:

- **`arithmetic`** — exact rational arithmetic via bounded AST evaluation
- **`python_function`** — safe AST interpreter runs test cases against generated code (no eval/exec)
- **`reference_json`** — exact JSON match against a known reference
- **`grounded_json`** — extracts values from supplied source data via JSON pointers
- **`grounded_claims`** — structured fact-checking across supplied evidence sources

Passing `expected_schema` with no contract checks JSON Schema conformance of the
output. A single markdown code fence around the whole answer is tolerated (free models
add one even when told not to); prose around the JSON is still a schema failure. That proves shape, not truth, so it scores 85: accepted at `commodity` and
`standard`, escalated at `advanced` and `high_impact_support`. Tasks the profiler
flags as needing code or grounding still require a matching contract.

### Work nothing can verify

Every contract below settles an answer mechanically. Open-ended work — analysis, a
plan, a design rationale, long-form reasoning — has no such contract, so
`acceptable()` never passes it and the request can only escalate with
`QUALITY_VERIFICATION_UNAVAILABLE`, whichever model answered.

`accept_unverified=True` changes that, and nothing else:

```python
result = await fair.solve("Explain the trade-offs between X and Y", accept_unverified=True)
result.status              # "ACCEPTED_UNVERIFIED", never "ACCEPTED"
result.verification_state  # "UNVERIFIED"
result.best_quality_score  # None
```

FAIR's guarantee is that it never presents unverified text as verified. That is not
the same as never returning it, and the two had been conflated. The answer comes
back under its own status, with no score, and the attempt log still records the
disposition as `UNVERIFIED`: what FAIR knows about an answer does not change because
of what the caller is willing to take. A caller that tests `status == "ACCEPTED"`
keeps refusing it without changing a line.

Unverified means nothing could prove the answer right, not that nothing checked it.
Everything that finds an answer actually **wrong** still refuses: an empty or
truncated response, a schema, arithmetic or claim mismatch, a fabricated or
unsupported citation, a self-contradiction, grounding the task required and did not
get, and a source policy that was blocked or could not run.

It cannot be combined with `cross_check_required` or `high_impact_support`. Both ask
for independent corroboration, which is precisely what an unverified answer lacks.
Unverified answers are never cached, since the cache only keeps what a deterministic
contract settled.

### Schemas and provider dialects

The schema you pass is full JSON Schema 2020-12 and every response is validated
against it locally. Provider structured-output APIs accept much less: OpenAI strict
mode (Groq, OpenRouter, Mistral, Kilo, Cloudflare) rejects `minLength`, `minItems`,
`maxItems`, `minimum`, `maximum`, `pattern`, `format` and `const`, and requires
`additionalProperties: false` plus every property listed in `required`; Gemini
rejects a different set including `additionalProperties`.

FAIR therefore sends each provider only the shape of your schema — types,
properties, required, items, enums — and restates the dropped constraints as prompt
text ("1 to 5 items", "must not be empty"). Nothing is weakened: the full schema
still judges the answer, and a response that breaks a dropped constraint is still a
schema failure. `const` is rewritten as a single-value `enum`, which every dialect
accepts.

Two problems cannot be fixed this way, because repairing them would change what a
schema means, and providers reject both with an opaque HTTP 400: an object without
`additionalProperties: false`, or a `required` list that omits a declared property;
and the `oneOf` / `allOf` / `not` combinators. Check a schema before wiring it in:

```bash
python -m fair.tools.schema_compat request.json
python -m fair.tools.schema_compat request.json --key expected_schema
python -m fair.tools.schema_compat request.json --emit openai_strict
```

It prints, per dialect, what must be fixed, what will be dropped, and the prompt
text that replaces it. It exits non-zero when a schema needs an author fix, so it
works as a CI check.

### Diagnosing a provider rejection

A non-200 from a provider is reported as its status code alone — `HTTP_400` —
because an upstream error body is provider text, not FAIR's own observation. That
makes an unsupported-schema rejection indistinguishable from a context-length one.
Set `provider_error_diagnostics=True` (or `FAIR_PROVIDER_ERROR_DIAGNOSTICS=1`) and
the last non-200 message per adapter is kept — bounded to 512 characters, with any
credential redacted — and readable through `safe_diagnostics()`:

```python
fair = FAIR(..., provider_error_diagnostics=True)
result = await fair.solve(...)
fair._registry.adapters["groq"].safe_diagnostics()
# {"last_provider_error": {"status": "HTTP_400",
#                          "provider_message": "... 'minLength' is unsupported ..."}}
```

Raised codes, reason codes and the attempt log are identical either way; the flag
only adds the record. It is off by default. The record is cleared as each request
starts and names the path it came from, so it always describes the call in front of
you rather than an earlier one that failed.

`safe_diagnostics()` also reports, without any flag, why a reviewed model was left
out of the live catalog. `REVIEWED_MODEL_UNAVAILABLE_OR_PRICING_CHANGED` covers five
different causes and the router can only report the last of them:

```python
fair._registry.adapters["openrouter_free"].safe_diagnostics()["catalog_drops"]
# {"nex-agi/nex-n2.5-mini:free": "CATALOG_CONTEXT_BELOW_REVIEWED_262144"}
```

`ABSENT_FROM_CATALOG`, `AMBIGUOUS_IN_CATALOG`, `INACTIVE_IN_CATALOG`,
`NOT_ZERO_PRICED`, `CONTEXT_NOT_REPORTED` and `CATALOG_CONTEXT_BELOW_REVIEWED_<n>`
are the reasons; the last names the reviewed value the catalog now contradicts.

A catalog fetch that fails is remembered for as long as a successful one stays
fresh. Only success used to be cached, so every eligible model asked again and one
unreachable endpoint cost a read timeout per model rather than per solve.

### Attempt budgets and streaming

A per-attempt timeout is a bet that a completion of any size arrives inside it.
FAIR enforced two such bets — the router waited `timeout_seconds` around the call
and the adapter allowed 25 seconds for one read — so a large request was cancelled
twice over before a model had finished writing. Both now scale with the requested
output:

```
attempt budget = timeout_seconds + max_output_tokens / output_tokens_per_second
                 bounded by max_timeout_seconds
```

At the defaults that is 41s for 1024 tokens and 117s for 4096, against a flat 15s
before. `output_tokens_per_second` decides only how long FAIR waits before calling
an attempt failed. The default of 40 comes from probing eight routes on 2026-09-30:
five finished on their own at 73.6 to 324.3 tokens/second, and the three that were
truncated had spent their whole budget getting there, which puts them above 52.6,
64.3 and 91.6. Re-probe and raise it if your routes are faster.

A generous budget is only safe if a provider that has stopped responding is still
noticed quickly, so completions are streamed. The read timeout then applies to each
chunk rather than to the whole answer: arriving tokens keep resetting it, and a
stalled generation trips it in `read_timeout_seconds` however large the budget is.

Streaming changes how the bytes arrive, never what has to be proved about them. The
stream is assembled into the same envelope a buffered response returns, and every
check — model identity, finish reason, tool-call refusal, and the zero-cost
observation above all — then runs on the shape it has always run on. A stream
carrying no usage is refused exactly as a buffered response carrying none is.

Every adapter streams. `openrouter_free` and `kilo_free` were buffered until a
live probe on 2026-09-30 showed a streamed completion still carrying the zero-cost
observation they fail closed without. A route that did stop reporting a cost would
still fail closed rather than bill, and Kilo's catalog fallback covers a response
that omits the field on a stream exactly as when buffered.

### Measuring what a provider actually accepts

Several values here can only be known by asking: the output ceiling a free endpoint
enforces, whether a schema survives its structured-output parser, whether a cost
observation arrives on a stream, how fast a route really is. The probe asks, using
the registered adapters so every admission, zero-cost and credential check applies
exactly as it does in routing. It never calls `solve()` — the quality gate would
spend extra requests and confound the measurement.

Requests are the scarce resource (OpenRouter Free allows 50 a day), so the catalog
pass costs none, every other pass costs one request per model, and the run stops at
`--max-requests` whatever is left. Always start with a dry run:

```bash
python -m fair.tools.probe --dry-run --all          # what it would send, and how many
python -m fair.tools.probe                          # catalog only, zero completions
python -m fair.tools.probe --limits --output-tokens 8192
python -m fair.tools.probe --schema request.json --streaming
python -m fair.tools.probe --providers groq --models openai/gpt-oss-120b --limits
```

Whether an endpoint accepts a budget is answered by the request not being refused,
so the limits pass asks for a one-word answer with a large declared ceiling rather
than asking a model to fill it — filling 16384 tokens takes as long as 16384 tokens
take. Throughput is measured separately, on a short generative answer. Every request
is bounded by `--max-seconds` (90 by default), because routing budgets scale into
minutes for a large answer and a probe only needs to know the request was taken.
Progress is printed per request; pass `--quiet` to suppress it.

It writes a JSON report and prints what a reviewer could defend putting in a
descriptor:

```json
{
  "output_tokens": {
    "groq/openai/gpt-oss-120b": {"accepted_at_least": 8192, "descriptor_says": 4096},
    "mistral/ministral-8b-latest": {"refused_at": 8192, "refusal": "OUTPUT_BUDGET_INVALID"}
  },
  "suggested_output_tokens_per_second": 34,
  "cost_observed_on_stream": {"openrouter_free/...": true}
}
```

An accepted budget is a **floor**, not a ceiling: it says the endpoint took that
many tokens, not that it would refuse one more. A refusal is the upper bound. Both
are named for what they are, because reporting either as "the limit" is the
overclaim that put an unconfirmed 262144 in the registry to begin with.

A throughput figure comes only from an answer that finished on its own. A truncated
one (`finish_reason` of `length`) is reported separately as a lower bound, because
the budget bounded it and a model that reasons before answering spends much of that
budget on thought parts the adapter strips out of the text — `gemini-3.6-flash`
returned 22 visible tokens against a 512-token budget, which is thinking, not a slow
provider. Sizing a timeout from a figure like that would give a 1024-token attempt
several minutes.

The probe turns provider diagnostics on for its own run, so a refusal shows the
provider's message rather than a bare `HTTP_400`. `cost_observed_on_stream` answers
the one question holding `openrouter_free` and `kilo_free` on the buffered path: it
sets `supports_streaming` on the adapter instance for a single request, restores it
afterwards, and reports whether the zero-cost proof still arrived.

### Measured output limits

Probed against the live endpoints on 2026-09-30:

| Route | Accepted | Note |
|---|---|---|
| `openai/gpt-oss-20b`, `openai/gpt-oss-120b` (Groq) | 32768 | floor; >250 tok/s |
| `ministral-8b-latest`, `ministral-3b-latest` | 32768 | floor |
| `@cf/openai/gpt-oss-20b` | 32768 | floor; not refused below it |
| `@cf/meta/llama-4-scout-17b-16e-instruct` | 32768 | floor |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | 16384 | 32768 exceeds its 24000 context |
| `gemini-3.5-flash-lite`, `gemini-3.6-flash` | 32768 | checked against Google's published `outputTokenLimit` |

`kilo_free` and `openrouter_free` keep the conservative 4096 default: Kilo's models
were rate-limited and OpenRouter refused on its data policy, so neither endpoint
actually answered. `MAX_OUTPUT_TOKENS_CEILING` bounds what any reviewed descriptor may reach;
raising it alone changes nothing, because a descriptor declaring less still governs.

### Asking for headroom without losing routes

`max_output_tokens` is the most any route will be asked for. `min_output_tokens` is
the least the answer can be complete within. They are different questions and used to
share one dial, so a request for 32768 dropped every route whose own ceiling was lower
— refused for not reaching a budget the task never needed.

A route is offered when its ceiling reaches the **floor**, and is then asked for
`min(max_output_tokens, its own ceiling)`. For the concept request that needed about
3300 tokens:

| request | routes offered | sent |
|---|---|---|
| `max_output_tokens=4096` | 10 | 4096 to every route, truncating the large ones |
| `max_output_tokens=32768` | 8 | 32768; the 16384 and 4096 routes are dropped |
| `max_output_tokens=32768, min_output_tokens=4096` | 10 | 32768, 16384 or 4096 per route |

Leaving `min_output_tokens` unset makes the floor equal the whole budget, which is
what every route had to meet before the field existed — so callers that say nothing
keep exactly the behaviour they had.

### Published output limits

Where a provider publishes its own completion-token ceiling in the live catalog,
that ceiling lowers the reviewed descriptor — it never raises it. A live catalog is
current where a reviewed value is a claim from the day it was written, so a smaller
published limit wins; raising a limit on unreviewed data is the guess FAIR refuses
to make. A request above the published limit is refused before dispatch instead of
spending quota on an HTTP 400. A provider that publishes nothing usable changes
nothing, and only `openrouter_free` reads a field today.

```python
# Code validation
result = await fair.solve(
    "Write a function that adds two numbers",
    validation={
        "kind": "python_function",
        "function_name": "add",
        "cases": [
            {"arguments": [1, 2], "expected": 3},
            {"arguments": [0, 0], "expected": 0},
        ],
    },
)

# JSON reference
result = await fair.solve(
    "Return the config as JSON",
    validation={"kind": "reference_json", "expected": {"debug": False, "port": 8080}},
)
```

## Data privacy classes

`privacy_class` bounds which providers may see a task. A provider is eligible only when
its `max_data_class` is at least as permissive as the request, and every remote adapter
is PUBLIC-only by construction, so anything above `PUBLIC` routes to local Ollama or does
not route at all. An unroutable classification escalates; it is never downgraded to a
cloud provider.

```python
result = await fair.solve(
    "Summarize this internal incident report: ...",
    privacy_class="CONFIDENTIAL",
)
# With no local provider configured this returns ESCALATION_REQUIRED and no
# cloud provider is contacted.
```

Discovered local Ollama models are registered with `max_data_class="RESTRICTED"`, so a
local daemon is what makes `INTERNAL`, `CONFIDENTIAL` and `RESTRICTED` traffic routable.

## Source review policies

`source_policy` constrains which supplied evidence a grounded contract may rely on: how
recently it was observed, which source classes are allowed, and how many independent
origins must corroborate each value. Policies are evaluated against operator-supplied
reviews, so a policy without a matching review blocks the answer rather than passing it.

```python
fair = FAIR(ollama_url="http://127.0.0.1:11434", source_reviews="reviews.yaml")

result = await fair.solve(
    "Extract the reported revenue",
    validation={"kind": "grounded_json", "fields": [...]},
    evidence=[{"source_id": "s1", "review_id": "r1", "text": "..."}],
    source_policy={"min_independent_origins": 2, "max_age_seconds": 86400},
)
result.source_policy.state  # PASSED, BLOCKED, SERVICE_FAILED or NOT_REQUESTED
```

Reviews are snapshots an operator has checked; the policy does not establish real-world
truth. Without `source_reviews`, any request carrying a `source_policy` is reported
`BLOCKED` with `SOURCE_REVIEW_REQUIRED`.

## Cross-checking

Two gateways serving one model give it two ids, and comparing ids alone would call
that model its own independent verifier — `openai/gpt-oss-20b` on Groq and
`@cf/openai/gpt-oss-20b` on Cloudflare are the same weights. Descriptors that name
the same model share an `independence_group`, so a cross-check cannot be satisfied
by asking it twice. Two sizes from one lab, or two generations of one family, may
share failure modes as well, but that is a judgement about models rather than a fact
about names, and it is left to whoever reviews the registry.

Request a second independent model to verify the answer:

```python
result = await fair.solve(
    "What is 15 * 23?",
    validation={"kind": "arithmetic", "expression": "15*23"},
    cross_check_required=True,
)
# Cross-check agreement is assessed only for supported deterministic or
# structured validation contracts; free-form text is not treated as verified
# merely because two models produce similar answers.
```

## Response statuses

- **`ACCEPTED`** — answer passed all validation checks
- **`ACCEPTED_UNVERIFIED`** — an answer nothing could verify, returned because the
  request asked for it (see below). Deliberately not `ACCEPTED`
- **`ESCALATION_REQUIRED`** — no model produced a verified answer
- **`FAILED`** — infrastructure failure (validator error, all providers down)

An answer is accepted only when it is both scored at or above the level's threshold **and**
carries a verification state from a deterministic contract. A high score alone is never
enough: an unverified answer escalates.

A `BillingViolation` is the one outcome that does not come back as a status. It stops the
router (`fair.stopped = True`), blocks the provider, and raises out of `solve()`, so a
caller that must survive it has to catch it:

```python
from fair.providers.base import BillingViolation

try:
    result = await fair.solve("...")
except BillingViolation:
    # A provider reported a non-zero or unreadable cost. Dispatch has stopped.
    ...
```

## How many models get tried

Two independent budgets bound a single `solve()`, and the distinction matters:

- **`max_attempts`** (default 3) counts *answered* attempts — ones the quality gate
  actually judged, whether it accepted or rejected them.
- **`max_unanswered_attempts`** (default 6) separately bounds models that never answered:
  down, throttled, timed out, quota-exhausted.

A provider that timed out said nothing about the task, so it does not consume the answer
budget. Counting them together meant a few outages ended the search while healthy models
sat untried, reported as though every model had been asked. Every (provider, model) pair is
tried at most once per solve either way.

## Constructor options

```python
FAIR(
    gemini_api_key="...",         # or env: GEMINI_API_KEY
    groq_api_key="...",           # or env: GROQ_API_KEY
    openrouter_api_key="...",     # or env: OPENROUTER_API_KEY
    mistral_api_key="...",        # or env: MISTRAL_API_KEY
    kilo_api_key="...",           # or env: KILO_API_KEY
    cloudflare_api_token="...",   # or env: CLOUDFLARE_API_TOKEN
    cloudflare_account_id="...",  # or env: CLOUDFLARE_ACCOUNT_ID
    confirmed_free_providers={     # explicit account-tier confirmation where required
        "google_gemini_api",
        "groq",
    },
    ollama_url="...",             # or env: OLLAMA_HOST / OLLAMA_URL (localhost is fine)
    ollama_models=["llama3.2:3b"],# skip daemon discovery and use these local models
    env_file=".env",              # optional dotenv file; process env takes precedence
    providers=[(spec, adapter)],  # custom providers (e.g. MockAdapter for testing)
    quality_level="standard",     # commodity|standard|advanced|high_impact_support
    max_attempts=3,               # answered attempts (judged by the quality gate) before escalating
    max_unanswered_attempts=6,    # models that never answered (down/slow/throttled) tolerated per solve
    timeout_seconds=15,           # base per-attempt budget, before the output allowance
    output_tokens_per_second=30,  # assumed free-tier throughput, sizing that budget
    max_timeout_seconds=600,      # hard ceiling on any one attempt
    cooldown_seconds=360,         # sit-out after circuit-break/throttle, for a provider or one model
    cache_enabled=True,           # in-memory LRU cache for deterministic tasks
    cross_check_required=False,   # require independent verification
    source_reviews="reviews.yaml", # operator-reviewed evidence snapshots (path or list)
    application_id="corp",        # stable app identity for shared quota attribution
    shared_quota_path=r"C:\FAIR Shared State\quota.sqlite3", # optional shared SQLite ledger
    quota_pool_ids={              # optional account/project identity overrides
        "openrouter_free": "openrouter-main",
    },
    provider_error_diagnostics=False, # or env: FAIR_PROVIDER_ERROR_DIAGNOSTICS
    on_event=callback,            # optional (event_type, payload) callback
)
```

### solve() options

```python
await fair.solve(
    task,
    task_type=None,               # override keyword-based task inference
    quality_level=None,           # per-request override of the constructor default
    privacy_class="PUBLIC",       # PUBLIC|INTERNAL|CONFIDENTIAL|RESTRICTED
    required_capabilities=None,   # additional model capabilities the task needs
    freshness_required=False,     # task needs current information
    expected_schema=None,         # JSON Schema the answer must conform to
    validation=None,              # a validation contract (see above)
    evidence=None,                # source documents for grounded contracts
    source_policy=None,           # constraints on which evidence may be relied on
    cross_check_required=None,    # per-request override
    max_output_tokens=1024,       # headroom wanted: the most any route is asked for
    min_output_tokens=None,       # floor: the least the answer can be complete within
    client_id="embedded",
    priority="P2",
    cache_mode="default",         # default|bypass|refresh
)
```

## Event callback

Monitor routing decisions without a database:

```python
def on_event(event_type, payload):
    print(f"{event_type}: {payload}")

fair = FAIR(
    gemini_api_key="...",
    confirmed_free_providers={"google_gemini_api"},
    on_event=on_event,
)
```

Events: `PROFILED`, `EXECUTING`, `ATTEMPT_COMPLETED`, `CROSS_CHECK_COMPLETED`, `ACCEPTED`, `ACCEPTED_UNVERIFIED`, `ESCALATION_REQUIRED`, `FAILED`.

## Architecture

FAIR is a router with a verifier on its accept path. Nothing is returned because a model
produced it — it is returned because a deterministic check passed. Two independent gates
run around every call, and both fail closed:

- **Admission** decides a provider is free (`fair/governor/policy.py`, `qualification.py`).
  Zero cost, no paid subscription, no credit purchase, no auto-billing, and a per-model
  qualification with a review date inside its window. Re-checked at registration, at
  selection, and again on a cache hit.
- **Quality** decides an answer is verified (`fair/quality/engine.py`). A contract runs
  against the response; both the score and the verification state must pass.

All validation logic remains pure functions. FAIR needs no server or database for routing, but an optional SQLite quota ledger can coordinate account-level free limits across multiple application processes.

```text
FAIR(api_keys)                     admission gate -> fair.providers() / fair.skipped
 ├─ embedded Python callers
 ├─ optional authenticated HTTP service
 └─ EmbeddedRouter
     └─ solve(request)
         1. profile_task          required capabilities, task class, context, threshold
         2. MemoryCache.get       arithmetic/reference only; re-validates before returning
         3. MemorySelector        admission + quota + privacy_class + capabilities + window,
                                  then quality x 0.65 + quota x 0.20 + reliability x 0.15
         4. adapter.complete      via CredentialedAdapter (credential-reflection guard)
         5. Quality Engine        arithmetic, bounded code interpreter, JSON, grounding,
                                  claims, source policy, optional independent cross-check
         6. accept or retry       two budgets: answered vs unanswered (see above)

     MemoryQuotaGovernor   local key health + circuit breaker; optional shared request ledger
     SharedQuotaLedger     SQLite atomic quota pools + per-application attribution
     MemoryPerformance     per (provider, model, task class) quality/reliability + drift
```

A cache hit is not trusted on its own: `MemoryCache.get` re-runs the validator against the
stored text and re-checks provider admission, and drops the entry if either now fails.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## Safety

- Paid routes are prohibited — recurring/free-plan accounts require explicit free-tier confirmation, while dynamic free gateways must prove zero-priced models/cost at runtime
- A `BillingViolation` from any provider stops the entire system immediately
- Provider credentials are never leaked in responses (`CredentialedAdapter`)
- The code validator uses a safe AST interpreter with bounded steps (4096) and iterations (1024) — no `eval`/`exec`
