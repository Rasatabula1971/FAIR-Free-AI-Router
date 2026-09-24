# FAIR — Free AI Router

Embeddable Python module for quality-verified AI inference through free providers.
Every answer is judged before it's accepted — arithmetic checks, code validation,
JSON schema matching, citation verification, and cross-checking between independent models.

## Install

```
pip install -e .
```

Requires Python 3.12+. Dependencies: `pydantic`, `httpx`, `jsonschema`.

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
|----------|---------|-------------|--------|
| Google Gemini | `GEMINI_API_KEY` | Free recurring | `gemini-3.5-flash-lite`, `gemini-3.6-flash` |
| Groq | `GROQ_API_KEY` | Free recurring | `openai/gpt-oss-20b`, `openai/gpt-oss-120b` |
| Mistral | `MISTRAL_API_KEY` | Free recurring | `ministral-8b-latest`, `ministral-3b-latest` |
| Cloudflare Workers AI | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | Free recurring (10k neurons/day, metered) | `llama-3.3-70b`, `gpt-oss-20b`, `llama-4-scout` |
| NVIDIA NIM | `NVIDIA_API_KEY` | Free recurring | `meta/llama-3.3-70b-instruct`, `meta/llama-3.1-8b-instruct` |
| Ollama Cloud | `OLLAMA_CLOUD_API_KEY` | Free recurring | `gpt-oss:20b` |
| OpenRouter | `OPENROUTER_API_KEY` | Free dynamic (`:free`, $0 priced, no data collection) | `gemma-4-26b`, `ling-3.0-flash-sante`, `north-mini-code`, `dots-3-note` |
| Kilo | `KILO_API_KEY` | Free dynamic (`:free`, $0 priced) | `nemotron-3-super-120b`, `nex-n2.5-mini`, `laguna-s-2.1` |
| Z.ai | `ZAI_API_KEY` | Free dynamic (flash models) | `glm-4.5-flash`, `glm-4.7-flash` |
| Ollama (local) | `OLLAMA_HOST` or `OLLAMA_URL` | Free local | auto-discovered from the daemon |

Pass API keys directly to the constructor, set env vars, or point at a dotenv file with
`FAIR(env_file=".env")` (the process environment wins over the file). At least one provider
is required.

FAIR does not treat possession of an API key as proof that a recurring provider account is
still on a free tier. For providers such as Gemini, Groq, Mistral, NVIDIA NIM, Ollama Cloud,
Z.ai, and Cloudflare Workers AI, explicitly attest the account is currently free-only with
`confirmed_free_providers={...}`. This is an operator assertion that the account/provider
configuration cannot auto-bill or otherwise incur paid API usage; do not set it merely
because the provider offers a free tier. OpenRouter Free and Kilo Free are auto-confirmed because
their adapters enforce zero-priced `:free` models and reject non-zero observed cost at
runtime. Providers whose key is present but cannot be safely registered are listed in
`fair.skipped` with the reason.

Hugging Face is intentionally not supported: its router reports a nonzero `estimated_cost`
on every call, which violates the free-only policy.

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

## Cross-checking

Request a second independent model to verify the answer:

```python
result = await fair.solve(
    "What is the capital of France?",
    cross_check_required=True,
)
# result.cross_check.state: "PASSED", "DISAGREEMENT", etc.
```

## Response statuses

- **`ACCEPTED`** — answer passed all validation checks
- **`ESCALATION_REQUIRED`** — no model produced a verified answer
- **`FAILED`** — infrastructure failure (validator error, all providers down)

## Constructor options

```python
FAIR(
    gemini_api_key="...",         # or env: GEMINI_API_KEY
    groq_api_key="...",           # or env: GROQ_API_KEY
    openrouter_api_key="...",     # or env: OPENROUTER_API_KEY
    mistral_api_key="...",        # or env: MISTRAL_API_KEY
    kilo_api_key="...",           # or env: KILO_API_KEY
    zai_api_key="...",            # or env: ZAI_API_KEY
    nvidia_api_key="...",         # or env: NVIDIA_API_KEY
    ollama_cloud_api_key="...",   # or env: OLLAMA_CLOUD_API_KEY
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
    on_event=callback,            # optional (event_type, payload) callback
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

All quality validation logic runs as pure functions — no database, no server, no YAML config.

```
FAIR(api_keys)
 └─ EmbeddedRouter
     ├─ MemorySelector      (weighted scoring: quality × 0.65 + quota × 0.20 + reliability × 0.15)
     ├─ MemoryQuotaGovernor  (circuit breaker: CLOSED → OPEN → HALF_OPEN)
     ├─ MemoryPerformance    (quality/reliability tracking from observed attempts)
     ├─ MemoryCache          (LRU with TTL for arithmetic/reference tasks)
     └─ Quality Engine       (arithmetic, code validator, JSON, consensus, grounding)
```

## Development

```
pip install -e ".[dev]"
pytest -q
```

## Safety

- Paid routes are prohibited — recurring/free-plan accounts require explicit free-tier confirmation, while dynamic free gateways must prove zero-priced models/cost at runtime
- A `BillingViolation` from any provider stops the entire system immediately
- Provider credentials are never leaked in responses (`CredentialedAdapter`)
- The code validator uses a safe AST interpreter with bounded steps (4096) and iterations (1024) — no `eval`/`exec`
