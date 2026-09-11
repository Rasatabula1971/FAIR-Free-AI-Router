# Free provider qualification

Inspected 2026-09-10 against the operator-supplied `FAIR_TRUE_FREE_API_ONLY_v2.md`.
This records the corrected candidate scope and observed tests. It does not certify
provider pricing, account billing settings, privacy terms or production readiness.

## Next phase implemented: qualification evidence and admission

Typed provider/model evidence, expiry and fail-closed cloud admission are implemented,
with nullable persistence migration `0009`. See [the evidence and upgrade guide](PROVIDER_QUALIFICATION.md).
The original gap analysis below records the pre-implementation inspection; its schema,
billing-state representation and exact-model evidence gaps are addressed by this phase.
Account verification and live qualification are still outstanding. No real approvals were
invented and no providers were activated.

The Gemini adapter is now implemented with native text/JSON transport, exact-model checks,
scoped credentials and router smoke support. See [Gemini setup and evidence](LIVE_ADAPTERS.md).
Live model metadata checks pass. The next qualification task is confirming the Gemini/Groq
free account settings and recording bounded live inference evidence for the initial three
routes. No Gemini priority or quota figures have been invented from its model capacity.

The smoke CLI now accepts `--env-file` for locally saved keys. This addresses the credential
loading gap below without modifying process settings or requiring keys in chat. Duplicate and
malformed files fail closed, and only the selected provider's key reaches its adapter.
The saved `.env` parsed successfully with Groq, Gemini and OpenRouter credentials available;
this local check made zero provider calls. See [the smoke command](LIVE_ADAPTERS.md).
Local Ollama `llama3.2:3b` was then revalidated through the full router using `--env-file`:
`ACCEPTED`, deterministic arithmetic, one attempt, no paid inference. Current validation:
858 passed, 28 environment-dependent tests skipped. Gemini/Groq live qualification remains next.

Gemini phase validation: **827 tests passed, 28 environment-dependent checks skipped**;
the total includes 60 new Gemini regressions. Lint and formatting checks passed.

Validation: **767 tests passed, 28 environment-dependent tests skipped**, with two upstream
deprecation warnings. Lint and formatting checks passed. Migration upgrade/downgrade and
evidence persistence tests passed on disposable SQLite databases; no operational database
was migrated. The PostgreSQL/Docker checks still require their configured environments.

## Completed checklist step

- [x] Inspect the current provider implementation and governance controls.
- [x] Remove Ollama Cloud and Hugging Face routed credits from the default candidate pool.
- [x] Add Z.AI as an inactive candidate for explicitly zero-priced models.
- [x] Preserve inactive defaults and existing admission, quota and cancellation controls.
- [x] Record credential checks separately from inference and release qualification.
- [x] Identify missing implementation and the next bounded steps.

`config/providers.yaml` remains the runtime candidate list. The removed entries had
already been inactive; this corrects their classification, not an observed paid dispatch.
No adapter code was deleted. All eight retained candidates have no approved models and
remain ineligible for production. Local Ollama remains disabled in the shipped config.
Private deployment configs, if any, require their own review; editing the default file
does not change a running service or an alternate `FAIR_CONFIG_DIR`.

## Current evidence

Credentials were read only from the local `.env`. Private diagnostic results are in
the Git-ignored `secrets/` directory. No credential values belong in this report.

