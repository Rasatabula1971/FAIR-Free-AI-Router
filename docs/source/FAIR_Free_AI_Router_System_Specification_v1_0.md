# FAIR — Free AI Router
## Governed Free-Intelligence Routing System Specification

**Version:** 1.0  
**Date:** 2026-09-08  
**Status:** Architecture / implementation specification  
**Primary objective:** Use the best legitimately available **free** AI resource for each task while refusing poor, ungrounded, or hallucinated answers and preserving a clean escalation path to a host application's separately governed paid-intelligence layer.

---

# 1. Executive Summary

FAIR (**Free AI Router**) is a reusable software component, agent, service, or skill that sits between an application and multiple free AI providers.

FAIR does **not** blindly send every task to the same model. It determines what kind of intelligence a task requires, checks which free models are currently available, ranks them based on measured performance for that task type, routes the request, evaluates the answer, and automatically changes models when:

- the selected provider is rate-limited;
- its free quota is exhausted;
- the provider is unavailable or too slow;
- the model lacks a required capability;
- the answer fails structural validation;
- the answer is poorly grounded;
- the answer is internally contradictory;
- hallucination risk is too high;
- measured answer quality is below the configured acceptance threshold.

FAIR is **free-only by design**. It has no authority to spend money and should not hold paid-model credentials.

A host application may separately maintain a paid AI subscription or paid API for high-level work. FAIR can request escalation to that layer, but a separate **Paid Intelligence Governor** owned by the host application decides whether the paid model may be used.

The governing principle is:

> **Free first, but never free at the expense of acceptable quality.**

If no free route can produce an acceptable answer, FAIR must return `ESCALATION_REQUIRED` or `UNVERIFIED_RESULT`; it must never present a low-confidence answer as trustworthy merely to avoid paid inference.

---

# 2. Scope

FAIR is responsible for:

1. discovering and registering free AI resources;
2. tracking provider availability and free quotas;
3. classifying incoming tasks;
4. determining required capabilities;
5. selecting the best currently eligible free model;
6. routing requests;
7. validating structured outputs;
8. evaluating answer quality;
9. detecting likely hallucinations or unsupported claims;
10. retrying or changing providers when necessary;
11. combining multiple free models when one answer is insufficient;
12. caching reusable results;
13. preserving provider credentials and privacy boundaries;
14. logging model decisions and answer lineage;
15. learning which models perform best on actual application workloads;
16. issuing structured escalation requests to a host application's paid intelligence layer.

FAIR is **not** responsible for:

- spending money;
- owning the host application's paid AI credentials;
- deciding business policy;
- overriding application-level permissions;
- bypassing provider terms of service;
- hiding uncertainty;
- using consumer-chat automation where programmatic use is not permitted.

---

# 3. Non-Negotiable Governance Rules

## 3.1 Zero-spend invariant

The FAIR Core must enforce:

```yaml
max_fair_ai_spend_usd: 0.00
paid_api_keys_allowed: false
auto_topup_allowed: false
pay_as_you_go_allowed: false
credit_purchase_allowed: false
paid_subscription_routing_inside_fair: false
```

A provider may enter the FAIR production pool only if its configured route is presently classified as one of:

- `FREE_RECURRING`
- `FREE_DYNAMIC`
- `FREE_LOCAL`

Temporary trials, signup bonuses, promotional credits, referral credits, paid subscriptions and deposit-unlocked free quotas are excluded from the strict FAIR core.

## 3.2 Deterministic governance

Hard rules are code/configuration, not prompts.

No model may persuade FAIR to:

- use a paid endpoint;
- reveal credentials;
- ignore privacy classification;
- exceed a provider quota intentionally;
- lower a mandatory quality threshold;
- return an unverified high-risk answer as verified;
- bypass application-level permissions.

## 3.3 Free does not mean acceptable

Poor-quality answers are rejected.

A free model that produces unsupported facts, fabricated citations, invalid structured data, material contradictions, or unacceptable task performance is not considered a successful route.

## 3.4 Provider independence

No provider is privileged by brand.

Models are selected using measured application-specific performance, current availability, capability fit, quota pressure and quality history.

