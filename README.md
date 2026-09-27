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
8. Clear this-session free-account confirmations
9. Exit

Live tests read the existing `.env` file. API keys and raw provider responses are never
printed by the console. A one-provider test constructs FAIR with only that selected
provider, so a Gemini test cannot silently route through Kilo or OpenRouter. Cross-check
tests similarly use only the two providers selected for that test.

OpenRouter Free and Kilo Free retain FAIR's runtime zero-cost checks. Providers whose
API keys may belong to paid/billable accounts still require an explicit free-only
account confirmation for the current console session. Kilo tests also expose a
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
| Kilo | `KILO_API_KEY` | Free dynamic (`:free`, $0 priced) | `nemotron-3-super-120b`, `nex-n2.5-pro`, `laguna-s-2.1` |
| Ollama (local) | `OLLAMA_HOST` or `OLLAMA_URL` | Free local | auto-discovered from the daemon |

† Nemotron 3 Ultra and North Mini Code do not accept `response_format`, so they are not
advertised as `structured_output` capable and are never selected for a task that needs a JSON schema.
Capabilities are declared per model, not per provider.

Pass API keys directly to the constructor, set env vars, or point at a dotenv file with
`FAIR(env_file=".env")` (the process environment wins over the file). At least one provider
is required.

### How many providers actually come up

A key alone does not register a provider. Supplying keys for all seven and nothing else
gives you **two usable providers**, not seven:

```python
fair = FAIR(**all_seven_keys)          # no confirmed_free_providers
len(fair.providers())                  # 2  -> openrouter_free, kilo_free
len(fair.skipped)                      # 5  -> each with the reason

fair = FAIR(**all_seven_keys, confirmed_free_providers={
    "google_gemini_api", "groq", "mistral", "cloudflare_workers_ai",
})
len(fair.providers())                  # 7, nothing skipped
```

Only OpenRouter Free and Kilo Free are auto-confirmed, because their adapters prove zero
cost on every request. The other five are recurring free-plan accounts whose API key could
belong to a billable account, so FAIR will not use them until you assert otherwise.
Local Ollama registers only when a daemon is reachable or `ollama_models` is passed.

`fair.providers()` lists what registered; `fair.skipped` maps every configured-but-unused
provider to why. Read both before concluding a provider is broken — a provider whose key
is absent is skipped silently and appears in neither.

For Mistral, `MISTRAL_ADMIN_API_KEY` is optional but recommended. Mistral's normal
inference API reports ordinary rate limits, while its Admin API exposes whether the
Organization's monthly completion limit has been reached. When the admin key is configured,
FAIR checks that status after a Mistral quota/rate-limit failure; a confirmed monthly limit
uses the Admin API billing-period `end_date` as the reset time. If that exact period end is
temporarily unavailable, FAIR waits six hours and checks again rather than guessing a month
boundary. Without the admin key, FAIR never guesses that a generic 429 is monthly exhaustion.

For Z.ai, FAIR only admits the text models Z.ai currently lists at zero price:
`glm-4.7-flash` and `glm-4.5-flash`. A model ending in `-flash` is not automatically
free; for example, newer Flash models may be paid. Z.ai publishes account/model-specific
rate limits in its console rather than one universal reset window. FAIR therefore treats
Z.ai rate-limit code 1302 as temporary, honors `Retry-After` when supplied, and otherwise
uses its normal cooldown before rechecking. Z.ai overload code 1305 is treated as provider
availability, not quota. If a zero-priced model returns insufficient-balance code 1113,
FAIR blocks Z.ai rather than risking paid fallback.

FAIR does not treat possession of an API key as proof that a recurring provider account is
still on a free tier. For providers such as Gemini, Groq, Mistral, Z.ai, and Cloudflare
Workers AI, explicitly attest the account is currently free-only with
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
    timeout_seconds=15,           # per-attempt timeout
    cooldown_seconds=360,         # provider sit-out after circuit-break/throttle (Groq's window)
    cache_enabled=True,           # in-memory LRU cache for deterministic tasks
    cross_check_required=False,   # require independent verification
    source_reviews="reviews.yaml", # operator-reviewed evidence snapshots (path or list)
    application_id="corp",        # stable app identity for shared quota attribution
    shared_quota_path=r"C:\FAIR Shared State\quota.sqlite3", # optional shared SQLite ledger
    quota_pool_ids={              # optional account/project identity overrides
        "openrouter_free": "openrouter-main",
    },
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
    max_output_tokens=1024,
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

Events: `PROFILED`, `EXECUTING`, `ATTEMPT_COMPLETED`, `CROSS_CHECK_COMPLETED`, `ACCEPTED`, `ESCALATION_REQUIRED`, `FAILED`.

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
