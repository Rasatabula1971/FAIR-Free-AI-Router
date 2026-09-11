# Provider qualification evidence

Cloud routes now require a typed `qualification` record in their private provider
configuration, in addition to the existing free-only admission and live-adapter checks.
An API key, `status: ACTIVE`, or zero `current_access_cost_usd` alone is insufficient.

## What admission checks

- The evidence identifies the configured provider and matches its access class:
  `FREE_RECURRING` requires `verified_free_plan`; `FREE_DYNAMIC` requires
  `verified_zero_price_model`. Development-only, conflicting, trial, promotional,
  credit-based, paid and unknown classifications cannot pass.
- Both the existing provider eligibility and evidence `production_allowed` must be true.
- `billing_enabled`, `payment_method_required` and `can_auto_bill` must explicitly be
  false. `payment_method_present` must be known. An existing payment method is not itself
  an automatic rejection if reviewed evidence establishes that this route cannot bill.
- Reviewer, billing, terms, privacy and account-limit evidence references must be present.
  Use opaque references to private reviewed records, never keys or raw account responses.
- Review timestamps must include a timezone, must not be in the future and must be at
  most 30 days old. Expiry is exclusive and cannot exceed 30 days after review.
- Every active configured model must have an exact model ID and revision match, zero
  input/output/request prices, a pricing reference and explicitly disabled paid tools.
  Missing request pricing stays unknown; it is never defaulted to zero.
- Each model must have a passed live-test observation and zero-charge confirmation,
  with references. Its live-test timestamp must be within the past 30 days and no later
  than the review that approves it. All prices use exact decimals.

These checks run through the common admission function at registration, selection and
execution. Live transports also recheck before making requests. Review expiry therefore
blocks a route without requiring a restart. New models or revisions need new evidence.
One unreviewed active model blocks that provider until it is reviewed or made inactive.

Local routes without a qualification record retain the existing behavior. The Ollama
adapter still requires explicit local-only confirmation, literal loopback, existing local
weights and its remote-metadata checks. If qualification is supplied for a local route,
it must pass the same evidence checks with `free_status: local_compute`.

## Preparing a private record

Do not create approval records from key presence or a model's name. Before adding a
production record, complete separately authorized, bounded qualification diagnostics with
verified free pricing/account settings; review the exact results and account billing.
The existing full-router smoke command uses production admission, so it now requires this
prior evidence too. It cannot bootstrap its own approval.

Add `qualification` beneath the relevant provider in the ignored private config. This
incomplete example deliberately cannot activate a cloud route:

```yaml
qualification:
  provider_id: groq
  free_status: unknown
  production_allowed: false
  billing_enabled: null
  payment_method_required: null
  payment_method_present: null
  can_auto_bill: null
  reviewed_at: null
  expires_at: null
  reviewer_reference: null
  billing_reference: null
  terms_reference: null
  privacy_reference: null
  limits_reference: null
  models: []
```

Each model review supports `model_id`, optional `model_revision`,
`input_price_per_million`, `output_price_per_million`, `request_price`,
`pricing_reference`, `paid_tools_enabled`, `live_test_passed`, `live_test_at`,
`live_test_reference`, `zero_charge_verified` and `zero_charge_reference`.
Use quoted decimal strings for prices. Unknown values and missing observations fail closed.
Account limit references record where limits were reviewed; this phase does not implement
new token/neuron counters or change the existing request-quota governor.

The records are operator assertions linked to evidence, not cryptographically authenticated
proof or automatic account verification. Configuration remains a trusted operator boundary.
Live catalogs, cost responses and transport checks still apply independently. In particular,
recording a zero request price cannot bypass OpenRouter's missing live catalog price check.

## Persistence and upgrades

Migration `0009` adds nullable JSON qualification snapshots to `providers`. Registry
reconciliation writes JSON-safe decimal/timestamp representations without resetting quota
usage, security blocks, stop state or audit history. The public provider listing does not
return these private records.

Apply migrations through the normal maintenance/backup process before starting this code:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

This implementation task did not migrate an operational database or change running routes.
Readiness now expects schema `0009`. Legacy cloud rows receive no fabricated evidence;
their private configuration must be reviewed and updated before they can activate under
this version. Downgrading to `0008` removes the evidence column; re-upgrading leaves it
empty. Historical audit and quota state survive, but approval records must be reloaded
from the reviewed private configuration. Default candidates remain inactive.

This phase adds evidence representation, persistence and admission. Provider-specific
quota extensions, new adapters, gateway provenance, live free-plan confirmation and the
three-independent-route release gate remain separate work.
