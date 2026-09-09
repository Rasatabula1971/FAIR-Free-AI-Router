# Deterministic quality contracts

The Sprint B engine accepts only answers covered by an explicit validation contract.
The authenticated host chooses that contract; the provider cannot choose its own acceptance
criteria. A contract defines the output being checked. Do not use an arithmetic contract to
claim that unrelated instructions or additional work in the task have been verified.

## Arithmetic

```json
{
  "client_id": "my-app",
  "task": "Calculate the expression",
  "validation": {"kind": "arithmetic", "expression": "0.1 + 0.2"}
}
```

The response must contain only an integer, decimal or fraction. `0.3` passes; `0.4` and
`0.3 plus an unsupported claim` fail. Expressions support decimal literals, parentheses,
unary signs and `+ - * /`. Parsing, tree depth, operation count and rational-number size
are bounded. No `eval`, imports, function calls, exponentiation or generated code is executed.
Fractional results are exact: `1/3` passes for `1 / 3`; a rounded decimal does not.

Passing verification state: `DETERMINISTIC_ARITHMETIC`.

## Host-reference JSON

```json
{
  "client_id": "my-app",
  "task": "Extract the answer 42 into a JSON object",
  "validation": {"kind": "reference_json", "expected": {"answer": 42}},
  "expected_schema": {"type": "object", "required": ["answer"]}
}
```

The complete JSON value must match the host reference. Object key order and whitespace may
differ. Extra fields, duplicate keys, non-finite numbers, mismatched types and different values
fail. Integer `42` and decimal `42.0` intentionally differ under this exact contract.
The reference is withheld from the normalized model request. The integrator remains responsible
for the reference's correctness. This mode is useful for known-answer checks and controlled
extraction/evaluation workloads, not open-ended factual verification.

Passing verification state: `HOST_REFERENCE_MATCH`.

## Extraction from supplied JSON sources

```json
{
  "client_id": "my-app",
  "task": "Extract the report population",
  "validation": {
    "kind": "grounded_json",
    "fields": [{"output_key": "population", "source_id": "report", "pointer": "/city/population"}]
  },
  "evidence": [{"source_id": "report", "text": "{\"city\":{\"population\":1000}}"}]
}
```

The model must return exactly:

```json
{
  "answer": {"population": 1000},
  "sources": {"population": {"source_id": "report", "pointer": "/city/population"}}
}
```

FAIR resolves each host-selected JSON pointer in the supplied source. Both the complete answer
and its source/path mapping must match. A correct value attributed to a different source,
an invented value, duplicate keys or an extra claim all fail. Pointers support object members,
array indices and `~0`/`~1` escapes, without retrieving remote content. Invalid source JSON,
missing paths/sources, duplicate requested output keys and oversized results are rejected
before inference. Sources remain untrusted data and are included in the model request as such.

Passing verification state: `SOURCE_DATA_MATCH`. This verifies exact extraction from the supplied
data, not source credibility, real-world truth, freshness or arbitrary natural-language entailment.

## Structured claims across sources

`grounded_claims` checks requested facts against **all** supplied fact documents, including
sources the model did not cite. It extends path extraction with explicit missing-evidence,
contradiction, conflict and attribution checks. It does not infer facts from natural-language prose.

```json
{
  "client_id": "my-app",
  "task": "Report the supplied population fact",
  "validation": {
    "kind": "grounded_claims",
    "claims": [{
      "claim_id": "population",
      "subject": "Example City",
      "predicate": "population",
      "context": "2025; residents"
    }]
  },
  "evidence": [{
    "source_id": "report",
    "text": "{\"facts\":[{\"subject\":\"Example City\",\"predicate\":\"population\",\"context\":\"2025; residents\",\"value\":1000}]}"
  }]
}
```

The supported response is:

```json
{"claims":[{
  "claim_id":"population",
  "status":"answered",
  "value":1000,
  "sources":[{"source_id":"report","pointer":"/facts/0"}]
}]}
```

The subject, predicate and context must match exactly, without case folding, synonym matching,
unit conversion or date inference. The host must make context explicit: different reporting years,
units or populations belong to different keys. Each key represents a single-valued fact; this
contract does not represent multi-valued relations. All facts with that key must have the same
strict JSON scalar value. `true`, `1` and `1.0` differ; `null` is a value, not an abstention.

Every matching fact must appear once in the answer's provenance. Omitting a matching source,
inventing a source, citing the wrong fact, duplicate references, unsupported values, missing
requested claims and extra claims/fields cause rejection. Duplicate response claim IDs, including
conflicting duplicate answers, are format failures. Claim and citation ordering may differ.
No free-form explanation or uncaptured claim can accompany an accepted response.