## 3.5 Separation from paid intelligence

FAIR may recommend escalation, but cannot execute paid inference itself.

```text
FAIR -> ESCALATION_REQUEST -> HOST PAID INTELLIGENCE GOVERNOR -> APPROVE / DENY
```

This separation allows FAIR to be embedded inside applications that use a paid model only for high-value or high-intelligence work without weakening FAIR's zero-spend guarantee.

---

# 4. High-Level Architecture

```text
                     HOST APPLICATION
                           |
                    Application Policy
                           |
             +-------------+--------------+
             |                            |
       Routine / normal              High-criticality
          AI tasks                       tasks
             |                            |
             v                            v
          +------+               Paid Intelligence
          | FAIR |<--escalation--+    Governor
          +------+               +---------+-------+
             |                             |
             |                             v
             |                       Paid AI Layer
             |
             v
+---------------------------------------------------------+
|                     FAIR GOVERNOR                       |
| zero spend | privacy | eligibility | quota | audit      |
+---------------------------------------------------------+
             |
             v
+---------------------------------------------------------+
|                    TASK PROFILER                        |
| classify task | capability needs | risk | context       |
+---------------------------------------------------------+
             |
             v
+---------------------------------------------------------+
|                   MODEL SELECTOR                        |
| benchmark score | capability | quota | health | latency |
+---------------------------------------------------------+
             |
             v
+---------------------------------------------------------+
|                    FREE AI POOL                         |
| Groq | Gemini | Cloudflare | Mistral | Kilo |           |
| OpenRouter free | Ollama Cloud | local Ollama | others  |
+---------------------------------------------------------+
             |
             v
+---------------------------------------------------------+
|                  QUALITY ENGINE                         |
| format | grounding | consistency | citations | evidence |
| cross-check | hallucination risk | acceptance threshold |
+---------------------------------------------------------+
             |
       +-----+------+------------------+
       |            |                  |
    ACCEPT      TRY OTHER FREE     ESCALATE
       |            MODEL              |
       v                               v
   return result                  host governor
```

---

# 5. Major Components

## 5.1 FAIR Governor

The deterministic control plane.

Responsibilities:

- enforce zero-spend rule;
- allow only approved provider classes;
- protect credentials;
- enforce privacy/data policies;
- enforce request and provider rate limits;
- prevent runaway retry loops;
- disable degraded providers;
- maintain kill switches;
- control escalation permissions;
- create append-only decision/audit records.

## 5.2 Provider Registry

Maintains the authoritative record for every model/provider.

Each provider/model record should include:

```yaml
provider_id:
model_id:
access_class:
programmatic_access:
production_eligibility:
commercial_use_status:
terms_last_verified:
auth_method:
context_window:
capabilities:
  reasoning:
  coding:
  vision:
  tool_calling:
  structured_output:
  embeddings:
quota:
  unit:
  limit:
  reset_rule:
  remaining_estimate:
rate_limit:
  rpm:
  tpm:
  concurrent_requests:
health:
  status:
  rolling_latency_ms:
  recent_failure_rate:
quality:
  overall_score:
  factuality_score:
  coding_score:
  research_score:
  extraction_score:
  writing_score:
  reasoning_score:
  hallucination_rate:
security:
  maximum_data_class:
  credential_reference:
```

Provider eligibility is configuration, not hard-coded architecture. Free plans change; FAIR must be able to disable or modify a provider without changing the application.

## 5.3 Task Profiler

Every request is classified before model selection.

Suggested task classes:

- classification;
- extraction;
- summarisation;
- transformation;
- structured-data generation;
- coding;
- debugging;
- mathematical reasoning;
- general reasoning;
- factual research;
- web-grounded research;
- planning;
- strategy;
- long-context analysis;
- vision/image understanding;
- tool use;
- creative writing;
- high-risk/irreversible decision support.

The profiler also estimates:

- required context length;
- reasoning depth;
- tool requirements;
- factuality requirement;
- freshness requirement;
- latency tolerance;
- data sensitivity;
- consequence of error;
- expected output schema.

## 5.4 Capability Matcher

Rejects models that cannot perform the task before quality scoring.

Examples:

