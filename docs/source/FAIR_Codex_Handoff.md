# FAIR — Codex Build Handoff
## Implementation Contract v1.0

**Project:** FAIR — Free AI Router  
**Goal:** Build a production-oriented, governed, free-only AI routing service using the useful control patterns from the supplied ASVS Sprint 2/3 skeletons while explicitly excluding the ASVS financial/venture system.

---

## 0. Read this first

This file is the build contract. Do not rebuild ASVS. Create a new repository named `fair` (or use an empty FAIR repo supplied by the owner).

FAIR is a **free-only model router and quality governor**. It selects the best currently eligible free model for each task, monitors quota/health, evaluates output quality, changes models when limits or quality fail, learns model-task performance, and returns `ESCALATION_REQUIRED` when the free pool cannot satisfy the task.

A host application may have a paid model/subscription, but FAIR must **never** execute paid inference. Paid work is owned by a separate host `Paid Intelligence Governor`.

### Hard invariant

```yaml
MAX_FAIR_AI_SPEND_USD: 0.00
PAID_API_KEYS_ALLOWED: false
AUTO_TOPUP_ALLOWED: false
PAY_AS_YOU_GO_ALLOWED: false
CREDIT_PURCHASE_ALLOWED: false
PAID_SUBSCRIPTION_ROUTING_INSIDE_FAIR: false
```

If any implementation choice conflicts with that invariant, the invariant wins.

---

## 1. Source artifacts / donor code

If these files are available, inspect them before coding:

- `FAIR_Free_AI_Router_System_Specification_v1_0.md`
- `FAIR_Provider_Registry_Seed_v1_0.yaml`
- `ASVS_Build_Roadmap_v1.0.md`
- `asvs_sprint2_skeleton.tar.gz`
- `asvs_sprint3_skeleton.tar.gz`

The relevant ASVS donor subset has been checked with:

```bash
pytest -q \
  tests/integration/test_intelligence_router.py \
  tests/unit/test_ai_budget.py \
  tests/unit/test_scheduler.py \
  tests/unit/test_tool_gateway.py
```

Result in the supplied Sprint 3 skeleton: **28 passed**.

This is evidence that the donor patterns are coherent; it is **not** permission to copy the entire ASVS system.

---

## 2. Exact donor files to inspect and adapt

From `asvs_sprint3_skeleton.tar.gz`:

### KEEP/ADAPT heavily

```text
asvs/services/intelligence_router/router.py
asvs/services/intelligence_router/registry.py
asvs/services/usage_governor/scheduler.py
asvs/services/agents/tool_gateway.py
asvs/services/agents/isolation.py
asvs/packages/schemas/db.py
asvs/apps/api/main.py
asvs/infra/docker/docker-compose.yml
asvs/tests/integration/test_intelligence_router.py
asvs/tests/unit/test_scheduler.py
asvs/tests/unit/test_tool_gateway.py
```

### REUSE CONCEPT, NOT DOMAIN LOGIC

```text
asvs/services/usage_governor/ai_budget.py
asvs/services/intelligence_router/flagship.py
asvs/packages/schemas/models.py
```

Transform them as follows:

- `ai_budget.py` → `quota/governor.py`: replace dollar entitlement with provider quota, rate limit, reset, client fairness, and retry budget.
- `flagship.py` → `escalation/service.py`: create escalation evidence; never approve or execute paid inference.
- `models.py` → new FAIR domain models only.

### DO NOT COPY into FAIR

```text
asvs/packages/money/
asvs/services/financial_governor/
asvs/services/ledger/
asvs/services/payments/
asvs/services/risk/                  # financial exposure domain
asvs/services/compliance/            # venture/business compliance domain
asvs/services/agents/opportunity.py
asvs/services/agents/experiment.py
asvs/services/agents/strategy.py
asvs/services/agents/memory.py        # ASVS venture memory domain
asvs/agents/hermes-profiles/
asvs/workflows/payments/
asvs/workflows/weekly-close/
asvs/workflows/fulfillment/
asvs/workflows/deletion/
asvs/prompts/agents/
asvs/prompts/constitution/            # ASVS venture constitution; replace with FAIR policy config
```

