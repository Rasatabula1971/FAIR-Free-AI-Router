"""The probe spends real provider quota, so what it would send is testable offline."""

import json

import pytest

from fair.providers.base import BillingViolation, ProviderUnavailable, RequestNotSupported
from fair.schemas.domain import ModelDescriptor, NormalizedModelResponse
from fair.tools import probe


class _Adapter:
    """Stands in for a registered adapter: same three methods the probe uses."""

    supports_streaming = False

    def __init__(self, provider_id="p", text="x" * 400, error=None, models=None, drops=None):
        self.provider_id = provider_id
        self.text = text
        self.error = error
        self.calls = []
        self.streamed = []
        self._models = models
        self._drops = drops or {}
        self._adapter = self

    async def list_models(self):
        if self._models is None:
            return [ModelDescriptor(model_id="m", context_window=1000, max_output_tokens=4096)]
        return self._models

    def safe_diagnostics(self):
        record = {}
        if self._drops:
            record["catalog_drops"] = dict(self._drops)
        if self.error is not None:
            record["last_provider_error"] = {
                "status": "HTTP_400",
                "provider_message": "minLength is unsupported",
            }
        return record

    async def complete(self, request):
        self.calls.append(request)
        self.streamed.append(self.supports_streaming)
        if self.error is not None:
            raise self.error
        return NormalizedModelResponse(
            provider_id=self.provider_id,
            model_id=request.model_id,
            text=self.text,
            finish_reason="stop",
        )


def _adapters(adapter, models=("m",)):
    return {adapter.provider_id: (adapter, list(models))}


class TestBudget:
    def test_it_stops_at_the_cap(self):
        budget = probe.Budget(remaining=2)
        assert [budget.take(str(n)) for n in range(4)] == [True, True, False, False]
        assert budget.spent == 2
        assert budget.refused == ["2", "3"]

    async def test_no_request_is_sent_once_the_budget_is_gone(self):
        adapter = _Adapter()
        plan = probe.Plan(limits=True, streaming=True, max_requests=1)
        report = await probe.run(_adapters(adapter), plan)
        assert len(adapter.calls) == 1
        assert report["requests_spent"] == 1
        assert report["requests_not_attempted"] == ["streaming:m"]

    async def test_a_catalog_only_run_spends_nothing(self):
        adapter = _Adapter()
        report = await probe.run(_adapters(adapter), probe.Plan())
        assert adapter.calls == []
        assert report["requests_spent"] == 0
        assert report["passes"] == ["catalog"]


class TestPlanCost:
    def test_the_cost_is_known_before_anything_is_sent(self):
        adapters = {"a": (_Adapter("a"), ["m1", "m2"]), "b": (_Adapter("b"), ["m3"])}
        assert probe.plan_cost(adapters, probe.Plan()) == 0
        assert probe.plan_cost(adapters, probe.Plan(limits=True)) == 3
        assert probe.plan_cost(adapters, probe.Plan(limits=True, streaming=True)) == 6

    def test_every_pass_is_counted_and_none_is_silent(self):
        plan = probe.Plan(limits=True, schema={"type": "object"}, streaming=True)
        assert plan.passes() == ["limits", "schema", "streaming"]
        assert probe.Plan().passes() == []


class TestCatalogPass:
    async def test_the_reconciled_descriptor_is_reported(self):
        adapter = _Adapter(
            models=[ModelDescriptor(model_id="m", context_window=9000, max_output_tokens=2048)]
        )
        report = await probe.run(_adapters(adapter), probe.Plan())
        assert report["providers"][0]["models"][0]["catalog"] == {
            "context_window": 9000,
            "max_output_tokens": 2048,
        }

    async def test_a_dropped_model_and_its_reason_are_reported(self):
        adapter = _Adapter(models=[], drops={"m": "CATALOG_CONTEXT_BELOW_REVIEWED_262144"})
        report = await probe.run(_adapters(adapter), probe.Plan())
        assert report["providers"][0]["catalog_drops"] == {
            "m": "CATALOG_CONTEXT_BELOW_REVIEWED_262144"
        }

    async def test_an_unreachable_catalog_does_not_end_the_run(self):
        adapter = _Adapter(models=None)

        async def fail():
            raise ProviderUnavailable("PROVIDER_TRANSPORT_FAILED")

        adapter.list_models = fail
        report = await probe.run(_adapters(adapter), probe.Plan(limits=True))
        assert report["providers"][0]["catalog_error"]["code"] == "PROVIDER_TRANSPORT_FAILED"
        assert report["providers"][0]["models"][0]["limits"]["accepted"] is True