- image input requires vision;
- 80K-token document requires sufficient context or chunking strategy;
- structured API response may require strong JSON/schema reliability;
- live research requires tool/web support in the host environment;
- code execution must not be assigned to a text-only route unless a tool executor is available.

## 5.5 Free Capacity Governor

Tracks scarcity even though cash cost is zero.

It maintains:

- provider quota remaining;
- reset time;
- requests per minute;
- tokens per minute/day/month;
- concurrency;
- rolling quota estimation when provider data is opaque;
- quota burn rate;
- predicted exhaustion time.

The router should preserve scarce high-performing free capacity for tasks where it adds the most value.

## 5.6 Model Selector

The selector ranks only eligible models.

Selection should be task-specific rather than global.

A configurable default scoring model:

```text
Route Score =
  Task Quality Fit
+ Factual Reliability
+ Capability Match
+ Provider Health
+ Context Fit
+ Quota Availability
+ Latency Suitability
- Hallucination Penalty
- Recent Failure Penalty
- Scarcity Penalty
```

Weights should vary by task.

For factual research, factual reliability and grounding dominate.

For coding, coding benchmark performance and test-pass rate dominate.

For summarisation, faithfulness and context handling dominate.

For classification/extraction, schema accuracy and deterministic benchmark performance dominate.

## 5.7 Provider Adapter Layer

Every provider is hidden behind a common internal interface.

Example:

```python
response = fair.generate(
    task=task,
    messages=messages,
    requirements=requirements,
    output_schema=schema,
)
```

Adapters normalize:

- messages;
- system prompts;
- token limits;
- tool/function calling;
- structured outputs;
- streaming;
- usage data;
- rate-limit errors;
- quota errors;
- provider-specific failure codes.

## 5.8 Execution Manager

Responsible for:

- retries;
- timeouts;
- exponential backoff;
- idempotency;
- cancellation;
- provider switching;
- parallel independent calls when required;
- execution budgets.

A retry against the same model should occur only for transient transport/provider errors. A **quality failure should normally cause a model change**, not repeated attempts to obtain a different answer from the same weak route.

---

# 6. Request State Machine

```text
RECEIVED
   |
   v
POLICY_CHECK
   |
   v
TASK_CLASSIFIED
   |
   v
CANDIDATES_SELECTED
   |
   v
FREE_MODEL_RUNNING
   |
   v
QUALITY_CHECK
   |
   +---------------- ACCEPTED ----------------> RETURNED
   |
   +--- QUALITY_FAIL ---> NEXT_FREE_MODEL
   |                         |
   |                         +--> QUALITY_CHECK
   |
   +--- QUOTA_LIMIT ------> MARK_QUOTA_EXHAUSTED
   |                         |
   |                         +--> NEXT_FREE_MODEL
   |
   +--- PROVIDER_FAIL ----> MARK_DEGRADED
   |                         |
   |                         +--> NEXT_FREE_MODEL
   |
   +--- ALL_FREE_FAILED --> ESCALATION_REQUIRED
                              |
                              v
                         HOST GOVERNOR
```

---

# 7. Task Intelligence Levels

FAIR should not attempt every task simply because free capacity exists.

Suggested host-visible levels:

## L0 — Deterministic

No LLM needed.

Examples:

- arithmetic;
- validation;
- simple mapping;
- database lookup;
- exact rule evaluation;
- cached result.

## L1 — Commodity AI

High confidence that free models can handle it reliably.

Examples:

- classification;
- extraction;
- formatting;
- simple summarisation;
- rewriting;
- simple code transformations.

## L2 — Standard Reasoning

Normal FAIR workload.

Examples:

- research summaries;
- ordinary coding;
- planning;
- comparison;
- document analysis;
- data interpretation.

## L3 — Advanced Reasoning

FAIR may try the strongest suitable free models and/or multiple-model consensus.

Examples:

- complex debugging;
- difficult architecture;
- multi-step reasoning;
- nuanced research synthesis.

If quality remains below threshold, FAIR returns `ESCALATION_REQUIRED`.

## L4 — High-Impact / High-Intelligence

The host application may choose to bypass free inference and send the task directly to its Paid Intelligence Governor.

