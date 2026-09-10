# Live text adapters (Step 8)

Groq, OpenRouter explicit free models, and local Ollama now implement the common adapter
contract. All shipped switches remain off and provider candidates remain inactive. Demo mode
cannot be combined with live mode. No adapter downloads models, buys credits, upgrades an
account, or retries a paid route. Normal quality validation still determines acceptance;
availability and a successful smoke test do not qualify a model for general workloads.

## Review and transport boundaries

Documentation reviewed **2026-09-09**. Operator approval must cover the actual account,
model, workload, license, privacy terms and quotas. Public documentation does not establish
the eligibility of an individual account. These first cloud adapters accept PUBLIC data only.

| Adapter | Required access | Transport and checks |
| --- | --- | --- |
| `groq` | `FREE_RECURRING`, operator confirms free plan | Fixed `https://api.groq.com/openai/v1`; exact configured model must be active in current catalog; no Compound/server tools. |
| `openrouter_free` | `FREE_DYNAMIC`, operator confirms free account | Fixed `https://openrouter.ai/api/v1`; exact `owner/model:free`, zero catalog prices, `/key` must report `is_free_tier: true`. Automatic routers are rejected. |
| `ollama_local` | `FREE_LOCAL`, operator confirms local weights | Literal loopback HTTP only; existing GGUF tags and show metadata must confirm completion capability and context capacity. Optional exact model digest binding. Cloud names and remote metadata are rejected. |

Cloud terms reviews must have an explicit timezone and be no more than 30 days old;
future dates fail closed. Provider identity and access class must match the concrete adapter.
Catalog discovery intersects the reviewed allowlist; it never registers or approves new models.
Only text reasoning, coding and structured output capabilities are supported by these transports.
Declare capabilities only after testing the chosen model; a transport field is not capability proof.

Groq's [rate-limit documentation](https://console.groq.com/docs/rate-limits) describes account
and organization limits. FAIR reads **request** limit/remaining/reset headers conservatively.
Token headers never become request-reset evidence. No published allowance is hardcoded.
Groq free-plan status is an operator attestation: the models API does not prove that an account
cannot incur charges. See the [API reference](https://console.groq.com/docs/api-reference) and
[data handling policy](https://console.groq.com/docs/your-data); retention exceptions mean
that a blanket private-data approval would be inappropriate.

OpenRouter's [free suffix](https://openrouter.ai/docs/guides/routing/model-variants/free) and
[account limits](https://openrouter.ai/docs/api_reference/limits) are checked alongside each
configured model's catalog prices. All returned pricing fields must be finite zero, including
explicit prompt, completion and request prices; missing required prices fail closed. Requests
set `allow_fallbacks: false`, `require_parameters: true`, `data_collection: deny` and zero
`max_price` for prompt, completion, request and image according to
[provider selection](https://openrouter.ai/docs/guides/routing/provider-selection).
Account credit balances are not request quotas. Free routes have limited capacity and need
workload-specific eligibility review; see the [FAQ](https://openrouter.ai/docs/faq).

The returned OpenRouter `usage.cost` must also be finite zero. A missing, invalid or nonzero
cost causes a persistent global stop and provider security block, with an audited failed attempt.
The API returns HTTP 503 `PROVIDER_COST_POLICY_VIOLATION` and
`paid_inference_executed: null`, withholding the answer rather than asserting zero spend.
Investigate the account and upstream behavior before using the existing stopped/idle recovery
workflow. These controls cannot reverse a provider charge. The
[chat API](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion) and
[error reference](https://openrouter.ai/docs/api_reference/errors-and-debugging) define the
response envelopes. Exact returned model identity is required; cloud compatibility is still
unverified against real credentials, including any provider identity canonicalization.

Ollama can proxy cloud inference even through a local server. For a deployment, explicitly
disable server cloud access using `OLLAMA_NO_CLOUD=1` or `disable_ollama_cloud` in its server
configuration and restart Ollama, as described in the [FAQ](https://docs.ollama.com/faq).
FAIR additionally checks [local tags](https://docs.ollama.com/api/tags), GGUF metadata,
remote markers and completion/context support before each [chat](https://docs.ollama.com/api/chat).
It sends `stream: false`, a bounded context/output budget, and `keep_alive: 0`. It does not pull,
create or delete models. Literal loopback excludes Docker host aliases; run FAIR beside Ollama
in the same host/network namespace for this adapter. The local process and configuration are
trusted; metadata checks are not OS network isolation.

## Activation and smoke testing

1. Copy configuration into a private directory, for example ignored `secrets/live-config`.
   Retain the routing and quality files when using it as `FAIR_CONFIG_DIR` for the API.
2. Review the account and exact model. In `providers.yaml`, set the correct access class,
   `status: ACTIVE`, `current_access_cost_usd: 0`, all three paid/billing requirements false,
   `programmatic_access: true`, `production_eligibility: true`, a current timezone-aware cloud
   `terms_last_verified`, and the reviewed model ID/context/capabilities. Bind a local digest
   through `model_revision` where possible. Set a request limit only from actual account evidence.
   Do not copy another operator's attestations or fabricate approvals.
3. In `live_adapters.yaml`, set `enabled: true` and only the applicable account confirmation:
   `groq_free_plan_confirmed`, `openrouter_free_account_confirmed`, or
   `ollama_local_only_confirmed`. Other candidates remain inactive.
4. Inject `GROQ_API_KEY` or `OPENROUTER_API_KEY` for cloud use. These are provider-scoped
   environment credentials, distinct from FAIR client/admin keys; they are not read from YAML,
   returned, logged or persisted by the adapter. Restart after rotation. Never commit secrets.
5. Run one isolated smoke request, substituting the exact reviewed model:

```powershell
.\.venv\Scripts\python.exe -m fair.providers.smoke --config-directory secrets/live-config --provider ollama_local --model llama3.2:3b
```

The command uses in-memory routing state, one attempt, a 60-second total timeout and a bounded
arithmetic contract. It prints metadata only and exits nonzero on non-acceptance. It does not
change the production database or ship an activation configuration. A cloud invocation consumes
the account's free allowance. `/healthz` reports whether a live adapter is attached and the global
stop is clear; it does not assert remote availability, remaining quota or model quality.

## Evidence and limits

On 2026-09-09, local Ollama **0.33.3** and its existing **llama3.2:3b** GGUF model completed
the smoke request through the registry, router, governor, quality engine and SQLite persistence:
`ACCEPTED`, `DETERMINISTIC_ARITHMETIC`, one attempt, `paid_inference_executed: false`.
Configured context was 4096; observed model capacity was 131072. The exact local digest was
`a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72`.
No download or cloud request was made; the isolated local configuration checked this existing
model, without claiming the server's global cloud setting was changed.

Groq and OpenRouter keys were absent. Their transports and policy/error paths were tested with
HTTP mock transports; real cloud inference, exact endpoint compatibility and representative
quality/benchmark collection remain pending operator credentials and account approval.
All production candidates remain inactive. No database migration or quality-engine change is
required. Streaming, vision, embeddings, server tools, automatic discovery approval, token quota
accounting and distributed reservations are outside this increment.

All transports disable environment proxies and redirects, bound response bodies, reject
compressed/malformed JSON, enforce model identity and normalize diagnostics. Requests contain
the task and requested output schema, excluding FAIR identities and hidden host reference answers.
Each HTTP client closes on completion, error and cancellation. A valid Retry-After can extend
the durable cooldown; a successful request-quota observation can reduce available allowance
and provide an observed reset boundary, but cannot refund consumed usage within a window.