Do not bring across Book A/Book B, TTD accounting, PayWise, capital floors, venture survival, A1/A2/A3 identity, revenue scoring, merit-by-profit, UDOs, opportunities, experiments, or agent deletion.

---

## 3. Target repository

```text
fair/
├── README.md
├── pyproject.toml
├── .env.example
├── apps/
│   └── api/
│       ├── __init__.py
│       └── main.py
├── fair/
│   ├── __init__.py
│   ├── governor/
│   │   ├── policy.py
│   │   ├── kill_switch.py
│   │   └── retry_policy.py
│   ├── classifier/
│   │   ├── task_profiler.py
│   │   └── rules.py
│   ├── router/
│   │   ├── selector.py
│   │   ├── orchestrator.py
│   │   └── scoring.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── secrets.py
│   │   ├── mock.py
│   │   ├── groq.py
│   │   ├── gemini.py
│   │   ├── cloudflare.py
│   │   ├── mistral.py
│   │   ├── kilo.py
│   │   ├── openrouter.py
│   │   ├── ollama_cloud.py
│   │   ├── huggingface.py
│   │   └── ollama_local.py
│   ├── quota/
│   │   ├── governor.py
│   │   ├── tracker.py
│   │   ├── rate_limits.py
│   │   ├── circuit_breaker.py
│   │   └── scheduler.py
│   ├── quality/
│   │   ├── engine.py
│   │   ├── scoring.py
│   │   ├── schema_validator.py
│   │   ├── factuality.py
│   │   ├── citations.py
│   │   ├── consensus.py
│   │   ├── code_validator.py
│   │   └── math_validator.py
│   ├── performance/
│   │   ├── registry.py
│   │   ├── benchmark.py
│   │   ├── shadow.py
│   │   └── drift.py
│   ├── escalation/
│   │   ├── models.py
│   │   └── service.py
│   ├── cache/
│   │   ├── keying.py
│   │   └── store.py
│   ├── security/
│   │   ├── capability.py
│   │   ├── isolation.py
│   │   └── untrusted.py
│   ├── observability/
│   │   ├── audit.py
│   │   ├── metrics.py
│   │   └── logging.py
│   ├── schemas/
│   │   ├── api.py
│   │   ├── domain.py
│   │   └── db.py
│   └── config.py
├── config/
│   ├── providers.yaml
│   ├── routing.yaml
│   └── quality_thresholds.yaml
├── migrations/
├── sdk/
│   ├── python/
│   └── javascript/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── quality/
│   ├── provider/
│   ├── security/
│   └── simulation/
└── infra/
    └── docker/
        └── docker-compose.yml
```

Use Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, Postgres in production, SQLite only for fast unit tests, `httpx` for provider clients, `pytest`, and structured JSON logging. Avoid adding a heavy agent framework to FAIR core.

---

## 4. Domain states and enums

### AccessClass

```text
FREE_RECURRING
FREE_DYNAMIC
FREE_LOCAL
```

No other access class may be ACTIVE in strict FAIR production.

### ProviderState

```text
ACTIVE
THROTTLED
QUOTA_PRESSURE
QUOTA_EXHAUSTED
DEGRADED
OUTAGE
DISABLED
TERMS_REVIEW
SECURITY_BLOCKED
```

### RequestStatus

```text
RECEIVED
PROFILED
ROUTING
EXECUTING
VALIDATING
ACCEPTED
UNVERIFIED_RESULT
ESCALATION_REQUIRED
FAILED
CANCELLED
```

### AttemptDisposition

```text
ACCEPTED
INFRA_FAILURE
QUOTA_FAILURE
QUALITY_FAILURE
CAPABILITY_MISMATCH
PRIVACY_BLOCK
TERMS_BLOCK
CANCELLED
```

Infrastructure/quota failures MUST NOT lower model quality scores. Quality failures MUST affect task-specific performance.

---

## 5. Required data models