Examples:

- irreversible financial decisions;
- high-stakes legal/medical/safety decisions;
- major strategic decisions;
- complex security decisions;
- tasks with extremely high cost of error.

FAIR may still be used for supporting sub-tasks, retrieval, extraction or independent second opinions where host policy permits.

---

# 8. Quality Engine

## 8.1 Principle

FAIR does not measure success as “the API returned text.”

Success means **acceptable output**.

## 8.2 Quality dimensions

Depending on task type, evaluate:

1. instruction adherence;
2. factual grounding;
3. source support;
4. internal consistency;
5. completeness;
6. schema/format validity;
7. calculation correctness;
8. code test results;
9. citation validity;
10. relevance;
11. uncertainty calibration;
12. contradiction rate;
13. hallucination risk.

## 8.3 Default quality levels

Suggested configurable defaults:

```yaml
quality_thresholds:
  commodity: 75
  standard: 82
  advanced: 88
  high_impact_support: 92
```

Scores are not universal truth; they are application-specific control thresholds.

## 8.4 Deterministic checks first

Before using another LLM as a judge, FAIR should use deterministic verification whenever possible:

- JSON schema validation;
- regex/format validation;
- parser validation;
- arithmetic recomputation;
- unit tests;
- SQL validation;
- source URL existence;
- citation-to-source matching;
- required-field presence;
- duplicate/contradiction checks.

## 8.5 Evidence grounding

For factual or research outputs:

- identify claims that require evidence;
- require retrieval/tool evidence where freshness matters;
- associate claims with supporting source snippets/IDs;
- reject fabricated citations;
- penalize claims not supported by retrieved evidence;
- distinguish source-derived facts from model inference.

## 8.6 Cross-model verification

For important tasks, FAIR may call a **different model/provider** to critique or independently solve the problem.

The second model should not merely be asked “is this correct?” It should receive the task, evidence and candidate answer and be asked to identify specific unsupported claims, contradictions and missing requirements.

## 8.7 Consensus mode

For difficult tasks where free calls are available:

```text
Model A -> answer A
Model B -> answer B
Model C -> answer C
          |
          v
 disagreement analysis
          |
          v
 evidence-based synthesis
```

Consensus is not majority voting on facts. Evidence outranks model count.

## 8.8 Uncertainty requirement

If FAIR cannot establish adequate confidence, it must return uncertainty explicitly.

Permitted outcomes include:

- `ACCEPTED`
- `ACCEPTED_WITH_UNCERTAINTY`
- `UNVERIFIED_RESULT`
- `ESCALATION_REQUIRED`
- `NO_ELIGIBLE_FREE_MODEL`

---

# 9. Hallucination Guard

Hallucinating answers are considered a routing failure.

## 9.1 Detection signals

- unsupported factual claims;
- citations not present in retrieval results;
- invented quotations;
- impossible dates/entities;
- contradiction with trusted tool output;
- mathematical mismatch;
- answer changes materially under independent re-query;
- model confidence unsupported by evidence;
- structured fields filled with guessed values.

## 9.2 Response policy

When hallucination risk exceeds threshold:

1. reject candidate response;
2. mark quality failure against the model/task profile;
3. retry using a different free model or grounded workflow;
4. use independent verification if appropriate;
5. escalate if free routes remain inadequate.

FAIR must **never silently repair invented facts by inventing more facts**.

---

# 10. Model Performance Ledger

FAIR should learn from real workloads.

Each model maintains task-specific statistics:

```text
MODEL               FACTUAL   CODING   EXTRACT   REASONING   FORMAT   LATENCY
-----------------------------------------------------------------------------
Provider/Model A       91        73       96         82         98       420ms
Provider/Model B       83        94       89         90         91       800ms
Provider/Model C       95        61       92         84         87      1500ms
```

Metrics may include:

- accepted-output rate;
- hallucination rate;
- evidence-grounding rate;
- schema pass rate;
- code test-pass rate;
- average retries;
- average latency;
- timeout rate;
- provider failure rate;
- user/downstream rejection rate;
- performance by task class;
- performance by context length.

Recent performance should matter more than historical performance because providers/models change.