class TestLimitsPass:
    async def test_an_accepted_budget_is_recorded_with_a_rate(self):
        report = await probe.run(_adapters(_Adapter()), probe.Plan(limits=True, output_tokens=999))
        limits = report["providers"][0]["models"][0]["limits"]
        assert limits["accepted"] is True
        assert limits["requested_tokens"] == 999
        assert limits["estimated_tokens"] == 100
        assert limits["estimated_tokens_per_second"] > 0

    async def test_a_refusal_records_why_the_provider_said_no(self):
        adapter = _Adapter(error=RequestNotSupported("OUTPUT_BUDGET_INVALID"))
        report = await probe.run(_adapters(adapter), probe.Plan(limits=True))
        limits = report["providers"][0]["models"][0]["limits"]
        assert limits["accepted"] is False
        assert limits["code"] == "OUTPUT_BUDGET_INVALID"
        assert limits["provider_message"] == "minLength is unsupported"

    async def test_the_requested_budget_reaches_the_adapter(self):
        adapter = _Adapter()
        await probe.run(_adapters(adapter), probe.Plan(limits=True, output_tokens=5000))
        assert adapter.calls[0].max_output_tokens == 5000


class TestSchemaPass:
    SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}

    async def test_the_schema_is_sent(self):
        adapter = _Adapter()
        await probe.run(_adapters(adapter), probe.Plan(schema=self.SCHEMA))
        assert adapter.calls[0].expected_json_schema == self.SCHEMA

    async def test_a_schema_rejection_is_reported(self):
        adapter = _Adapter(error=ProviderUnavailable("HTTP_400"))
        report = await probe.run(_adapters(adapter), probe.Plan(schema=self.SCHEMA))
        outcome = report["providers"][0]["models"][0]["schema"]
        assert outcome["accepted"] is False
        assert outcome["provider_message"] == "minLength is unsupported"