If another supplied document reports population `2000` for the same key, an unqualified answer
of either `1000` or `2000` is rejected as `CLAIM_OVER_CONFLICTING_EVIDENCE`. The model cannot
hide disagreement by omitting the other citation. An answer contrary to one consistent set of
facts is `CONTRADICTED_CLAIM`; answering a key with no matching facts is `UNSUPPORTED_CLAIM`.
These terms describe support within the supplied data, not independently established truth.

When evidence is absent or conflicts, the appropriate response is:

```json
{"claims":[{"claim_id":"population","status":"abstained"}]}
```

Abstention yields `UNVERIFIED` with a null score and a claim status of `INSUFFICIENT_EVIDENCE`,
`CONFLICTING_EVIDENCE`, or `ABSTAINED` when consistent evidence was available. It is not counted
as a measured model failure. Any abstention prevents final acceptance, even when other claims
are supported. If another claim is actually wrong, hard rejection still takes precedence.

Passing verification state: `STRUCTURED_CLAIMS_SUPPORTED`. `quality.claim_checks` records
requested claim IDs, statuses and matching evidence counts; raw source facts and rejected values
are excluded from those reports and audit records. Accepted output and its provenance remain
in client-owned request history. The existing `hallucination_events` metric additionally counts
attempts with unsupported, contradicted or asserted-over-conflict claims; it is an evidence
failure count, not a claim that the supplied evidence is true.

Limits: 20 unique requested keys/IDs, 10 sources, 50 facts per source, 200 total facts, source
text up to 10,000 characters, and scalar values up to 1,024 JSON-encoded characters. All source
documents must contain only a `facts` array; each fact contains only `subject`, `predicate`,
`context` and `value`. Malformed documents and budget violations fail request validation before
any provider call. An empty facts array or no evidence is allowed, enabling explicit abstention.

High-impact requests still require an independent eligible provider. Agreement ignores claim
and citation ordering but preserves values and types; both answers must pass the grounding
checks. Freshness, source credibility, prose entailment and unrelated task requirements remain
outside this contract. Annotating prose as structured facts is the host's responsibility; this
validator does not verify that annotation against the original prose.

## Bounded Python function tests

```json
{
  "client_id": "my-app",
  "task": "Implement absolute value",
  "validation": {
    "kind": "python_function",
    "function_name": "solve",
    "cases": [
      {"arguments": [-4], "expected": 4},
      {"arguments": [3], "expected": 3},
      {"arguments": [0], "expected": 0}
    ]
  }
}
```

The provider must return raw Python source containing exactly one function with the specified
name and parameter count. The host test cases are withheld from the model request. FAIR parses
the source and interprets an allowlisted AST subset itself; generated text is never passed to
Python `exec`, `eval`, a shell or a subprocess. This is a restricted interpreter, not a general
Python sandbox or evidence that code has run under native CPython.

Supported features: integer/boolean values and flat lists of them; local assignments;
`if`/`else`; `return`; numeric `+ - * // %`; unary signs and `not`; comparisons; `and`/`or`;
conditional expressions; `for` over a list or bounded `range`; and bounded `while` loops.
Both loop types support `break`, `continue` and `else` with Python control-flow semantics.
Lists support literals, concatenation and single-element indexing, including negative indices.
Augmented assignments such as `+=` and `*=` support scalar targets only, preserving list aliases.

Allowed built-ins are `abs(x)`, `len(xs)`, `sum(xs)`, `sorted(xs)`, and `min`/`max` on one nonempty
list or two to eight scalar arguments. `range` accepts one to three scalar arguments only as a
`for` iterable. Built-in names are reserved and cannot be function names, parameters or assignment
targets. Other calls, attributes, recursion, imports, decorators, annotations, default arguments,
strings, nested collections, comprehensions, slices and list mutation are rejected, including in
unused branches. Incorrect outputs, invalid indexing, missing returns and arithmetic errors fail.

Limits: 8,192 source characters, 256 AST nodes, eight parameters, 32 nonempty host test cases,
256-bit integer values, 64 elements per flat list, 4,096 interpretation steps per test, depth 20,
and at most 1,024 iterations per loop. Nested loops share the same test's step budget; a complex
loop may exhaust it before reaching the iteration limit. Total host test data is capped at 24,000
JSON characters. Inputs and expected outputs use strict types, including every list element;
`[True]` does not match expected `[1]`.
Only all-tests-passing functions may be accepted. Test coverage remains the host's responsibility.

For example, a list is one argument in this contract:

```json
{
  "kind": "python_function",
  "function_name": "solve",
  "cases": [
    {"arguments": [[3, -2, 5]], "expected": [6, 3]},
    {"arguments": [[]], "expected": [0, 0]}
  ]
}
```

The corresponding function is `def solve(xs): return [sum(xs), len(xs)]`. More involved admitted
examples include factorial with `for range`, Euclid's algorithm with `while`, and scans using
`break`/`continue`. See `tests/test_expanded_code.py` for the tested examples.