---

# 11. Benchmark Harness

FAIR should include a continuously runnable benchmark suite made from the host application's **actual tasks**, not generic leaderboard scores alone.

Benchmark groups:

- factuality;
- retrieval grounding;
- extraction;
- JSON generation;
- coding;
- reasoning;
- summarisation faithfulness;
- long-context handling;
- tool calling;
- vision if required.

Each candidate model should be admitted to production only after passing minimum task-specific acceptance tests.

When providers change model versions, FAIR should re-run affected benchmarks.

---

# 12. Free-Quota and Provider State Management

Provider states:

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

## 12.1 Quota exhaustion

When a provider returns quota/rate-limit exhaustion:

1. classify the error;
2. record the limit;
3. estimate/reset timestamp;
4. mark the provider/model unavailable for the relevant window;
5. immediately route to the next eligible model;
6. do not repeatedly hammer the exhausted provider.

## 12.2 Quota preservation

A scarce free model that is exceptionally good at coding should not be wasted on basic rewriting if a less scarce model performs rewriting adequately.

The selector therefore includes a scarcity/opportunity-cost penalty.

## 12.3 Quota reset scheduler

At each known reset:

- restore eligibility;
- run a small health probe if needed;
- update quota estimate;
- return provider to `ACTIVE` if healthy.

---

# 13. Paid Intelligence Escalation Interface

FAIR itself remains free-only.

A host application may expose:

```text
PaidIntelligenceGovernor.request(...)
```

FAIR emits a structured escalation object:

```json
{
  "status": "ESCALATION_REQUIRED",
  "task_id": "...",
  "task_class": "advanced_reasoning",
  "risk_level": "high",
  "reason": "all eligible free routes failed quality threshold",
  "minimum_required_quality": 88,
  "best_free_quality": 74,
  "attempted_routes": [
    {"provider": "...", "model": "...", "result": "quality_fail"},
    {"provider": "...", "model": "...", "result": "quota_exhausted"}
  ],
  "unsupported_claims": ["..."],
  "recommended_paid_capability": "frontier_reasoning",
  "supporting_context_ref": "..."
}
```

The host application may:

- approve paid inference;
- deny escalation;
- request human review;
- queue until free quota resets;
- reduce task requirements.

The paid layer's answer should return through the host application's own quality/governance controls.

---

# 14. Direct Paid Bypass for High-Level Tasks

Some tasks should not waste multiple free calls if application policy already knows they require frontier-level reasoning.

The host can classify them as:

```text
PAID_DIRECT_ELIGIBLE
```

FAIR may still assist with free sub-tasks such as:

- extracting data;
- preparing context;
- retrieving references;
- formatting inputs;
- generating alternative hypotheses;
- independent checking after paid output.

This minimizes paid-token use while reserving high-grade intelligence for the part that actually requires it.

---

# 15. Privacy and Data Classification

Free services may have different data-use terms. FAIR must route according to data sensitivity, not merely quality.

Suggested data classes:

- `PUBLIC`
- `INTERNAL`
- `CONFIDENTIAL`
- `RESTRICTED`
- `SECRET`

Each provider specifies the maximum class it may receive.

`SECRET` data should never be placed into an external free model unless the host explicitly configures such use and provider terms permit it. Prefer local inference for sensitive workloads.

Secrets/API keys must never appear in prompts or audit logs.

---

# 16. Credential Architecture

```text
CALLING APP / AGENT
        |
        v
       FAIR
        |
        v
 Credential Broker / Secret Store
        |
        v
  Free Provider API
```

The calling application does not receive provider keys unless the integration explicitly requires it.

Controls:

- environment-specific credentials;
- least privilege;
- encryption at rest;
- key rotation;
- request IDs;
- credential revocation;
- no secrets in prompts;
- no secrets in application logs.

---

# 17. Caching

Caching is a capacity feature, not merely a speed feature.

FAIR should support:

## Exact cache

Identical normalized task/context -> previously validated response.

## Semantic cache

Near-equivalent question -> candidate cached answer, only when reuse is safe.

## Retrieval cache

Reuse fetched documents/evidence within freshness rules.

