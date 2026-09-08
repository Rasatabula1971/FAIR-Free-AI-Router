import json

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from fair.providers.mock import MockAdapter
from fair.quality.grounding import resolve_pointer
from fair.schemas.api import SolveRequest


def request(**changes):
    return SolveRequest(
        **(
            {
                "client_id": "alice",
                "task": "Extract the report population",
                "validation": {
                    "kind": "grounded_json",
                    "fields": [
                        {
                            "output_key": "population",
                            "source_id": "report",
                            "pointer": "/city/population",
                        }
                    ],
                },
                "evidence": [{"source_id": "report", "text": '{"city":{"population":1000}}'}],
            }
            | changes
        )
    )


def model(name):
    return provider(
        name,
        models=[
            {"model_id": "model", "context_window": 32768, "capabilities": ["structured_output"]}
        ],
    )


def answer(value=1000, source="report", pointer="/city/population"):
    return json.dumps(
        {
            "answer": {"population": value},
            "sources": {"population": {"source_id": source, "pointer": pointer}},
        }
    )


async def test_grounded_answer_accepts_with_exact_source_paths(make_router):
    router = make_router([(model("a"), MockAdapter("a", text=answer()))])
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.verification_state == "SOURCE_DATA_MATCH"
    assert json.loads(result.output)["answer"]["population"] == 1000
    assert result.quality.validator_results["grounded_json"] == "PASS"


@pytest.mark.parametrize(
    "text",
    [
        answer(1001),
        answer(source="fabricated"),
        answer(pointer="/wrong"),
        '{"answer":{"population":1000}}',
        '{"answer":{"population":1000},"sources":{},"claim":"unproven"}',
        answer().replace('"population": 1000', '"population": 1000, "population": 2000'),
        answer(value=True),
        answer(value=1000.0),
    ],
)
async def test_wrong_claim_provenance_or_extra_content_rejected(make_router, text):
    router = make_router([(model("a"), MockAdapter("a", text=text))])
    result = await router.solve(request())
    assert result.status == "ESCALATION_REQUIRED" and result.output is None
    assert "GROUNDED_DATA_MISMATCH" in result.attempts[0].quality.reject_reasons


async def test_unsupported_claim_retries_different_model(make_router):
    router = make_router(
        [
            (model("a"), MockAdapter("a", text=answer(2000))),
            (model("b"), MockAdapter("b", text=answer())),
        ]
    )
    result = await router.solve(request())
    assert result.status == "ACCEPTED" and result.provider_id == "b"
    assert [a.disposition for a in result.attempts] == ["QUALITY_FAILURE", "ACCEPTED"]


@pytest.mark.parametrize(
    "changes",
    [
        {"freshness_required": True},
        {"quality_level": "high_impact_support"},
        {"required_capabilities": {"reasoning"}},
    ],
)
async def test_source_match_does_not_claim_freshness_or_independent_verification(
    make_router, changes
):
    router = make_router([(model("a"), MockAdapter("a", text=answer()))])
    result = await router.solve(request(**changes))
    assert result.status == "ESCALATION_REQUIRED" and result.verification_state == "UNVERIFIED"


@pytest.mark.parametrize(
    "data,pointer,expected",
    [
        ({"a/b": {"~": [10, 20]}}, "/a~1b/~0/1", 20),
        ({"": 7}, "/", 7),
        ({"nested": [None, True]}, "/nested/0", None),
        ({"x": 1}, "", {"x": 1}),
    ],
)
def test_json_pointer_escapes_lists_and_root(data, pointer, expected):
    assert resolve_pointer(data, pointer) == expected


@pytest.mark.parametrize(
    "pointer", ["not-a-pointer", "/bad~2", "/city/missing", "/city/population/x"]
)
def test_invalid_grounding_contract_fails_before_inference(pointer):
    with pytest.raises(ValidationError):
        request(
            validation={
                "kind": "grounded_json",
                "fields": [{"output_key": "population", "source_id": "report", "pointer": pointer}],
            }
        )


@pytest.mark.parametrize("text", ["not JSON", '{"a":1,"a":2}', '{"value":NaN}', '{"value":1e999}'])
def test_invalid_sources_rejected(text):
    with pytest.raises(ValidationError):
        request(evidence=[{"source_id": "report", "text": text}])


def test_missing_source_and_duplicate_fields_rejected():
    with pytest.raises(ValidationError):
        request(evidence=[])
    field = {"output_key": "x", "source_id": "report", "pointer": "/city"}
    with pytest.raises(ValidationError):
        request(validation={"kind": "grounded_json", "fields": [field, field]})


def test_http_bad_source_returns_422_and_no_provider_call(make_router):
    router = make_router([(model("a"), MockAdapter("a", text=answer()))])
    body = request().model_dump(mode="json")
    body["evidence"] = []
    with TestClient(create_app(router, {"alice": "key"}, "admin")) as client:
        assert client.post("/v1/solve", headers={"X-API-Key": "key"}, json=body).status_code == 422
    assert router.registry.adapters["a"].calls == 0