Passing verification state: `BOUNDED_CODE_TESTS`. Passing these cases does not establish correctness
for untested inputs, native execution, external packages, filesystem/network access or arbitrary code.

## Native Python function tests (opt-in)

Use the same function name and case fields with `"kind": "native_python_function"` to require
native CPython execution. Passing verification state: `NATIVE_CODE_TESTS`. The allowed language
and host admission limits are the bounded numeric/list subset above; this is not arbitrary Python support.
The bounded interpreter checks admission before Docker is contacted, and the trusted image
repeats that check before compiling and executing the function. Each case gets a fresh namespace
containing only the allowed built-ins. Host preflight also caps the aggregate returned values at
24,000 JSON characters and serialized stdin at 65,536 bytes before contacting Docker.

The executor creates a new container per candidate, with no network, host mounts, provider keys,
expected answers or inherited host environment. Only source, function name and test arguments
travel over stdin. The router compares returned values to the expectations with strict types.
Independent cross-check candidates get separate container executions. Native quality reports
record the immutable image ID in `validator_results.sandbox_image_id`.

Container restrictions: non-root UID 65534, read-only root filesystem, all Linux capabilities
dropped, no new privileges, Docker's default seccomp filter, 128 MiB memory with no additional
swap, half a CPU, 32 processes, a two-second CPU limit, zero file/core-dump limits, and 64 file
descriptors. Each Docker operation has a ten-second wall deadline and each output stream has
a 32 KiB limit. The trusted runner also limits address space and input size. These controls
provide container isolation, not a separate kernel or a production hostile-code security claim.
See [Docker runtime constraints](https://docs.docker.com/engine/containers/run/)
and [seccomp](https://docs.docker.com/engine/security/seccomp/).

Missing opt-in configuration yields `UNVERIFIED` with no invented quality score. Docker outages,
timeouts, malformed output and cleanup failures produce `VALIDATION_SERVICE_FAILED`, withhold
output and do not lower measured model quality. Candidate subset/test failures remain quality
failures. Cancellation attempts forced container removal and persists cancelled lineage; an
uncertain cleanup disables further native execution until cleanup is established. Repeated
cancellation cannot cancel the protected cleanup task. With a stable `FAIR_SANDBOX_OWNER`,
startup and pre-execution recovery remove expired containers belonging to that owner and preserve
unexpired or unrelated work. Without an owner, crash recovery remains manual. See
[sandbox recovery](SANDBOX_RECOVERY.md) for lease timing, the audited admin endpoint and limits.

The API factory enables this only when `FAIR_SANDBOX_IMAGE` holds a trusted, locally built image
ID matching `sha256:` plus 64 lowercase hexadecimal characters. Tags and automatic image pulls
are prohibited. The executor explicitly connects to the local Docker Unix socket on Linux or
Docker Engine named pipe on Windows, ignoring remote Docker contexts. It never mounts that
socket into generated-code containers. The default Compose API does not enable this feature
or receive Docker-daemon access. Use a host-run, single-worker router for this opt-in development
mode; Linux execution is tested in CI, and Windows Docker execution remains unverified.

Example preparation from the repository root (Docker must already be installed):

```powershell
docker build -f infra/sandbox/Dockerfile -t fair-native-sandbox .
$env:FAIR_SANDBOX_IMAGE = docker image inspect fair-native-sandbox --format '{{.Id}}'
$env:FAIR_SANDBOX_OWNER = 'fair-development'
# Configure the database/client/admin keys and migrate as described in README.md, then:
.\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Rebuild this repository's dedicated sandbox image after changing the validator; older images
may reject the expanded language. Do not substitute an unrelated image. Enabling
the executor does not add or activate any live provider, and demo fixtures are not coding models.

## Hard rejection and incomplete coverage

Schema failure, empty or incomplete response, arithmetic/reference mismatch, fabricated
citation references, unsupported quoted text and conflicting structured assertions override
an otherwise passing result. JSON schema references are disabled for both HTTP and direct
core callers; validation cannot trigger remote schema retrieval.

Adapters may supply structured `citations` containing `source_id` and `quote`. The request's
`evidence` contains matching IDs and source text. Unknown IDs and quotes absent from their
source fail. Quote matching does not prove that the source is true or that it supports an
arbitrary claim. No URL is fetched. Text outside the normalized citation metadata is not
automatically parsed for citations. Likewise, contradiction checks compare structured
subject/predicate/value assertions; they do not detect arbitrary prose contradictions.

Fresh/current tasks remain unverified even when a narrow contract check passes. High-impact
tasks require the independent cross-check described below before accepting a supported contract.
Grounding is covered for exact `grounded_json` extraction and the `grounded_claims` contract,
and coding only for the
`python_function` / `native_python_function` subset. Other required capabilities are not covered.
Operator-reviewed snapshot policies are available for both grounding contracts; see the
[source review guide](SOURCE_REVIEWS.md) for exact-content binding, client authorization,
source classes, age/expiry checks and per-claim corroboration. Passing a policy does not
establish source truth or live freshness. Automated credibility evaluation and arbitrary Python
execution are not implemented. Schema-only and general
prose responses remain unverified even if multiple models return identical text.

## Independent cross-checks

Add `"cross_check_required": true` to a supported request, or use `"quality_level":
"high_impact_support"`. High-impact requests cannot disable the requirement by setting the
boolean to false. FAIR first obtains a locally validated candidate, then asks another eligible
provider/model to solve the same task without seeing the candidate answer. Both roles use the
same host validation contract; reference answers and code test cases remain hidden from providers.

Provider IDs and model IDs must differ after whitespace/case normalization. Optional model
`independence_group` metadata prevents known aliases or related underlying routes from checking
each other. Groups default to model ID for comparison when absent. Operators must identify
known aliases correctly: distinct configured IDs do not prove different training data,
infrastructure or independent error patterns.

Each verification call passes the normal admission, privacy, capability, context, quota,
timeout and kill-switch controls. `max_attempts` bounds primary attempts (default 3), while
`max_verification_attempts` separately bounds cross-check calls (default 2, maximum 3). Thus
the default request can dispatch at most five model calls. Infrastructure/quota failures may
try another eligible checker within that budget. A measured rejection or disagreement stops
the request immediately; FAIR does not keep querying until it finds agreement.

Arithmetic results are compared as exact rational values. JSON results are compared by their
complete canonical values. Different Python implementations may agree when both pass all the
same host cases; this comparison says nothing about untested inputs. A matching answer with
failed citations, schema or another hard rejection cannot approve the candidate.

The response retains the specific validation scope, such as `SOURCE_DATA_MATCH`, and adds a
`cross_check` report with state, attempt references, count and agreement basis. Attempt roles
are `PRIMARY` or `CROSS_CHECK`. `model_disagreement` is `NONE`, `DETECTED` or `NOT_ASSESSED`.
`PASSED` means both scoped checks passed and their outputs agree under that comparison, not
that arbitrary claims are true. The returned answer belongs to the primary producer.

No eligible distinct checker produces `INDEPENDENT_VERIFIER_UNAVAILABLE`; differing comparable
answers produce `MODEL_DISAGREEMENT`; a malformed or rejected checker produces
`CROSS_CHECK_REJECTED`. These results withhold all answer text, including the provisional
candidate. Stop and service-failure paths also withhold it. Cross-check evidence is audited
and retained in client-isolated request history. Cancellation records the cancelled attempt.

## Results and learning

`ACCEPTED` includes output, provider/model, quality report, attempts and the explicit verification
state. An accepted deterministic score of 100 means all implemented contract checks passed;
it is not a calibrated probability of truth. Optional [benchmark qualification](BENCHMARKS.md)
also requires the model's conservative workload score to meet the selected level, while every
answer still passes its local contract and other runtime gates. A known hard rejection scores 0. Missing validation
coverage has a null score and an `UNVERIFIED` attempt disposition. This additional disposition
keeps unavailable validation separate from demonstrated model failure.

Retries exclude models already attempted on the current request and respect the configured
attempt limit. Escalation includes attempt evidence, best measured score and minimum required
score; rejected text is withheld. A validator service failure returns `FAILED` and its error
text is withheld. All result paths explicitly declare `paid_inference_executed: false`.

Quality reports, escalation details and model/task outcomes are persisted. Task-specific
selection uses measured averages damped by five neutral prior observations. Infrastructure
and quota failures never enter the quality numerator or denominator; availability is tracked
separately. The latest 20 measured quality scores are retained alongside lifetime totals.
Unsupported tasks do not receive invented scores. The admin performance endpoint reports
aggregates without raw prompts, evidence or answer text.
An attempt's `ACCEPTED` disposition and its performance counter mean that the local contract
checks passed. The final request may still escalate because its required cross-check failed.
Consequently an escalation can legitimately have a best local score of 100; that score never
overrides the independent-verification gate.

The engine version is `deterministic-v8`. Existing quality reports retain their original engine
version; new validation kinds have separate model/task statistics. Migration `0004` adds the
nullable model independence group; historical attempts default to the `PRIMARY` role when read.

Full Sprint B remains open for broader code execution support, prose grounding/consistency,
automated source credibility evaluation and empirical calibration on actual live workloads.
Operator-reviewed source policies and offline benchmark qualification are implemented; live
providers remain disabled. Migration `0005` adds the model revision used to bind qualification.