Implement these as SQLAlchemy models and corresponding Pydantic DTOs.

### Provider

```text
id UUID
provider_id string unique
access_class enum
status enum
programmatic_access bool
production_eligibility bool
commercial_use_status string
terms_last_verified datetime nullable
auth_method string
max_data_class string
created_at
updated_at
```

### Model

```text
id UUID
provider_id FK
model_id string
context_window int nullable
reasoning bool
coding bool
vision bool
tool_calling bool
structured_output bool
embeddings bool
active bool
metadata_json json
```

### ProviderQuotaState

```text
id UUID
provider_id FK
model_id nullable
quota_unit string
quota_limit float nullable
quota_remaining_estimate float nullable
reset_at datetime nullable
rpm_limit int nullable
tpm_limit int nullable
rpd_limit int nullable
concurrent_limit int nullable
observed_rpm int
observed_tpm int
updated_at
```

### ProviderHealthEvent

```text
id UUID
provider_id
model_id nullable
state
http_status nullable
error_code nullable
latency_ms nullable
message nullable
created_at
```

### TaskRequest

```text
id UUID
client_id string
task_text text
task_type optional
required_quality_level string
privacy_class string
expected_schema_json nullable
status
created_at
completed_at nullable
```

### TaskProfile

```text
request_id PK/FK
task_class string
difficulty float 0..1
criticality string
requires_reasoning bool
requires_coding bool
requires_vision bool
requires_tools bool
requires_grounding bool
requires_structured_output bool
context_tokens_estimate int
latency_preference string
minimum_quality_score float
profile_source enum HOST|RULES|MODEL
```

Task classification itself should use deterministic rules first. If an LLM classifier is ever used, it must be a free eligible route and its output must be schema-validated.

### RoutingAttempt

```text
id UUID
request_id FK
attempt_number int
provider_id
model_id
selection_score float
selection_reason_json json
quota_snapshot_json json
started_at
completed_at nullable
latency_ms nullable
http_status nullable
disposition enum
error_type nullable
raw_response_ref nullable
```

Do NOT store secrets or model credentials in this table.

### QualityReport

```text
id UUID
attempt_id FK
overall_score float
instruction_score float nullable
structure_score float nullable
factuality_score float nullable
citation_score float nullable
consistency_score float nullable
code_test_score float nullable
hallucination_risk float nullable
hard_reject bool
reject_reasons json
validator_results json
created_at
```

### ModelTaskPerformance

```text
provider_id
model_id
task_class
attempts int
accepted int
quality_failures int
infra_failures int
hallucination_events int
avg_quality float
p10_quality float nullable
avg_latency_ms float
availability_rate float
recent_quality float
confidence float
updated_at
PRIMARY KEY(provider_id, model_id, task_class)
```

### FeedbackEvent

```text
id UUID
request_id
client_id
rating nullable
accepted bool nullable
correction_text nullable
reason nullable
created_at
```

### EscalationRequest

```text
id UUID
request_id
reason_code
models_tried json
best_quality_score float nullable
minimum_required float
model_disagreement string
recommended_capability string
summary text
created_at
```

### AuditEvent

Append-only.

```text
id UUID
request_id nullable
actor_type
actor_id
category
event_type
payload_json
created_at
```

---

## 6. Provider adapter contract

Define one normalized interface. Provider-specific modules implement it.

```python
from typing import Protocol


class ProviderAdapter(Protocol):
    provider_id: str

    async def health(self) -> ProviderHealth: ...
    async def quota(self) -> QuotaSnapshot: ...
    async def list_models(self) -> list[ModelDescriptor]: ...
    async def complete(self, request: NormalizedModelRequest) -> NormalizedModelResponse: ...
```

`NormalizedModelRequest` must support:

```text
messages/task
system_instruction optional
temperature optional
max_output_tokens optional
expected_json_schema optional
required_tools optional
metadata(request_id, client_id, task_class)
```

`NormalizedModelResponse` must include:

```text
provider_id
model_id
text
structured_data nullable
usage metadata if supplied
finish_reason
provider_request_id nullable
latency_ms
citations/evidence metadata if supplied
raw_metadata safe subset
```