| Provider | Observation | Remaining qualification |
| --- | --- | --- |
| Groq | Authenticated model listing returned HTTP 200, 14 models | Free-plan confirmation; reviewed model; inference through FAIR; quota and billing evidence |
| Gemini | Native adapter implemented; authenticated model listing returned HTTP 200; exact Flash/Flash-Lite metadata checked | Free-tier confirmation; reviewed model; inference and quota evidence |
| Mistral | Authenticated model listing returned HTTP 200, 46 models | Free-mode confirmation; development/privacy restrictions; adapter; inference and quota evidence |
| Cloudflare | Account-scoped Workers AI catalog returned HTTP 200, 65 models | Workers Free confirmation; inference permissions; adapter; neuron accounting and free model review |
| OpenRouter | Key endpoint confirmed `is_free_tier: true` | Resolve catalog pricing compatibility without assuming missing prices are zero; reviewed explicit free model; FAIR smoke test |
| Kilo | Direct `liquid/lfm-2.5-2.6b:free` arithmetic test passed; response reported `usage.cost: 0` | FAIR adapter; actual upstream provenance; terms/privacy and quota qualification |
| Z.AI | Direct `glm-4.7-flash` request timed out | Inconclusive key/inference result; controlled follow-up; adapter; reviewed exact model allowlist |
| Ollama Local | Direct `llama3.2:3b` arithmetic test passed; earlier full-router smoke evidence is in LIVE_ADAPTERS.md | Revalidate full-router behavior alongside the cloud routes; deployment controls |

Cloudflare's user-token verification endpoint rejected the saved token, while the
account-scoped Workers AI endpoint accepted it. The former must not be used alone to
declare this credential invalid. Catalog access does not prove inference permissions.

OpenRouter's live catalog supplied zero prompt/completion prices but omitted the
`request` price required by FAIR. The existing adapter correctly denies this uncertainty.
Do not insert a fabricated zero or remove the regression that rejects missing prices.

These direct checks did not enable production routes. Kilo's single response is evidence
of one zero-cost result, not an independent production route or broad quality benchmark.
No account dashboard or invoice review has established zero charges across all providers.

## Gap analysis

| Area | Existing implementation | Remaining work |
| --- | --- | --- |
| 1. Adapters | `fair/providers/base.py` defines health, quota, discovery and completion. `live.py` shares bounded HTTP transport between Groq/OpenRouter and includes local Ollama. | Gemini, Mistral, Cloudflare, Kilo and Z.AI have no registered production adapter. The private diagnostic scripts are not adapters. Extend existing abstractions rather than duplicating the router. |
| 2. Free-status assumptions | `ProviderSpec` has FREE_RECURRING, FREE_DYNAMIC and FREE_LOCAL access classes, unknown cost by default, inactive state and false production eligibility. | Represent verified plan/model status separately from a candidate class; include development-only, conflicting, trial, promotional, paid and unknown outcomes. |
| 3. Incorrect classifications | Ollama Cloud and Hugging Face were recurring-free candidates; neither was active. | Removed from the default pool in this change. Keep SambaNova on hold and NVIDIA development-only outside this pool. The other rejected providers are absent. |
| 4. Credentials | `ProviderCredentials` resolves one scoped environment secret; guarded adapters suppress credential-bearing errors. Groq/OpenRouter bindings exist. The app does not auto-load `.env`. | Add explicit local env-file loading for qualification commands without changing global process settings, rejecting malformed/duplicate bindings. Add scoped bindings for new adapters and Cloudflare account ID support. |
| 5. Hard-free admission | `admit_provider` rejects unknown/nonzero cost, paid requirements, inactive status and absent eligibility. Registration, selection and execution recheck it. OpenRouter constrains routing prices and stops on missing/nonzero reported cost. | Add verified evidence and expiry for each route/model; distinguish an approved free candidate from an independently qualified account. Extend cost checks where provider APIs support them. |
| 6. Model allowlists | Existing adapters intersect configured exact models with live catalogs. OpenRouter requires explicit `owner/model:free`; auto routers are rejected. Ollama verifies local weights and remote markers. | Add provider-specific allowlists and paid-tool exclusions, especially Z.AI Flash versus FlashX. Kilo auto routing requires a deliberate provenance/allowlist design before support. |
| 7. Billing safeguards | Paid-subscription, credit-purchase and auto-billing requirements fail admission. Groq uses an operator free-plan attestation; OpenRouter also reads key tier. | Record actual payment-method/billing state, evidence origin and review time. A model catalog or valid key alone cannot establish these. Confirm free plans before plan-dependent inference. |
| 8. Quotas | Conservative request reservations, bounded retries, durable cooldown/exhaustion state, trusted reset observations, Retry-After parsing and recovery are implemented. Groq parses request headers. | Per-model RPM/RPD/TPM/TPD, monthly-token allowances, Cloudflare neurons and verified account limits are missing. `QuotaSnapshot` currently models requests only. Do not infer resets or exhaust live accounts indiscriminately. |
| 9. Independence | `ModelDescriptor.independence_group`, persisted group snapshots and cross-check alias/provider exclusions already exist. | Capture actual upstream provider/model for gateways, bind observations to requests and refuse to count shared upstream capacity as independent. Current normalized responses expose configured provider/model only. |
| 10. Tests | Admission, adapter transport, billing violations, secret isolation, quota/recovery and cross-check tests already cover the implemented controls. | Add regressions with each new schema/adapter change, then bounded live tests tied to private reviewed configs. Authentication tests alone cannot satisfy the three-route gate. |

