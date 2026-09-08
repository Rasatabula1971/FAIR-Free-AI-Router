# Deterministic quality contracts

The first Sprint B increment accepts only answers covered by an explicit validation contract.
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

Fresh/current, grounded research, high-impact and capability-dependent tasks such as coding
remain unverified even when a narrow contract check passes. Independent verification, source
evaluation and sandboxed code tests are not implemented. Schema-only and general prose
responses also remain unverified. `model_disagreement` is `NOT_ASSESSED`.

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

Full Sprint B remains open for sandboxed code validation, broader grounding/consistency
checks, independent model verification and quality calibration. Live providers remain disabled.