Provider adapters must normalize:

- timeout
- 401/403 auth failure
- 429 rate/quota failure
- provider outage
- malformed response
- unsupported capability

into typed FAIR exceptions.

Do not infer free quota from dollar pricing in code. Quota state comes from provider APIs/headers when available, otherwise from configured limits plus observed counters. Exact provider quota values must live in `config/providers.yaml`, not in selector source code.

---

## 7. Provider registry and zero-spend admission gate

Start from `FAIR_Provider_Registry_Seed_v1_0.yaml` if present.

Initial candidates:

```text
groq
google_gemini_api
cloudflare_workers_ai
mistral
kilo_gateway
openrouter_free
ollama_cloud
huggingface_inference
ollama_local
```

`ollama_local` may default ACTIVE. All external candidates should default `TERMS_REVIEW` until the owner/operator marks them verified.

Implement:

```python
def admit_provider(spec) -> None:
    assert spec.access_class in {FREE_RECURRING, FREE_DYNAMIC, FREE_LOCAL}
    assert spec.current_access_cost_usd == 0
    assert spec.requires_paid_subscription is False
    assert spec.requires_credit_purchase is False
    assert spec.auto_billing_required is False
    assert spec.programmatic_access is True
    assert spec.production_eligibility is True
    assert spec.status not in {TERMS_REVIEW, SECURITY_BLOCKED, DISABLED}
```

A provider that fails any admission requirement must not be selectable.

Add a regression test proving that a synthetic paid provider cannot be activated even if somebody edits routing order.

---

## 8. Task profiler

Task profiler output drives candidate filtering and scoring.

Use deterministic heuristics first:

- code blocks, filenames, compiler/test language → coding/debugging
- numeric expressions/formulas → math
- “extract”, table/schema instructions → extraction
- factual/recent/source-required language → grounded research
- image input → vision
- requested JSON/schema → structured output
- long context → context-window constraint

Host may pass a trusted profile override.

Do not use the paid host model for profiling.

---

## 9. Model selector scoring

Do **not** create a global static order such as Gemini > Groq > Mistral.

First filter ineligible candidates. Then compute a score approximately like:

```text
selection_score =
    0.35 * task_quality_score
  + 0.15 * capability_fit
  + 0.10 * reliability_score
  + 0.10 * availability_score
  + 0.10 * quota_headroom_score
  + 0.08 * latency_score
  + 0.07 * score_confidence
  + 0.05 * diversity_bonus
  - hallucination_penalty
  - quota_scarcity_penalty
```

Make weights configuration, not constants hidden in code.

If there is insufficient performance history, use a neutral prior and allow controlled exploration. Never invent a precise benchmark score for a model that has not been measured.

A model that has failed quality on the current request must be excluded from immediate retry unless the failure was only a transient infrastructure condition.

---

## 10. Orchestration loop

Implement bounded orchestration:

```python
async def solve(req):
    enforce_governor(req)
    profile = profile_task(req)
    check_cache(req, profile)

    attempts = []
    for n in range(MAX_ATTEMPTS):
        candidates = selector.eligible_candidates(profile, attempts)
        if not candidates:
            return escalation_required(...)

        route = selector.choose(candidates, profile)

        try:
            response = await adapter(route).complete(...)
        except QuotaExceeded:
            mark_quota_exhausted(route)
            attempts.append(QUOTA_FAILURE)
            continue
        except RateLimited:
            mark_throttled(route)
            attempts.append(QUOTA_FAILURE or INFRA_FAILURE)
            continue
        except ProviderUnavailable:
            circuit_breaker.record_failure(route)
            attempts.append(INFRA_FAILURE)
            continue

        quality = await quality_engine.evaluate(req, profile, response)
        persist_attempt_and_quality(...)

        if quality.hard_reject:
            performance.record_quality_failure(...)
            attempts.append(QUALITY_FAILURE)
            continue

        if quality.overall_score >= profile.minimum_quality_score:
            performance.record_accept(...)
            cache_if_allowed(...)
            return ACCEPTED

        performance.record_quality_failure(...)
        attempts.append(QUALITY_FAILURE)

    return escalation_required(...)
```

