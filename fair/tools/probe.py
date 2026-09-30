"""Measure what a provider accepts, instead of writing down what it probably accepts.

Several values in this package can only be known by asking the provider: the output
ceiling a free endpoint really enforces, whether a schema survives its
structured-output parser, whether a cost observation arrives on a stream. Each was
either guessed from documentation or left at a conservative default, and a guess
that is wrong shows up as an opaque HTTP 400 in production rather than here.

The probe uses the registered adapters, so every admission, zero-cost and
credential check applies exactly as it does in routing. It never calls solve(): the
quality gate would spend extra requests and confound the measurement.

Requests are the scarce resource -- OpenRouter Free allows 50 a day -- so the
catalog pass costs none, every other pass costs one request per model, and the run
stops at --max-requests whatever is left to probe. Start with a dry run:

    python -m fair.tools.probe --dry-run --all
    python -m fair.tools.probe                              # catalog only, free
    python -m fair.tools.probe --limits --output-tokens 8192
    python -m fair.tools.probe --schema request.json --streaming
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field

from fair.providers.live import MAX_OUTPUT_TOKENS
from fair.schemas.domain import NormalizedModelRequest

# Whether an endpoint accepts a budget is answered by the request not being refused,
# not by the model filling it. Asking for a long answer made an accepted 16384-token
# probe generate for as long as 16384 tokens actually take -- around seventeen minutes
# at the rate these routes run at -- to learn something the first byte already proved.
LIMITS_TASK = "Reply with exactly: OK"
# Rates need an answer long enough to time, and a budget small enough to wait for.
RATE_TASK = (
    "Write a numbered list of short, self-contained factual sentences about the water "
    "cycle. Number every line."
)
RATE_TASK_TOKENS = 512
DEFAULT_MAX_REQUESTS = 12
DEFAULT_ENV_FILE = ".env"
# The adapters return text, not usage, so a rate is reported in characters and in
# tokens estimated at the usual four characters each. It sizes a timeout; it is not
# an accounting figure.
CHARS_PER_TOKEN = 4


@dataclass
class Plan:
    catalog: bool = True
    limits: bool = False
    schema: dict | None = None
    streaming: bool = False
    output_tokens: int = 8192
    max_requests: int = DEFAULT_MAX_REQUESTS
    # One probe request is allowed this long. Routing budgets scale into minutes for
    # a large answer; a probe only needs to know whether the request was taken.
    max_seconds: float = 90.0
    dry_run: bool = False
    progress: bool = False

    def passes(self):
        return [name for name in ("limits", "schema", "streaming") if getattr(self, name)]


@dataclass
class Budget:
    """Requests are the scarce resource; nothing is sent once this reaches zero."""

    remaining: int
    spent: int = 0
    refused: list = field(default_factory=list)

    def take(self, label):
        if self.remaining <= 0:
            self.refused.append(label)
            return False
        self.remaining -= 1
        self.spent += 1
        return True

    def refund(self, outcome):
        """A request FAIR refused before dispatch spent no provider quota."""
        if outcome.get("refused_by") == "fair":
            self.remaining += 1
            self.spent -= 1


def _request(model_id, *, max_output_tokens, schema=None, task=RATE_TASK):
    return NormalizedModelRequest(
        task=task,
        model_id=model_id,
        request_id="probe",
        client_id="fair-probe",
        task_class="general",
        max_output_tokens=max_output_tokens,
        expected_json_schema=schema,
    )


def _diagnostic(adapter):
    """Why a provider refused, when diagnostics are on; the code alone otherwise."""
    try:
        record = adapter.safe_diagnostics()
    except Exception:
        return None
    error = record.get("last_provider_error") if isinstance(record, dict) else None
    return error.get("provider_message") if isinstance(error, dict) else None


# FAIR raises this before dispatch, so it is FAIR's own budget answering, not the
# provider's. It spends no quota and measures nothing about the endpoint.
LOCAL_REFUSALS = ("RequestNotSupported",)


async def _complete(adapter, request, plan=None):
    started = time.monotonic()
    seconds = plan.max_seconds if plan is not None else Plan.max_seconds
    try:
        response = await asyncio.wait_for(adapter.complete(request), timeout=seconds)
    except TimeoutError:
        return {
            "accepted": False,
            "error": "TimedOut",
            "code": f"NO_ANSWER_WITHIN_{int(seconds)}S",
            "refused_by": "probe",
            "provider_message": None,
            "seconds": round(time.monotonic() - started, 2),
        }
    except Exception as error:
        name = type(error).__name__
        return {
            "accepted": False,
            "error": name,
            "code": str(error),
            "refused_by": "fair" if name in LOCAL_REFUSALS else "provider",
            "provider_message": _diagnostic(adapter),
            "seconds": round(time.monotonic() - started, 2),
        }
    seconds = max(time.monotonic() - started, 1e-6)
    characters = len(response.text)
    tokens = characters / CHARS_PER_TOKEN
    return {
        "accepted": True,
        "finish_reason": response.finish_reason,
        "characters": characters,
        "estimated_tokens": round(tokens),
        "estimated_tokens_per_second": round(tokens / seconds, 1),
        "seconds": round(seconds, 2),
    }


def _progress(plan, message):
    """A probe pass can wait on a slow route; silence looks the same as a hang."""
    if plan.progress:
        print(message, file=sys.stderr, flush=True)


async def probe_model(adapter, model_id, plan, budget, descriptor=None):
    result: dict = {"model_id": model_id}
    if descriptor is not None:
        result["catalog"] = {
            "context_window": descriptor.context_window,
            "max_output_tokens": descriptor.max_output_tokens,
        }
    if plan.limits and budget.take(f"limits:{model_id}"):
        _progress(plan, f"  limits     {model_id} ...")
        result["limits"] = await _complete(
            adapter,
            _request(model_id, max_output_tokens=plan.output_tokens, task=LIMITS_TASK),
            plan,
        )
        result["limits"]["requested_tokens"] = plan.output_tokens
        budget.refund(result["limits"])
    if plan.schema is not None and budget.take(f"schema:{model_id}"):
        _progress(plan, f"  schema     {model_id} ...")
        result["schema"] = await _complete(
            adapter,
            _request(model_id, max_output_tokens=min(plan.output_tokens, 2048), schema=plan.schema),
            plan,
        )
        budget.refund(result["schema"])
    if plan.streaming and budget.take(f"streaming:{model_id}"):
        _progress(plan, f"  streaming  {model_id} ...")
        result["streaming"] = await _stream_probe(adapter, model_id, plan)
        budget.refund(result["streaming"])
    return result


async def _stream_probe(adapter, model_id, plan):
    """Does this provider still prove a zero cost when the answer is streamed?

    openrouter_free and kilo_free are buffered because that is unverified, and each
    fails closed without the proof. Turning streaming on for one request answers it:
    an accepted answer means the observation arrived, a BillingViolation means it did
    not. The flag is set on the adapter instance and restored, never on the class.
    """
    inner = getattr(adapter, "_adapter", adapter)
    before = inner.supports_streaming
    inner.supports_streaming = True
    try:
        outcome = await _complete(
            adapter, _request(model_id, max_output_tokens=RATE_TASK_TOKENS), plan
        )
    finally:
        inner.supports_streaming = before
    outcome["cost_observed_on_stream"] = (
        True
        if outcome["accepted"]
        else None
        if outcome.get("error") != "BillingViolation"
        else False
    )
    outcome["was_buffered_by_default"] = before is False
    return outcome


async def probe_provider(provider_id, adapter, model_ids, plan, budget):
    result: dict = {"provider_id": provider_id}
    descriptors = {}
    if plan.catalog:
        try:
            live = await adapter.list_models()
            descriptors = {model.model_id: model for model in live}
            result["catalog_models"] = sorted(descriptors)
        except Exception as error:
            result["catalog_error"] = {"error": type(error).__name__, "code": str(error)}
        drops = adapter.safe_diagnostics().get("catalog_drops") if plan.catalog else None
        if drops:
            result["catalog_drops"] = drops
    result["models"] = [
        await probe_model(adapter, model_id, plan, budget, descriptors.get(model_id))
        for model_id in model_ids
    ]
    return result


async def run(adapters, plan):
    """adapters: {provider_id: (adapter, [model_id, ...])}."""
    budget = Budget(remaining=plan.max_requests)
    providers = []
    for provider_id, (adapter, model_ids) in sorted(adapters.items()):
        _progress(plan, f"=== {provider_id} ===")
        providers.append(await probe_provider(provider_id, adapter, model_ids, plan, budget))
    return {
        "passes": plan.passes() or ["catalog"],
        "requests_spent": budget.spent,
        "requests_not_attempted": budget.refused,
        "providers": providers,
    }


def plan_cost(adapters, plan):
    """How many requests a run would send, before it sends any."""
    per_model = len(plan.passes())
    return sum(len(model_ids) for _, model_ids in adapters.values()) * per_model


def recommend(report):
    """Turn observations into what a reviewer could defend writing into a descriptor.

    An accepted budget is a floor, not a ceiling: it says the endpoint took that many
    tokens, not that it would refuse one more. A refusal is the upper bound. Reporting
    either as "the limit" is the overclaim that put an unconfirmed 262144 in the
    registry in the first place, so both are named for what they are, next to whatever
    the live catalog publishes.
    """
    output, rates, streamable = {}, [], {}
    for provider in report["providers"]:
        for model in provider["models"]:
            key = f"{provider['provider_id']}/{model['model_id']}"
            for name in ("limits", "schema", "streaming"):
                outcome = model.get(name)
                if outcome is not None and outcome["accepted"]:
                    rates.append(outcome["estimated_tokens_per_second"])
            limits = model.get("limits")
            if limits is not None:
                observed: dict = {}
                if limits["accepted"]:
                    observed["accepted_at_least"] = limits["requested_tokens"]
                else:
                    observed["refused_at"] = limits["requested_tokens"]
                    observed["refusal"] = limits["code"]
                    observed["refused_by"] = limits["refused_by"]
                catalog = model.get("catalog") or {}
                if catalog.get("max_output_tokens") is not None:
                    # The descriptor after live reconciliation. Where a provider
                    # publishes its own ceiling this already reflects it; where it
                    # publishes none, this is the reviewed value and nothing more.
                    observed["descriptor_says"] = catalog["max_output_tokens"]
                output[key] = observed
            stream = model.get("streaming")
            if stream is not None and stream.get("was_buffered_by_default"):
                streamable[key] = stream["cost_observed_on_stream"]
    advice = {}
    if output:
        advice["output_tokens"] = output
    if rates:
        # The slowest route decides whether one budget is long enough for all of them,
        # and a rate set at the slowest observation would leave that route no headroom.
        advice["slowest_estimated_tokens_per_second"] = min(rates)
        advice["suggested_output_tokens_per_second"] = max(1, round(min(rates) * 0.8))
    if streamable:
        advice["cost_observed_on_stream"] = streamable
    return advice


def summarise(report):
    lines = [f"passes: {', '.join(report['passes'])}   requests spent: {report['requests_spent']}"]
    for provider in report["providers"]:
        lines.append("")
        lines.append(f"=== {provider['provider_id']} ===")
        if "catalog_error" in provider:
            lines.append(f"  catalog unavailable: {provider['catalog_error']['code']}")
        for model_id, reason in (provider.get("catalog_drops") or {}).items():
            lines.append(f"  dropped  {model_id}: {reason}")
        for model in provider["models"]:
            lines.append(f"  {model['model_id']}")
            catalog = model.get("catalog")
            if catalog:
                lines.append(
                    f"    catalog    context {catalog['context_window']}, "
                    f"output {catalog['max_output_tokens']}"
                )
            for name in ("limits", "schema", "streaming"):
                outcome = model.get(name)
                if outcome is None:
                    continue
                if outcome["accepted"]:
                    lines.append(
                        f"    {name:<10} accepted, {outcome['estimated_tokens']} tokens in "
                        f"{outcome['seconds']}s "
                        f"({outcome['estimated_tokens_per_second']} tok/s, "
                        f"{outcome['finish_reason']})"
                    )
                else:
                    detail = outcome.get("provider_message") or outcome["code"]
                    lines.append(f"    {name:<10} REFUSED  {outcome['error']}: {detail}")
    if report["requests_not_attempted"]:
        lines.append("")
        lines.append(
            f"budget exhausted, not attempted: {', '.join(report['requests_not_attempted'])}"
        )
    return "\n".join(lines)


def _build_plan(args):
    schema = None
    if args.schema:
        from fair.tools.schema_compat import find_schema

        with open(args.schema, encoding="utf-8") as handle:
            schema = find_schema(json.load(handle), args.schema_key)
    return Plan(
        limits=args.limits or args.all,
        schema=schema,
        streaming=args.streaming or args.all,
        output_tokens=args.output_tokens,
        max_requests=args.max_requests,
        max_seconds=args.max_seconds,
        dry_run=args.dry_run,
        progress=not args.quiet,
    )


def _lift_descriptor_caps(fair, adapters):
    """Let the limits pass reach the endpoint's answer rather than the descriptor's.

    A reviewed cap of 4096 makes every larger request a local refusal, which is the
    value under test. None means "no static local cap", so the adapter's ceiling --
    raised for this run only -- decides, and the provider gets to answer. The process
    exits after the run, so nothing outlives it.
    """
    for provider_id in adapters:
        spec = fair._registry.providers[provider_id]
        for model in spec.models:
            model.max_output_tokens = None
        adapter = getattr(fair._registry.adapters[provider_id], "_adapter", None)
        if adapter is not None:
            adapter._model_cache = None


def _adapters(fair, only_providers, only_models):
    selected = {}
    for spec in fair._registry.providers.values():
        if only_providers and spec.provider_id not in only_providers:
            continue
        model_ids = [
            model.model_id
            for model in spec.models
            if model.active and (not only_models or model.model_id in only_models)
        ]
        if model_ids:
            selected[spec.provider_id] = (fair._registry.adapters[spec.provider_id], model_ids)
    return selected


def _confirmed(args, env_file):
    """The recurring-free accounts an operator has confirmed, as the console reads them.

    Without these only the runtime-zero-cost gateways come up, so a probe that did not
    read them silently measured a fraction of the fleet and said nothing about the
    rest. --confirm still overrides.
    """
    if args.confirm:
        return {name.strip() for name in args.confirm.split(",") if name.strip()}
    from fair.embedded.module import _CLOUD_PROVIDERS, _read_env_file

    values = dict(os.environ)
    if env_file and os.path.exists(env_file):
        values = _read_env_file(env_file) | values
    named = values.get("FAIR_CONFIRMED_FREE_PROVIDERS", "")
    return {name.strip() for name in named.split(",") if name.strip()} & set(_CLOUD_PROVIDERS)


async def _main(args):
    from fair import FAIR

    plan = _build_plan(args)
    kwargs: dict = {"cache_enabled": False, "provider_error_diagnostics": True}
    # A default .env that is not there is not an error; one asked for by name is.
    if args.env_file and os.path.exists(args.env_file):
        kwargs["env_file"] = args.env_file
    elif args.env_file and args.env_file != DEFAULT_ENV_FILE:
        print(f"No such env file: {args.env_file}", file=sys.stderr)
        return 2
    kwargs["confirmed_free_providers"] = _confirmed(args, args.env_file)
    # The limits pass has to be allowed past FAIR's own ceiling, or it measures that
    # constant instead of the endpoint and never sends a request at all.
    if plan.limits:
        kwargs["max_output_tokens_ceiling"] = max(plan.output_tokens, MAX_OUTPUT_TOKENS)
    fair = FAIR(**kwargs)
    try:
        adapters = _adapters(fair, set(args.providers or ()), set(args.models or ()))
        if fair.skipped:
            print("Not configured, so not probed:", file=sys.stderr)
            for provider_id, reason in sorted(fair.skipped.items()):
                print(f"  {provider_id}: {reason}", file=sys.stderr)
            print(file=sys.stderr)
        if not adapters:
            print("No configured provider matched.", file=sys.stderr)
            return 2
        if plan.limits:
            _lift_descriptor_caps(fair, adapters)
        cost = plan_cost(adapters, plan)
        if plan.dry_run:
            print(
                f"Would probe {sum(len(m) for _, m in adapters.values())} models across "
                f"{len(adapters)} providers."
            )
            print(f"Passes: {', '.join(plan.passes()) or 'catalog only'}")
            print(f"Completion requests: {cost} (capped at {plan.max_requests})")
            for provider_id, (_, model_ids) in sorted(adapters.items()):
                print(f"  {provider_id}: {', '.join(model_ids)}")
            return 0
        if cost > plan.max_requests:
            print(
                f"Plan needs {cost} requests but --max-requests is {plan.max_requests}; "
                f"{cost - plan.max_requests} would be skipped. Narrow with --providers/--models "
                f"or raise the cap.",
                file=sys.stderr,
            )
        report = await run(adapters, plan)
        report["recommendations"] = recommend(report)
        print(summarise(report))
        print()
        print(json.dumps(report["recommendations"], indent=2))
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print(f"\nFull report written to {args.out}")
        return 0
    finally:
        await fair.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limits", action="store_true", help="probe the output ceiling")
    parser.add_argument("--schema", help="JSON file whose schema to send")
    parser.add_argument("--schema-key", help="key the schema sits under in that file")
    parser.add_argument("--streaming", action="store_true", help="probe cost proof on a stream")
    parser.add_argument("--all", action="store_true", help="every pass")
    parser.add_argument("--output-tokens", type=int, default=8192)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--max-seconds", type=float, default=90.0, help="per request")
    parser.add_argument(
        "--providers", help="comma-separated provider ids", type=lambda v: v.split(",")
    )
    parser.add_argument("--models", help="comma-separated model ids", type=lambda v: v.split(","))
    parser.add_argument("--confirm", help="comma-separated confirmed_free_providers")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--out", default="fair-probe-report.json")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    parser.add_argument("--quiet", action="store_true", help="no per-request progress")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
