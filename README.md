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

fair = FAIR(gemini_api_key="...")
result = await fair.solve(
    "What is 15 * 23?",
    validation={"kind": "arithmetic", "expression": "15*23"},
)
print(result.status)  # "ACCEPTED"
print(result.output)  # "345"
```

## Supported providers

| Provider | Env var | Access class |
|----------|---------|-------------|
| Google Gemini | `GEMINI_API_KEY` | Free recurring |
| Groq | `GROQ_API_KEY` | Free recurring |
| OpenRouter | `OPENROUTER_API_KEY` | Free dynamic |
| Ollama | `OLLAMA_URL` + `OLLAMA_ENABLED=1` | Free local |

Pass API keys directly to the constructor or set env vars. At least one provider is required.

## Validation contracts

FAIR verifies AI responses before accepting them:

- **`arithmetic`** — exact rational arithmetic via bounded AST evaluation
- **`python_function`** — safe AST interpreter runs test cases against generated code (no eval/exec)
- **`reference_json`** — exact JSON match against a known reference
- **`grounded_json`** — extracts values from supplied source data via JSON pointers
- **`grounded_claims`** — structured fact-checking across supplied evidence sources

Passing `expected_schema` with no contract checks JSON Schema conformance of the
output. That proves shape, not truth, so it scores 85: accepted at `commodity` and
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
    ollama_url="...",             # or env: OLLAMA_URL (+ OLLAMA_ENABLED=1)
    providers=[(spec, adapter)],  # custom providers (e.g. MockAdapter for testing)
    quality_level="standard",     # commodity|standard|advanced|high_impact_support
    max_attempts=3,               # retry budget across providers
    timeout_seconds=15,           # per-attempt timeout
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

fair = FAIR(gemini_api_key="...", on_event=on_event)
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

- Paid routes are prohibited — the admission policy enforces $0 cost, no billing, no paid subscriptions
- A `BillingViolation` from any provider stops the entire system immediately
- Provider credentials are never leaked in responses (`CredentialedAdapter`)
- The code validator uses a safe AST interpreter with bounded steps (4096) and iterations (1024) — no `eval`/`exec`