## Prompt-prefix cache

Use provider-supported cached context where available.

Cached results must preserve:

- source/evidence IDs;
- generation time;
- freshness limit;
- model/version;
- quality score;
- privacy classification.

---

# 18. Context Management

FAIR should manage context rather than fail when a free model has a smaller window.

Capabilities:

- token estimation;
- chunking;
- hierarchical summarisation;
- retrieval-based context selection;
- duplicate removal;
- source prioritisation;
- context compression;
- model switching when large context is essential.

Any compression step that could lose critical facts must be visible in the route log.

---

# 19. Structured Outputs

Where applications depend on machine-readable output:

1. define a JSON Schema/Pydantic model;
2. ask the provider for structured output if supported;
3. parse response;
4. validate schema;
5. reject invalid output;
6. attempt deterministic repair only for syntactic defects;
7. switch models if semantic fields are missing or fabricated.

The system must distinguish **syntax repair** from **content invention**.

---

# 20. Tool Use

FAIR should be able to call models that reason about tools while keeping actual tool authority in the host application.

```text
MODEL proposes tool call
        |
        v
FAIR/HOST validates permission
        |
        v
HOST executes tool
        |
        v
result returned to model
```

The model never receives unrestricted system, database, financial or security credentials.

---

# 21. Provider Discovery

A useful optional feature is a **Free Provider Watcher**.

It periodically or manually checks configured provider documentation for:

- new free tiers;
- free models added/removed;
- quota changes;
- authentication changes;
- context changes;
- model deprecations;
- terms changes;
- production/commercial-use restrictions.

Changes should never automatically activate a provider in production.

Process:

```text
DISCOVERED -> TERMS_REVIEW -> BENCHMARK -> APPROVED -> ACTIVE
```

---

# 22. User and Downstream Feedback

FAIR should learn from real outcomes.

Feedback signals:

- user thumbs up/down;
- explicit correction;
- application rejected output;
- code failed tests;
- parser rejected output;
- human-approved answer;
- downstream conversion/success metric where appropriate.

Feedback updates task-specific model rankings but must be resistant to single noisy events.

---

# 23. Reliability and Circuit Breakers

Automatic provider circuit breaker triggers may include:

- repeated 5xx failures;
- authentication anomaly;
- unexpected billing response;
- model identity/version mismatch;
- quota behavior change;
- sharp hallucination-rate increase;
- response latency > configured tolerance;
- malformed outputs above threshold;
- provider terms uncertainty.

A provider in circuit-breaker state is not routed production traffic until revalidated.

---

# 24. Audit and Answer Lineage

Every request should record:

- request/task ID;
- caller/application;
- task class;
- required capabilities;
- privacy classification;
- candidate models considered;
- route scores;
- provider/model chosen;
- quota state before/after;
- latency;
- input/output usage if reported;
- route/fallback reasons;
- validation results;
- quality score;
- hallucination flags;
- evidence/source references;
- final status;
- escalation request if any;
- user/downstream feedback.

This allows a developer to answer:

> Why did FAIR choose this model and why did it trust this answer?

---

# 25. Observability Dashboard

Recommended dashboard panels:

## Provider health

- active providers;
- degraded providers;
- quota remaining/reset;
- latency;
- failure rate.

## Quality

- acceptance rate by model/task;
- hallucination rate;
- grounding failures;
- schema failures;
- retry rate;
- escalation rate.

## Efficiency

- percentage served by cache;
- percentage served by local inference;
- free calls per provider;
- quota burn forecast;
- avoided paid escalations where host can estimate them.

## Paid boundary

FAIR itself always reports:

```text
FAIR inference spend = $0.00
```

The host application tracks paid intelligence separately.

---

# 26. Core API

Suggested REST interface:

## `POST /v1/solve`

```json
{
  "task": "...",
  "task_type": "auto",
  "intelligence_level": "auto",
  "quality_required": 82,
  "data_class": "PUBLIC",
  "freshness": "current",
  "tools": ["web"],
  "output_schema": null,
  "allow_escalation_request": true
}
```

Possible response:

```json
{
  "status": "ACCEPTED",
  "answer": "...",
  "quality_score": 89,
  "provider": "...",
  "model": "...",
  "verified": true,
  "evidence": [],
  "route_trace_id": "..."
}
```

Or:

```json
{
  "status": "ESCALATION_REQUIRED",
  "answer": null,
  "reason": "No eligible free model met the required quality threshold",
  "best_free_quality": 76,
  "route_trace_id": "..."
}
```

## Additional endpoints

```text
GET  /v1/providers
GET  /v1/providers/{id}/health
GET  /v1/quotas
POST /v1/benchmark
GET  /v1/routes/{trace_id}
POST /v1/feedback
POST /v1/providers/{id}/disable
POST /v1/providers/{id}/enable
```

---

# 27. Embedding FAIR Into Other Applications

FAIR should be usable as:

1. **local library/SDK**;
2. **sidecar service**;
3. **Docker service**;
4. **OpenClaw/Hermes/CrewAI skill or tool**;
5. **central network service for several applications**.

Recommended integration pattern:

```text
APP
 |
 +--> deterministic code
 |
 +--> FAIR for ordinary AI work
 |
 +--> Paid Intelligence Governor for high-level work
```

The host should be able to configure:

```yaml
ai_policy:
  default_route: fair
  direct_paid_levels: [L4]
  fair_escalation_allowed: true
  human_review_levels: [L4]
```

---

# 28. Current Seed Free-Provider Pool

The production registry should be dynamically verified because free tiers change. The following are **seed candidates identified during FAIR research**, not permanent constitutional assumptions:

## Core candidates

- Groq Free API
- Google Gemini Developer API Free tier
- Cloudflare Workers AI Free plan
- Mistral Free API mode
- Kilo free-model gateway
- OpenRouter free-model routes that require no deposit
- Ollama Cloud Free plan
- local Ollama/open-weight models

## Secondary candidate

- Hugging Face Inference Providers free recurring credit

## Admission rule

Before activation, verify:

1. currently $0;
2. recurring or ongoing, not temporary promotional credit;
3. usable programmatically;
4. no mandatory paid subscription/deposit;
5. no automatic billing requirement;
6. terms permit the intended workload;
7. privacy policy is compatible;
8. model passes FAIR benchmarks.

If any condition is uncertain, state = `TERMS_REVIEW` and production routing is blocked.

---

# 29. Recommended Data Model

Minimum tables/collections:

```text
providers
models
provider_quotas
provider_health
model_benchmarks
model_task_scores
requests
route_attempts
quality_evaluations
evidence_records
cache_entries
escalation_requests
feedback
policy_config
audit_events
```

---

# 30. Recommended Technology Shape

Implementation should remain replaceable, but a practical first build is:

- Python;
- FastAPI;
- Pydantic/JSON Schema;
- SQLite for local prototype or PostgreSQL for service deployment;
- Redis optional for quotas/cache/concurrency;
- Docker;
- provider adapter abstraction;
- Ollama for local models;
- OpenTelemetry-compatible tracing/logging where practical.

A third-party multi-provider library may reduce adapter work, but FAIR governance, quality evaluation, quota policy and escalation must remain FAIR-owned rather than being delegated to a model-provider SDK.

---

# 31. Features That Should Be Added Beyond Basic Routing

These features materially improve FAIR and should be included in the design:

