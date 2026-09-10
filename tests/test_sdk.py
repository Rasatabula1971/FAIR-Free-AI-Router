import asyncio
import json

import httpx
import pytest
from conftest import provider

from apps.api.main import create_app
from fair.providers.mock import MockAdapter
from fair.schemas.api import SolveResponse
from fair.sdk import AsyncClient, Client, FAIRClientError


def result(**changes):
    return SolveResponse(
        **(
            {
                "request_id": "request-1",
                "status": "ESCALATION_REQUIRED",
                "reason_code": "QUALITY_VERIFICATION_UNAVAILABLE",
                "attempts": [],
                "minimum_required": 82,
            }
            | changes
        )
    ).model_dump(mode="json")


def client(handler, cls=Client, **changes):
    return cls(
        "http://127.0.0.1:8000",
        client_id="alice",
        api_key="private-client-key",
        transport=httpx.MockTransport(handler),
        **changes,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com?key=secret",
        "https://example.com#fragment",
        "ftp://example.com",
        "not a URL",
    ],
)
def test_invalid_endpoints_fail_without_network(url):
    with pytest.raises(FAIRClientError, match="INVALID_ENDPOINT"):
        Client(url, client_id="alice", api_key="key")


@pytest.mark.parametrize("status", [302, 401, 403, 429, 503])
def test_http_errors_have_status_without_secret_body_or_retries(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status,
            text="private-client-key leaked by upstream",
            headers={"location": "https://other.example"},
        )

    with client(handler) as sdk:
        with pytest.raises(FAIRClientError) as caught:
            sdk.solve("Hello")
    assert caught.value.status_code == status and str(caught.value) == "HTTP_ERROR"
    assert len(calls) == 1


def test_sync_payload_identity_and_response_contract():
    def handler(req):
        assert req.headers["X-API-Key"] == "private-client-key"
        body = json.loads(req.content)
        assert body["client_id"] == "alice" and body["task"] == "Hello"
        assert body["cache_mode"] == "refresh"
        assert "private-client-key" not in req.content.decode()
        return httpx.Response(200, json=result())

    with client(handler) as sdk:
        response = sdk.solve("Hello", cache_mode="refresh")
        assert (
            response.status == "ESCALATION_REQUIRED" and response.paid_inference_executed is False
        )
        with pytest.raises(FAIRClientError, match="INVALID_REQUEST"):
            sdk.solve("Hello", client_id="bob")
    assert sdk._http.is_closed


@pytest.mark.parametrize(
    "body",
    [b"{}", b'{"paid_inference_executed": true}', b'{"x": 1, "x": 2}', b"\xff", b" " * 2_000_001],
    ids=["empty", "paid", "duplicates", "utf8", "oversized"],
)
def test_invalid_or_oversized_solve_response(body):
    with client(lambda req: httpx.Response(200, content=body)) as sdk:
        with pytest.raises(FAIRClientError):
            sdk.solve("Hello")


def test_auxiliary_methods_use_expected_paths_and_no_path_injection():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    with client(handler) as sdk:
        sdk.providers()
        sdk.request("id")
        sdk.audit("id")
        sdk.feedback("id", accepted=True)
        sdk.request_feedback("id")
        sdk.clear_cache()
        with pytest.raises(FAIRClientError, match="INVALID_IDENTIFIER"):
            sdk.audit("../system/stop")
    assert seen == [
        ("GET", "/v1/providers"),
        ("GET", "/v1/requests/id"),
        ("GET", "/v1/requests/id/audit"),
        ("POST", "/v1/feedback"),
        ("GET", "/v1/requests/id/feedback"),
        ("DELETE", "/v1/cache"),
    ]


async def test_async_client_real_asgi_routing_cache_and_errors(make_router):
    mock = MockAdapter("a", "4")
    router = make_router([(provider(), mock)], cache_enabled=True)
    app = create_app(router, {"alice": "client-key"}, "admin-key")
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            "http://127.0.0.1",
            client_id="alice",
            api_key="client-key",
            transport=httpx.ASGITransport(app=app),
        ) as sdk:
            first = await sdk.solve(
                "2 + 2", validation={"kind": "arithmetic", "expression": "2 + 2"}
            )
            second = await sdk.solve(
                "2 + 2", validation={"kind": "arithmetic", "expression": "2 + 2"}
            )
            assert first.status == "ACCEPTED" and second.cache_hit and mock.calls == 1
            assert (await sdk.request(second.request_id))["cache_hit"]
            assert any(
                event["event_type"] == "CACHE_HIT" for event in await sdk.audit(second.request_id)
            )
            assert (await sdk.feedback(first.request_id, accepted=True))["accepted"] is True
            assert (await sdk.request_feedback(first.request_id))["accepted"] is True
            assert (await sdk.clear_cache())["entries_removed"] == 1
            with pytest.raises(FAIRClientError) as caught:
                await sdk.request("unknown")
            assert caught.value.status_code == 404
        assert sdk._http.is_closed


async def test_async_cancellation_closes_stream_and_never_retries():
    started, closed = asyncio.Event(), asyncio.Event()
    calls = []

    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            closed.set()

    def handler(req):
        calls.append(req)
        return httpx.Response(200, stream=Hanging())

    async with client(handler, AsyncClient) as sdk:
        task = asyncio.create_task(sdk.solve("Hello"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set() and len(calls) == 1


def test_timeout_is_normalized_without_raw_request():
    def handler(req):
        raise httpx.ReadTimeout("private-client-key")

    with client(handler) as sdk:
        with pytest.raises(FAIRClientError, match="^TRANSPORT_ERROR$"):
            sdk.solve("Hello")
