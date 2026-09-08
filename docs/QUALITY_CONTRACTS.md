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

Supported features: integer/boolean parameters and constants; local assignments; `if`/`else`;
`return`; `+ - * // %`; unary signs and `not`; comparisons; `and`/`or`; conditional expressions.
Imports, calls, attributes, loops, recursion, decorators, annotations, default arguments,
collections, strings and other statements are outside the contract and hard-rejected, including
when they occur in unused branches. Incorrect outputs, missing returns and division by zero fail.

Limits: 8,192 source characters, 128 AST nodes, eight parameters, 32 nonempty host test cases,
256-bit integer values, 256 interpretation steps per test, and depth 20. Inputs and expected
outputs must be strict integers or booleans; `True` does not match expected integer `1`.
Only all-tests-passing functions may be accepted. Test coverage remains the host's responsibility.

Passing verification state: `BOUNDED_CODE_TESTS`. Passing these cases does not establish correctness
for untested inputs, native execution, external packages, filesystem/network access or arbitrary code.

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
Grounding is covered only for exact `grounded_json` extraction, and coding only for the
`python_function` subset. Other required capabilities are not covered. Source credibility
evaluation and native sandboxed code execution are not implemented. Schema-only and general
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
it is not a calibrated probability of truth. A known hard rejection scores 0. Missing validation
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

The engine version is `deterministic-v3`. Existing quality reports retain their original engine
version; new validation kinds have separate model/task statistics. Migration `0004` adds the
nullable model independence group; historical attempts default to the `PRIMARY` role when read.

Full Sprint B remains open for native sandboxed code validation, broader grounding/consistency
checks, source credibility evaluation and quality calibration. Live providers remain disabled.
