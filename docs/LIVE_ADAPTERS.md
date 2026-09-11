# Live text adapters (Step 8)

Groq, Gemini, OpenRouter explicit free models, and local Ollama now implement the common adapter
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
| `google_gemini_api` | `FREE_RECURRING`, operator confirms Gemini free tier and reviewed zero-priced model | Fixed `https://generativelanguage.googleapis.com/v1beta`; exact model metadata, input/output limits and optional catalog revision checked before generation. |
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

Cloud activation now also requires [provider/model qualification evidence](PROVIDER_QUALIFICATION.md)
and database migration `0009`. Complete reviewed direct diagnostics and zero-charge checks
before the full-router smoke test. The previous account flags alone no longer suffice;
missing, stale or incomplete qualification records deny cloud admission.

1. Copy configuration into a private directory, for example ignored `secrets/live-config`.
   Retain the routing and quality files when using it as `FAIR_CONFIG_DIR` for the API.
2. Review the account and exact model. In `providers.yaml`, set the correct access class,
   `status: ACTIVE`, `current_access_cost_usd: 0`, all three paid/billing requirements false,
   `programmatic_access: true`, `production_eligibility: true`, a current timezone-aware cloud
   `terms_last_verified`, and the reviewed model ID/context/capabilities. Bind a local digest
   through `model_revision` where possible. Set a request limit only from actual account evidence.
   Do not copy another operator's attestations or fabricate approvals.
3. In `live_adapters.yaml`, set `enabled: true` and only the applicable account confirmation:
   `groq_free_plan_confirmed`, `gemini_free_tier_confirmed`, `openrouter_free_account_confirmed`, or
   `ollama_local_only_confirmed`. Other candidates remain inactive.
4. Inject `GROQ_API_KEY`, `GEMINI_API_KEY` or `OPENROUTER_API_KEY` for cloud use, or give the smoke
   command an explicit local `--env-file`. These are provider-scoped
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

### Gemini implementation

Gemini uses the native [generateContent endpoint](https://ai.google.dev/api/generate-content)
and [`x-goog-api-key` header](https://ai.google.dev/gemini-api/docs/api-key). Keys never enter
URLs, model payloads or configuration snapshots. Only exact reviewed `gemini-*` names are
accepted; tuned-model paths, unrelated model families and path/query injection are rejected.
No model becomes free or approved merely because its ID matches that syntax.

Discovery fetches only configured active models using [models.get](https://ai.google.dev/api/models),
checking identity, `generateContent` support and input/output capacity. Optional
`model_revision` binds catalog `version`. Responses must identify the exact configured
`modelVersion`; aliases resolving to a different name are rejected rather than silently mapped.
Returned thought parts are withheld. Non-text/tool parts, prompt blocks, unsupported finish
reasons, missing answers and malformed envelopes fail. A truncated answer still goes through
the normal quality validators and is not automatically accepted.

Text and [structured JSON](https://ai.google.dev/gemini-api/docs/structured-output) are supported.
Generation sends one candidate, a bounded output budget and no tools, grounding, caching,
batch/priority selection or file uploads. The shared transport retains bounded response size,
timeouts, no redirects/environment proxies, redacted diagnostics and cancellation cleanup.
HTTP 429 uses the existing cooldown/failover path and bounded Retry-After handling; it does
not invent remaining token quotas or daily resets. Gemini token usage metadata is not yet
mapped into a token-quota governor.

FAIR requests accept `max_output_tokens` (integer, 1–65,536; default 1,024). For example:

```json
{"client_id": "my-client", "task": "Explain this design in detail.", "max_output_tokens": 8192}
```

The Gemini server ceiling is configurable with `gemini_max_output_tokens` in
`live_adapters.yaml`, defaulting to 65,536. The adapter also checks the exact model's reported
`outputTokenLimit` before generation. Input capacity is checked separately from this output
allowance. Other adapters retain their existing limits. This is a maximum output budget;
it does not increase the account's free quota or guarantee that many tokens will be returned.
Existing response-size and timeout limits still apply. The Python SDK accepts
`client.solve(task, max_output_tokens=8192)`; JavaScript solve options accept the same field.

Gemini [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) are per project/model,
not per API key. Actual RPM, TPM and RPD must be reviewed in AI Studio. Large model context
capacity does not establish the free allowance or a global provider ranking. Eligibility,
task quality and verified remaining quotas continue to govern routing; no fixed Gemini
priority or fabricated allowance was added.

The saved key successfully fetched live metadata for `gemini-2.5-flash-lite` and
`gemini-2.5-flash`: both reported version `001`, 1,048,576 input tokens and 65,536 output
tokens of model capacity. This is metadata/authentication evidence, not live inference or
account-tier verification. The native adapter was tested through the full router with an
offline transport. Free-tier confirmation and real inference qualification remain pending.

For a privately reviewed Gemini configuration, the smoke command now accepts
`--provider google_gemini_api --model <exact-reviewed-model>`. The application and smoke CLI
do not auto-load `.env`. The smoke CLI can read the saved keys explicitly:

```powershell
.\.venv\Scripts\python.exe -m fair.providers.smoke --config-directory secrets/live-config --provider google_gemini_api --model <exact-reviewed-model> --env-file "C:\FAIR Free AI Router\.env"
```

`--env-file` reads only the selected provider's credential into an isolated snapshot. It does
not modify environment variables, read application settings from the file, or fall back to a
different key from the shell. Omitting the flag retains environment-based credential loading.
Use UTF-8 `NAME=value` lines, optionally single/double quoted, blank lines and `#` comments.
Values are literal: no shell commands, variable expansion, escape decoding or multiline values.
Duplicate names, malformed lines and files larger than 64 KiB fail with redacted diagnostics.
Blank/missing selected keys fail as unavailable. Saved keys do not grant account/model approval;
the same reviewed configuration and admission checks apply.

### Earlier adapter evidence

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