1. **Application-specific benchmarking** — models ranked on real workload, not marketing benchmarks.
2. **Continuous model score decay/revalidation** — old performance does not remain trusted indefinitely.
3. **Evidence-linked answers** — factual claims traceable to retrieved evidence.
4. **Cross-provider verification** — important answers checked by an independent provider.
5. **Consensus/disagreement detection** — disagreement triggers investigation rather than blind synthesis.
6. **Quota forecasting** — preserve scarce free resources.
7. **Semantic/exact caching** — reduces free quota consumption.
8. **Privacy-aware routing** — sensitive data may force local inference or escalation.
9. **Provider terms registry** — free does not override legal/commercial restrictions.
10. **Circuit breakers** — automatically stop degraded or anomalous providers.
11. **Structured-output verification** — essential for app integration.
12. **Context optimization** — chunk/compress/retrieve rather than fail on small context windows.
13. **Tool-permission separation** — model proposes; host executes.
14. **Answer lineage** — every final answer traceable to model, evidence and quality checks.
15. **Escalation contracts** — clean handoff to paid intelligence without giving FAIR spending power.
16. **Direct paid bypass policy** — obvious L4 work need not burn free quotas first.
17. **Free-provider watcher** — discover new capacity without auto-enabling it.
18. **Health probes and reset scheduler** — automatically restore providers after limits reset.
19. **Per-task timeout/latency policy** — fastest acceptable model for interactive workloads.
20. **Human feedback loop** — model rankings improve from real outcomes.
21. **Canary testing** — new model/version gets a small fraction of low-risk work before broad use.
22. **Prompt/version registry** — quality changes can be traced to prompt changes as well as models.
23. **Red-team benchmark set** — tests fabricated sources, prompt injection, adversarial instructions and schema manipulation.
24. **Graceful no-answer behavior** — inability to verify is preferable to fabrication.
25. **Offline/local fallback** — service remains partially useful during internet/provider outages.

---

# 32. Acceptance Criteria

FAIR v1 should not be considered ready until all of the following are demonstrated:

1. At least three independent free provider routes operate through the common adapter layer.
2. Local model route operates without external inference cost.
3. Paid API credentials cannot be configured inside FAIR production mode.
4. A simulated quota-exhausted provider causes automatic failover.
5. A provider outage causes automatic failover without duplicate application actions.
6. A deliberately hallucinated research answer is rejected by the quality pipeline.
7. A fabricated citation is detected and rejected.
8. Invalid JSON output is not returned as successful structured output.
9. Coding route can use deterministic tests as a quality signal.
10. Model rankings differ by task category when benchmark results justify it.
11. Provider/model changes are auditable.
12. An advanced task that cannot meet the free quality threshold returns `ESCALATION_REQUIRED`.
13. FAIR itself incurs $0 in paid inference during acceptance testing.
14. Free-provider quota reset restores the route automatically.
15. Sensitive-data policy prevents use of disallowed external free providers.
16. Every returned answer has a route trace and quality record.
17. Retry loops have hard ceilings.
18. No model has access to provider credentials or host paid-model credentials.

---

# 33. Recommended Build Sequence

## Stage 1 — FAIR Core

- policy engine;
- provider registry;
- common provider interface;
- request/task schema;
- audit log;
- zero-spend invariant.

## Stage 2 — Multi-provider routing

- integrate first 3–4 free providers;
- quota manager;
- health manager;
- routing/failover;
- local Ollama route.

## Stage 3 — Quality system

- deterministic validators;
- task-specific scoring;
- evidence grounding;
- cross-model critic;
- hallucination guard.

## Stage 4 — Learning router

- benchmark harness;
- model performance ledger;
- task-specific rankings;
- quota-aware optimization.

## Stage 5 — Host integration

- REST API/SDK;
- escalation contract;
- Paid Intelligence Governor interface;
- tool permission interface.

## Stage 6 — Production hardening

- privacy routing;
- credential vault;
- circuit breakers;
- dashboards;
- provider watcher;
- canary testing;
- red-team tests;
- backup/recovery.

---

# 34. Final System Principle

FAIR is not a system for obtaining the cheapest possible answer.

It is a system for obtaining the **best acceptable answer from legitimately free AI capacity**, while intelligently managing provider strengths, quotas, failures and quality.

The host application's paid AI exists above FAIR as a deliberately scarce high-intelligence resource.

The resulting hierarchy is:

```text
DETERMINISTIC CODE
       |
       v
FAIR — FREE AI ROUTER
       |
       | acceptable answer
       +----------------------> RETURN
       |
       | free routes inadequate
       v
ESCALATION REQUEST
       |
       v
HOST PAID INTELLIGENCE GOVERNOR
       |
       +--> DENY / HUMAN REVIEW / QUEUE
       |
       `--> PAID HIGH-INTELLIGENCE MODEL
```

The correct optimization target is therefore:

> **Maximum acceptable intelligence per zero-cost FAIR request, with paid intelligence reserved by the host for work whose required quality or consequence justifies it.**