class TestStreamingPass:
    async def test_streaming_is_turned_on_for_the_probe_and_restored(self):
        adapter = _Adapter()
        assert adapter.supports_streaming is False
        await probe.run(_adapters(adapter), probe.Plan(streaming=True))
        assert adapter.streamed == [True]
        assert adapter.supports_streaming is False

    async def test_the_flag_is_restored_even_when_the_provider_refuses(self):
        adapter = _Adapter(error=BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED"))
        await probe.run(_adapters(adapter), probe.Plan(streaming=True))
        assert adapter.supports_streaming is False

    async def test_an_accepted_stream_means_the_cost_proof_arrived(self):
        report = await probe.run(_adapters(_Adapter()), probe.Plan(streaming=True))
        assert report["providers"][0]["models"][0]["streaming"]["cost_observed_on_stream"] is True

    async def test_a_billing_violation_means_it_did_not(self):
        adapter = _Adapter(error=BillingViolation("ZERO_COST_OBSERVATION_NOT_CONFIRMED"))
        report = await probe.run(_adapters(adapter), probe.Plan(streaming=True))
        assert report["providers"][0]["models"][0]["streaming"]["cost_observed_on_stream"] is False

    async def test_an_unrelated_failure_answers_nothing_either_way(self):
        adapter = _Adapter(error=ProviderUnavailable("HTTP_503"))
        report = await probe.run(_adapters(adapter), probe.Plan(streaming=True))
        assert report["providers"][0]["models"][0]["streaming"]["cost_observed_on_stream"] is None


class TestRecommendations:
    async def _report(self, adapter, plan):
        report = await probe.run(_adapters(adapter), plan)
        return probe.recommend(report)

    async def test_an_accepted_budget_is_reported_as_a_floor_not_a_ceiling(self):
        """It says the endpoint took that many tokens, not that it would refuse one more."""
        advice = await self._report(_Adapter(), probe.Plan(limits=True, output_tokens=8192))
        assert advice["output_tokens"]["p/m"]["accepted_at_least"] == 8192
        assert "limit" not in json.dumps(advice["output_tokens"])

    async def test_a_refusal_is_reported_as_the_upper_bound_it_is(self):
        adapter = _Adapter(error=RequestNotSupported("OUTPUT_BUDGET_INVALID"))
        advice = await self._report(adapter, probe.Plan(limits=True, output_tokens=8192))
        observed = advice["output_tokens"]["p/m"]
        assert observed["refused_at"] == 8192
        assert observed["refusal"] == "OUTPUT_BUDGET_INVALID"
        assert "accepted_at_least" not in observed

    async def test_the_descriptor_sits_next_to_what_was_observed(self):
        adapter = _Adapter(
            models=[ModelDescriptor(model_id="m", context_window=9000, max_output_tokens=2048)]
        )
        advice = await self._report(adapter, probe.Plan(limits=True, output_tokens=8192))
        assert advice["output_tokens"]["p/m"]["descriptor_says"] == 2048

    async def test_the_suggested_rate_is_under_the_slowest_route(self):
        """A rate at the slowest observation would leave that route no headroom."""
        advice = await self._report(_Adapter(), probe.Plan(limits=True))
        assert (
            advice["suggested_output_tokens_per_second"]
            <= advice["slowest_estimated_tokens_per_second"]
        )

    async def test_streaming_advice_only_covers_providers_that_are_buffered_today(self):
        adapter = _Adapter()
        advice = await self._report(adapter, probe.Plan(streaming=True))
        assert advice["cost_observed_on_stream"] == {"p/m": True}
        adapter.supports_streaming = True
        assert "cost_observed_on_stream" not in await self._report(
            adapter, probe.Plan(streaming=True)
        )


class TestSummary:
    async def test_the_summary_names_each_outcome(self):
        report = await probe.run(_adapters(_Adapter()), probe.Plan(limits=True))
        text = probe.summarise(report)
        assert "=== p ===" in text
        assert "limits" in text and "accepted" in text

    async def test_a_refusal_shows_the_provider_message_not_just_the_code(self):
        adapter = _Adapter(error=ProviderUnavailable("HTTP_400"))
        report = await probe.run(_adapters(adapter), probe.Plan(limits=True))
        assert "minLength is unsupported" in probe.summarise(report)

    async def test_the_report_is_json_serialisable(self):
        report = await probe.run(_adapters(_Adapter()), probe.Plan(limits=True, streaming=True))
        report["recommendations"] = probe.recommend(report)
        assert json.loads(json.dumps(report))["requests_spent"] == 2


class TestNoSecretsLeave:
    async def test_the_report_carries_nothing_but_what_the_adapter_published(self):
        adapter = _Adapter(error=ProviderUnavailable("HTTP_401"))
        report = await probe.run(_adapters(adapter), probe.Plan(limits=True, streaming=True))
        assert "sk-" not in json.dumps(report)
        assert "Authorization" not in json.dumps(report)


class _Spec:
    def __init__(self, provider_id, model_ids):
        self.provider_id = provider_id
        self.models = [
            ModelDescriptor(model_id=model_id, context_window=1000) for model_id in model_ids
        ]


class _Registry:
    def __init__(self, adapters):
        self.providers = {p: _Spec(p, m) for p, (_, m) in adapters.items()}
        self.adapters = {p: a for p, (a, _) in adapters.items()}


class _FairStub:
    def __init__(self, adapters):
        self._registry = _Registry(adapters)

    async def close(self):
        self.closed = True


@pytest.fixture
def stub_fair(monkeypatch):
    """Replace the FAIR the CLI builds, so main() can be driven without credentials."""

    def install(adapters):
        import fair as fair_package

        stub = _FairStub(adapters)
        monkeypatch.setattr(fair_package, "FAIR", lambda **kwargs: stub, raising=True)
        return stub

    return install


class TestCommandLine:
    def test_a_dry_run_prints_the_cost_and_sends_nothing(self, stub_fair, capsys):
        adapter = _Adapter()
        stub_fair(_adapters(adapter, ["m1", "m2"]))
        assert probe.main(["--dry-run", "--all"]) == 0
        out = capsys.readouterr().out
        assert "Completion requests: 4" in out
        assert adapter.calls == []

    def test_a_missing_default_env_file_is_not_an_error(self, stub_fair, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        stub_fair(_adapters(_Adapter()))
        assert probe.main(["--dry-run"]) == 0

    def test_an_env_file_asked_for_by_name_must_exist(self, stub_fair, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        stub_fair(_adapters(_Adapter()))
        assert probe.main(["--env-file", "missing.env"]) == 2

    def test_a_run_writes_its_report(self, stub_fair, tmp_path, capsys):
        stub_fair(_adapters(_Adapter()))
        out = tmp_path / "report.json"
        assert probe.main(["--limits", "--out", str(out)]) == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["requests_spent"] == 1
        assert report["recommendations"]["output_tokens"]["p/m"]["accepted_at_least"] == 8192

    def test_scoping_narrows_what_is_probed(self, stub_fair, tmp_path):
        adapter = _Adapter()
        stub_fair(_adapters(adapter, ["m1", "m2"]))
        probe.main(["--limits", "--models", "m2", "--out", str(tmp_path / "r.json")])
        assert [call.model_id for call in adapter.calls] == ["m2"]

    def test_no_matching_provider_is_reported_rather_than_run(self, stub_fair, tmp_path):
        stub_fair(_adapters(_Adapter()))
        assert probe.main(["--providers", "absent", "--out", str(tmp_path / "r.json")]) == 2