Default `MAX_ATTEMPTS` should be small (for example 3) and configurable by task class/criticality. Cross-model verification is separate from retry count and also bounded.

---

## 11. Quality engine

This is new; do not inherit an ASVS equivalent because none exists.

### Order of validation

1. **Deterministic checks first**
   - response non-empty
   - JSON/schema parse
   - required fields
   - citations syntactically present if required
   - code compilation/unit tests if configured
   - deterministic arithmetic checks where possible
   - forbidden output patterns

2. **Grounding checks**
   - claim/evidence alignment when host supplied evidence
   - citation existence/URL/reference validation where possible
   - reject fabricated citations

3. **Consistency checks**
   - material self-contradiction
   - answer vs structured data mismatch

4. **Cross-model judge/check when warranted**
   - use a different provider/model from the producing model where possible
   - never let the same model be sole producer and sole judge for high-impact output

5. **Consensus/disagreement** for high-impact tasks

Return a `QualityReport`, never a bare boolean.

### Hard rejects

- fabricated citation
- required schema failure
- material unsupported factual claim in a grounded task
- material internal contradiction
- required tests fail
- policy/security failure

A hard reject cannot be overridden by a high average score.

---

## 12. Hallucination control

Do not promise impossible “hallucination elimination.” Implement measurable controls:

- require grounding for task classes that need current/factual truth
- isolate claims from style text
- validate citations/evidence when present
- use deterministic calculations/tests where possible
- cross-check high-impact claims with a different provider
- track per-model hallucination events by task class
- penalize models with repeated unsupported claims
- return uncertainty/escalation rather than inventing missing facts

Never label an answer “verified” solely because two models agree; correlated model errors are possible.

---

## 13. Quota and health manager

Replace ASVS money budget logic with quota state.

Track any subset of:

```text
RPM
RPH
RPD
TPM
TPD
monthly tokens
provider compute units
concurrency
reset_at
```

Provider adapters update state from headers/API when possible. Otherwise counters use configured conservative limits.

### Circuit breaker

Configurable thresholds, e.g.:

```text
CLOSED
  -> 3 infra failures within 60s
OPEN
  -> exclude provider for cooldown
HALF_OPEN
  -> one probe
CLOSED on success / OPEN on failure
```

Quality failures do not open an infrastructure circuit breaker. They affect task-performance routing weight instead.

---

## 14. Scheduler

Adapt `services/usage_governor/scheduler.py` to arbitrary `client_id`s.

Priorities:

```text
P0 security/system emergency
P1 interactive critical
P2 user-blocking normal
P3 batch/normal background
P4 benchmarking/shadow testing
```

Strict priority across classes; round-robin fairness within class. Preserve starvation tests from ASVS but replace agent IDs with client/workspace IDs.

---

## 15. Performance learning

Update `ModelTaskPerformance` after every completed attempt.

Use separate rolling and lifetime statistics. Avoid overreacting to tiny samples. `confidence` should rise with sample count and decay when a model/provider has not been tested recently.

Quality failure updates model task score. Infra failure updates availability/reliability, not quality.

Explicit host feedback should have high weight but be audit-logged.

---

## 16. Shadow benchmarking and drift detection

When quota headroom is high and the task is non-sensitive, optionally route a shadow copy to another eligible free model.

Shadow result is not returned to caller. Compare with accepted result and available deterministic evidence.

Example policy:

```text
quota headroom > 70% -> max shadow rate 10%
40-70% -> max 3%
< 40% -> 0%
```

Make thresholds configurable.

Drift detection compares recent task-quality distribution with historical baseline. If recent performance degrades materially, reduce routing weight and mark model for benchmark review.

---

## 17. Cache

Add deterministic request hashing and optional semantic cache only after basic router/quality path works.

Cache key must include at least:

```text
normalized task
relevant system/policy version
expected schema
privacy class
task class
freshness requirement
```