## Credential names

| Candidate | Environment fields |
| --- | --- |
| Groq | `GROQ_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| Cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Kilo | `KILO_API_KEY` |
| Z.AI | `ZAI_API_KEY` |
| Local Ollama | `OLLAMA_HOST` in the local credential file; the existing adapter actually uses `live_adapters.yaml` / `ollama_url` |
| NVIDIA, optional development only | `NVIDIA_API_KEY` |

The Ollama adapter requires a literal loopback IP, for example `http://127.0.0.1:11434`;
it does not accept a `localhost` hostname as its configured `ollama_url`.

## Existing regression coverage to preserve

- `tests/test_core.py`: unsafe admission, execution-boundary checks, failover,
  bounded retries, request quotas, client isolation and stop behavior.
- `tests/test_live_adapters.py`: exact allowlists, unknown/nonzero pricing, non-free
  accounts, billing violations, malformed responses, redirection, local-only models,
  cancellation cleanup and isolated router smoke tests.
- `tests/test_security.py`: scoped credentials, secret file handling and redacted errors.
- `tests/test_durable_governance.py` and `tests/test_recovery.py`: durable quota and
  security blocks, trusted resets and audited recovery.
- `tests/test_cross_check.py`: aliases, independence groups, verification limits,
  cancellation and ownership isolation.
- `tests/test_operations.py`: configuration/readiness and non-inferencing preflight.

## Smallest implementation sequence

1. **Account evidence and live diagnostics.** Obtain the outstanding free-plan confirmations
   for Groq, Gemini, Mistral and Cloudflare; no key values need to be shared. Record exact
   model/account limits. Preserve the OpenRouter pricing block while checking authoritative
   endpoint metadata for the missing price. Diagnose Z.AI with a bounded explicit retry.
2. **Qualification schema and admission.** Add typed per-provider/model evidence, billing
   state, review timestamps and eligibility categories. Plan persistence/migration changes
   together: registry snapshots currently map `ProviderSpec` directly onto ORM columns.
   Test unknown/expired/conflicting/development-only evidence and state preservation.
3. **First additional direct adapter.** Implement Gemini using the existing credential,
   transport, error and governance boundaries. Test missing/invalid keys, model rejection,
   structured output, quota responses, timeout and cancellation with offline transports.
4. **Prove the initial three routes.** Run isolated full-router smoke tests for local
   Ollama, Groq and Gemini after each route's prerequisites are satisfied. Record actual
   model/provider, answer validation, bounded usage and zero-charge evidence separately.
5. **Expand only after the above works.** Add Mistral, Cloudflare, Kilo and Z.AI adapters,
   provider-specific quotas and gateway provenance. Verify controlled failover, stop and
   cancellation; then begin workload benchmarking and deployment qualification.

The immediate completed step is candidate correction and gap analysis. The free-provider
qualification milestone and FAIR v1 release gates remain incomplete.

## Validation of this change

The seven regression modules listed above passed: **218 tests**, with two upstream
deprecation warnings. A separate configuration check parsed all eight candidate specs,
confirmed admission denied each one and found zero registered live adapters. The default
pool contains neither removed credit-based provider. `git diff --check` passed. No live
inference, production database writes or provider activation occurred during this step.
