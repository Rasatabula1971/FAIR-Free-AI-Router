# Operator-reviewed evidence

Source policies apply to `grounded_json` and `grounded_claims`. They require operator-reviewed
snapshots with matching content, authorized clients, acceptable source classes, bounded age,
and enough configured origins for each requested fact. They do not establish real-world truth,
fetch URLs, authenticate publishers, or verify that a snapshot is still current online.
The normal grounding checks still reject incorrect values, conflicting facts and bad attribution.

## Review and register a snapshot

1. Inspect the original source and the exact evidence text that the client will submit. Record
   the source location, observation time and review rationale. Assign `PRIMARY` or `SECONDARY`
   and an explicit `APPROVED` or `REJECTED` decision based on that review.
2. Assign related sources and aliases the same `origin_group`. Different IDs alone do not prove
   independent origins. FAIR also prevents identical text hashes from counting twice.
3. Hash the exact evidence string as UTF-8. Whitespace and trailing newlines matter; hashing a
   reserialized JSON document can produce a different digest from the submitted text.
4. Store the record in `config/source_reviews.local.yaml` or a private file outside the repository.
   The local filename is excluded from Git and Docker builds. The shipped `source_reviews.yaml`
   contains no approvals. Restrict write access to operators who may approve evidence.

This synthetic example reviews the exact text `{"city":{"population":1000}}`. The dates are
fixed so the example can be reproduced; do not copy its approval into a live review registry.

```yaml
reviews:
  - review_id: report-review-1
    client_ids: [my-app]
    content_sha256: ba9394080de964b1cc6c995caca92b7ada5cac2cd20c3ca8dce8edd97a9ec807
    origin_group: city-records
    source_class: PRIMARY
    decision: APPROVED
    reviewer_id: operator-one
    source_locator: internal://examples/city-record
    review_note: Synthetic example inspected for this demonstration only.
    observed_at: '2026-09-09T10:00:00Z'
    reviewed_at: '2026-09-09T11:00:00Z'
    expires_at: '2026-09-10T11:00:00Z'
```

All timestamps need timezones, with `observed_at <= reviewed_at < expires_at`. Review IDs must
be unique. An unknown review and a review belonging only to another client both return
`REVIEW_UNAVAILABLE`; the caller cannot discover another client's review through this status.

Validate the file without starting the API or calling a provider:

```powershell
.\.venv\Scripts\python.exe -c "from fair.quality.source_reviews import SourceReviewRegistry; SourceReviewRegistry.from_file('config/source_reviews.local.yaml'); print('Review file is valid')"
$env:FAIR_SOURCE_REVIEWS_FILE = 'C:\FAIR Free AI Router\config\source_reviews.local.yaml'
```

The API reads the registry at startup. Restart the API after adding, rejecting or renewing a
review; editing the file does not update a running registry. An explicitly configured missing
file, malformed record or duplicate review ID prevents startup. With no file configured or
available, the registry is empty and requests requiring reviews remain blocked.

For Docker, mount the private file read-only and set `FAIR_SOURCE_REVIEWS_FILE` inside the API
container to its mounted path. The development Compose file does not forward this variable or
mount the file automatically. Do not add private reviews to the image build context.

## Require a policy

Add this top-level field to a grounded request:

```json
{
  "source_policy": {
    "max_age_seconds": 86400,
    "allowed_source_classes": ["PRIMARY", "SECONDARY"],
    "min_independent_origins": 1
  }
}
```

An empty `"source_policy": {}` uses those defaults. To enforce requirements even when clients
omit the field, add a server policy to `config/routing.yaml` and restart:

```yaml
source_policy:
  max_age_seconds: 86400
  allowed_source_classes: [PRIMARY]
  min_independent_origins: 1
```

Client policies can only tighten a server policy: FAIR uses the shortest maximum age, the
largest minimum-origin count and the intersection of allowed source classes. Disjoint class
requirements block the request. Without either policy, the report is `NOT_REQUESTED` and
existing grounding behavior continues. Request-level policies on other validation kinds are
rejected; server source policies apply only to the two supported grounding contracts.

This complete request uses the synthetic review above:

```json
{
  "client_id": "my-app",
  "task": "Extract the report population",
  "validation": {
    "kind": "grounded_json",
    "fields": [{"output_key": "population", "source_id": "report", "pointer": "/city/population"}]
  },
  "evidence": [{
    "source_id": "report",
    "text": "{\"city\":{\"population\":1000}}",
    "review_id": "report-review-1"
  }],
  "source_policy": {}
}
```

Its policy passes at `2026-09-09T12:00:00Z` when the example registry is loaded. Its age and
expiry checks will block it later. Passing the policy does not guarantee an accepted model
answer. The existing demo adapters are offline fixtures, not general solvers.

For `grounded_claims`, set `min_independent_origins: 2` to require two reviewed origins that
contain each requested subject/predicate/context key. Unrelated facts do not corroborate a
claim. Every supplied source must pass review, including unused evidence; failed sources are
never silently dropped. Contradictions between reviewed sources still fail grounding.

Each `grounded_json` output field binds to one source, so it cannot satisfy an origin minimum
above one. Use `grounded_claims` for corroboration across sources.

## Reports and renewal

The top-level `source_policy` report contains its state, check time, per-source statuses,
per-target origin counts, reasons and a policy fingerprint. Attempt quality reports retain
their own check snapshots. Policy checks run before provider dispatch, after responses and
before final release, including independent verification. A review expiring during a request
withholds the answer. An earlier passing attempt remains historical evidence of that check.

| Source status | Meaning and operator action |
| --- | --- |
| `REVIEWED` | The snapshot met the effective policy at the recorded time. |
| `REVIEW_UNAVAILABLE` | Supply a known review authorized for the authenticated client. |
| `CONTENT_MISMATCH` | Review the changed text and register its exact hash. |
| `REJECTED` | Resolve the review concerns before an operator approves a replacement. |
| `FUTURE_REVIEW` | Check the review timestamps and server clock. |
| `EXPIRED` | Perform a new review and register its validity period. |
| `STALE` | Obtain and review a newer observation; extending expiry alone does not refresh it. |
| `SOURCE_CLASS_DISALLOWED` | Supply a source of a class allowed by the effective policy. |

A blocked policy returns `SOURCE_POLICY_UNSATISFIED` with no output. Pre-dispatch blocks consume
no inference quota. Missing reviews or expired evidence alone do not lower measured model
quality. Registry evaluation failures return `FAILED` / `VALIDATION_SERVICE_FAILED` and a
`SERVICE_FAILED` source report, without exposing internal error text.

`SOURCE_POLICY_CHECKED` audit events and client-isolated request history retain report statuses
and fingerprints, not review notes, source locators, content hashes or raw evidence. Keep the
private review records separately if operators need to reconstruct a historical fingerprint.
Review metadata is omitted from the provider's normalized prompt; evidence text is supplied
to the provider for grounding.

Source review fields were introduced in `deterministic-v6`; the current engine is
`deterministic-v7`. No database migration was needed for the source-review JSON report fields.
`freshness_required: true` remains unverified: a recently observed snapshot is not proof of
live freshness. Automated credibility scoring, retrieval and prose entailment remain unfinished.