Do not cache tasks marked fresh/current unless TTL is explicitly supplied. Never share private cached data across clients unless configured.

---

## 18. Escalation contract

FAIR returns this; it does not call paid AI.

Example:

```json
{
  "status": "ESCALATION_REQUIRED",
  "request_id": "...",
  "reason_code": "ALL_FREE_MODELS_FAILED_QUALITY",
  "attempts": [
    {"provider": "groq", "model": "...", "disposition": "QUALITY_FAILURE", "quality": 71.2},
    {"provider": "gemini", "model": "...", "disposition": "QUALITY_FAILURE", "quality": 78.5}
  ],
  "minimum_required": 88,
  "best_quality_score": 78.5,
  "model_disagreement": "HIGH",
  "recommended_capability": "HIGH_REASONING_GROUNDED",
  "paid_inference_executed": false
}
```

Add a test that patches a fake paid adapter into dependency injection and proves FAIR core never invokes it.

---

## 19. API

### POST `/v1/solve`

Request:

```json
{
  "client_id": "my-app",
  "task": "...",
  "task_type": null,
  "quality_level": "standard",
  "privacy_class": "PUBLIC",
  "expected_schema": null,
  "required_capabilities": [],
  "freshness_required": false,
  "metadata": {}
}
```

Response statuses:

```text
ACCEPTED
UNVERIFIED_RESULT
ESCALATION_REQUIRED
FAILED
```

Return request ID, final model/provider only when appropriate, quality summary, attempts count, and explicit verification state. Do not leak raw secrets, hidden system prompts, or provider auth details.

### Administrative/read endpoints

```text
GET  /v1/providers
GET  /v1/providers/{provider_id}/health
GET  /v1/models/performance
GET  /v1/requests/{request_id}
GET  /v1/requests/{request_id}/audit
POST /v1/feedback
POST /v1/system/stop
POST /v1/system/resume
POST /v1/providers/{provider_id}/enable
POST /v1/providers/{provider_id}/disable
POST /v1/providers/{provider_id}/verify-terms
```

Admin endpoints require auth; build a simple API-key/RBAC boundary for v1.

---

## 20. Security requirements

Reuse the **patterns** from ASVS `tool_gateway.py` and `isolation.py`:

- default deny
- scoped capability where tools/retrieval are enabled
- no secret field in model-visible/token objects
- external content is untrusted data
- domain allowlisting when FAIR is given retrieval capability
- client isolation for private task/audit/cache data

Provider keys:

- load from environment or secret manager by provider ID
- never persist raw key in Postgres
- never log key
- never return key
- never place key in prompt/context

Add automated tests that scan serialized API responses and ORM-visible objects for credential fields.

---

## 21. Configuration files

### `config/providers.yaml`

Do not hard-code current quota numbers in router code.

Each provider/model entry should include:

```yaml
provider_id: groq
access_class: FREE_RECURRING
status: TERMS_REVIEW
production_eligibility: false
commercial_use_status: UNKNOWN
terms_last_verified: null
max_data_class: PUBLIC
auth_env: GROQ_API_KEY
models: []
quota: {}
rate_limit: {}
```

After operator verification, status/eligibility can change without code change.

### `config/quality_thresholds.yaml`

Start with:

```yaml
commodity: 75
standard: 82
advanced: 88
high_impact_support: 92
```

### `config/routing.yaml`

Put selector weights, retry limits, circuit breaker thresholds, and shadow rates here.

---

## 22. Tests that must exist before live provider integration

### Zero-spend/governance

1. Paid provider cannot be activated.
2. Auto-billing provider cannot be activated.
3. TERMS_REVIEW provider cannot be selected.
4. Kill switch prevents new model calls.

### Routing

5. Coding task excludes non-coding model when capability is mandatory.
6. Vision task excludes text-only model.
7. Context too large excludes insufficient-context model.
8. Best task-specific score wins when quota/health equal.
9. Quota scarcity can preserve a scarce model when a nearly-equivalent abundant model exists.

### Failover

