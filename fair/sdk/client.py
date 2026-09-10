import ipaddress
import math
import re
from urllib.parse import urlsplit

import httpx

from fair.performance.feedback import FeedbackRequest
from fair.quality.json_data import strict_json
from fair.schemas.api import SolveRequest, SolveResponse


class FAIRClientError(Exception):
    def __init__(self, code, status_code=None):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def endpoint(value):
    try:
        url = urlsplit(value)
        try:
            local = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            local = url.hostname == "localhost"
        if (
            (url.scheme != "https" and not (url.scheme == "http" and local))
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(part in {".", ".."} for part in url.path.split("/"))
        ):
            raise ValueError()
        _ = url.port
    except (ValueError, TypeError):
        raise FAIRClientError("INVALID_ENDPOINT") from None
    return value.rstrip("/")


def segment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise FAIRClientError("INVALID_IDENTIFIER")
    return value


def decode(content, model):
    try:
        value = strict_json(content.decode("utf-8"))
        if not isinstance(value, (dict, list)):
            raise ValueError()
        result = model.model_validate(value) if model else value
        if model is SolveResponse and result.status == "ACCEPTED" and result.output is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, RecursionError):
        raise FAIRClientError("INVALID_RESPONSE") from None


class Methods:
    def solve(self, task, **options):
        try:
            if "client_id" in options:
                raise ValueError()
            payload = SolveRequest(client_id=self.client_id, task=task, **options).model_dump(
                mode="json"
            )
        except (ValueError, TypeError):
            raise FAIRClientError("INVALID_REQUEST") from None
        return self._request("POST", "/v1/solve", payload, SolveResponse)

    def providers(self):
        return self._request("GET", "/v1/providers")

    def request(self, request_id):
        return self._request("GET", "/v1/requests/" + segment(request_id))

    def audit(self, request_id):
        return self._request("GET", "/v1/requests/" + segment(request_id) + "/audit")

    def feedback(self, request_id, **values):
        try:
            body = FeedbackRequest(request_id=request_id, **values).model_dump(mode="json")
        except (ValueError, TypeError):
            raise FAIRClientError("INVALID_REQUEST") from None
        return self._request("POST", "/v1/feedback", body)

    def request_feedback(self, request_id):
        return self._request("GET", "/v1/requests/" + segment(request_id) + "/feedback")

    def clear_cache(self):
        return self._request("DELETE", "/v1/cache")

    def _configure(self, base_url, client_id, api_key, timeout):
        self.base_url = endpoint(base_url)
        if (
            not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 128
            or not isinstance(api_key, str)
            or not api_key
            or not re.fullmatch(r"[\x21-\x7E]+", api_key)
        ):
            raise FAIRClientError("INVALID_CREDENTIAL_CONFIGURATION")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise FAIRClientError("INVALID_TIMEOUT")
        self.client_id = client_id
        return {
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
            "headers": {
                "X-API-Key": api_key,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        }

    @staticmethod
    def _status(response):
        if not 200 <= response.status_code < 300:
            raise FAIRClientError("HTTP_ERROR", response.status_code)
        if response.headers.get("content-encoding", "identity") != "identity":
            raise FAIRClientError("INVALID_RESPONSE")


class Client(Methods):
    def __init__(self, base_url, *, client_id, api_key, timeout=180, transport=None):
        self._http = httpx.Client(
            **self._configure(base_url, client_id, api_key, timeout), transport=transport
        )

    def _request(self, method, path, payload=None, model=None):
        try:
            with self._http.stream(method, self.base_url + path, json=payload) as response:
                self._status(response)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise FAIRClientError("RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                return decode(b"".join(chunks), model)
        except httpx.HTTPError:
            raise FAIRClientError("TRANSPORT_ERROR") from None

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncClient(Methods):
    def __init__(self, base_url, *, client_id, api_key, timeout=180, transport=None):
        self._http = httpx.AsyncClient(
            **self._configure(base_url, client_id, api_key, timeout), transport=transport
        )

    async def _request(self, method, path, payload=None, model=None):
        try:
            async with self._http.stream(method, self.base_url + path, json=payload) as response:
                self._status(response)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise FAIRClientError("RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                return decode(b"".join(chunks), model)
        except httpx.HTTPError:
            raise FAIRClientError("TRANSPORT_ERROR") from None

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