10. 429 -> next free provider.
11. quota exhausted -> provider state QUOTA_EXHAUSTED -> next provider.
12. timeout/outage -> circuit breaker -> next provider.
13. all providers unavailable -> ESCALATION_REQUIRED/FAILED by policy.

### Quality

14. valid answer above threshold -> ACCEPTED.
15. schema failure -> hard reject -> different model.
16. fabricated citation -> hard reject.
17. material contradiction -> reject.
18. required code tests fail -> reject.
19. all free models below threshold -> ESCALATION_REQUIRED.
20. quality failure lowers task score; 429 does not.

### Security

21. credential strings never appear in response/log fixture.
22. private client cannot read another client's audit/cache record.
23. untrusted retrieved text cannot execute tool operations.

### Scheduler

24. round-robin fairness.
25. P0 preempts P3.
26. background shadow jobs cannot starve user-facing requests.

### Learning/drift

27. repeated accepted results raise model-task score/confidence.
28. repeated quality failures lower it.
29. recent drift reduces route weight.

### Escalation

30. escalation object contains attempts/evidence and `paid_inference_executed=false`.
31. FAIR core has no code path to a paid adapter.

---

## 23. Implementation order

Do not integrate nine live providers at once.

### Sprint A — clean core

- scaffold repo
- domain enums/models
- database + Alembic
- provider base + mock providers
- governor zero-spend admission gate
- selector skeleton
- quota state
- API `/v1/solve`
- audit events
- tests 1–13

Gate: all tests pass using mocks.

### Sprint B — quality and escalation

- deterministic quality validators
- quality report
- retry with different model
- escalation object
- model-task performance table
- tests 14–20 and 30–31

Gate: a deliberately hallucinating mock model is never returned as accepted.

### Sprint C — security/scheduler

- client isolation
- secret boundary
- capability/untrusted-content pattern if retrieval enabled
- scheduler
- tests 21–26

### Sprint D — learning

- feedback
- rolling metrics
- shadow benchmark
- drift detection
- tests 27–29

### Sprint E — first live providers

Integrate only 2–3 providers first, chosen from currently verified FREE candidates. Recommended initial technical diversity:

- one OpenAI-compatible fast inference provider
- Gemini adapter
- local Ollama

Do **not** activate a provider based only on this document. Operator must update `providers.yaml` from `TERMS_REVIEW` to ACTIVE after current terms/quota/privacy verification.

### Sprint F — remaining providers + SDKs

- Cloudflare/Mistral/Kilo/OpenRouter/Ollama Cloud/HF adapters as verified
- Python SDK
- JavaScript SDK
- Docker deployment
- runbooks

---

## 24. Definition of Done

FAIR v1 is done when:

- strict FAIR core cannot spend money or activate paid routes;
- task-specific model selection works from measured scores;
- rate/quota/outage automatically fails over;
- poor/hallucinated/invalid responses are rejected;
- all free failures produce structured escalation rather than garbage;
- paid inference remains outside FAIR;
- provider credentials are isolated;
- request/attempt/quality lineage is auditable;
- model-task performance learns from outcomes;
- multi-client scheduler cannot starve peers;
- local Docker deployment works;
- all required tests pass;
- at least three verified free routes can run end-to-end in a test deployment, including local Ollama if available.

---

## 25. Codex operating instructions

1. Do not add product scope not described here.
2. Do not silently reintroduce ASVS money, venture, or agent-runtime logic.
3. Do not hard-code marketing benchmark claims as model quality scores.
4. Do not invent provider quota values. Use config and mark unknown values explicitly.
5. Do not activate external providers whose terms status remains `TERMS_REVIEW`.
6. Do not use a paid API for convenience during tests; use mock adapters/local Ollama.
7. Preserve deterministic tests for all governance decisions.
8. Prefer small, reviewable commits by sprint/component.
9. After each sprint, run the full test suite and provide a concise changelog plus any unresolved assumptions.
10. If source artifacts conflict, this build handoff and the FAIR PDR govern the FAIR project; ASVS artifacts are donors only.
