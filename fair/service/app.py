"""Authenticated HTTP service for the FAIR router."""

from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fair import FAIR
from fair.embedded.module import _read_env_file
from fair.providers.base import BillingViolation
from fair.schemas.domain import Capability, Priority, PrivacyClass
from fair.service.status_page import STATUS_HEADERS, STATUS_PAGE

SERVICE_MODEL_ID = "fair-router"


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FairSolvePayload(StrictPayload):
    task: str = Field(min_length=1, max_length=100_000)
    task_type: str | None = Field(default=None, max_length=64)
    quality_level: Literal["commodity", "standard", "advanced", "high_impact_support"] | None = None
    privacy_class: PrivacyClass = "PUBLIC"
    required_capabilities: set[Capability] = Field(default_factory=set)
    freshness_required: bool = False
    expected_schema: dict | None = None
    validation: dict | None = None
    evidence: list[dict] = Field(default_factory=list, max_length=10)
    source_policy: dict | None = None
    cross_check_required: bool | None = None
    max_output_tokens: int = Field(default=1024, ge=1, le=65536)
    priority: Priority = "P2"
    cache_mode: Literal["default", "bypass", "refresh"] = "default"


class ChatMessage(StrictPayload):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class FairChatOptions(StrictPayload):
    quality_level: Literal["commodity", "standard", "advanced", "high_impact_support"] | None = None
    privacy_class: PrivacyClass = "PUBLIC"
    required_capabilities: set[Capability] = Field(default_factory=set)
    freshness_required: bool = False
    validation: dict | None = None
    evidence: list[dict] = Field(default_factory=list, max_length=10)
    source_policy: dict | None = None
    cross_check_required: bool | None = None
    cache_mode: Literal["default", "bypass", "refresh"] = "default"


class ChatCompletionPayload(StrictPayload):
    model: str = SERVICE_MODEL_ID
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    stream: bool = False
    response_format: dict | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=65536)
    fair: FairChatOptions = Field(default_factory=FairChatOptions)


def parse_client_keys(raw: str | None) -> dict[str, str]:
    """Parse FAIR_SERVICE_CLIENTS without ever returning secrets in errors."""
    if not raw:
        raise ValueError("FAIR_SERVICE_CLIENTS is required")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("FAIR_SERVICE_CLIENTS must be a JSON object") from error
    if not isinstance(data, dict) or not data:
        raise ValueError("FAIR_SERVICE_CLIENTS must contain at least one client")
    clients: dict[str, str] = {}
    seen_secrets: set[str] = set()
    for client_id, secret in data.items():
        if (
            not isinstance(client_id, str)
            or not client_id.strip()
            or len(client_id.strip()) > 128
            or any(ord(character) < 32 for character in client_id)
        ):
            raise ValueError("FAIR_SERVICE_CLIENTS contains an invalid client id")
        if not isinstance(secret, str) or len(secret) < 16:
            raise ValueError("Every FAIR service client key must be at least 16 characters")
        if secret.startswith("replace-with"):
            raise ValueError("FAIR_SERVICE_CLIENTS still contains a placeholder client key")
        if secret in seen_secrets:
            raise ValueError("FAIR service client keys must be unique")
        seen_secrets.add(secret)
        clients[client_id.strip()] = secret
    return clients


def _parse_quota_pool_ids(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("FAIR_QUOTA_POOL_IDS must be a JSON object") from error
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and key and value
        for key, value in data.items()
    ):
        raise ValueError("FAIR_QUOTA_POOL_IDS must map provider ids to pool ids")
    return data


def _combined_env(env_file: str | None) -> dict[str, str]:
    file_values = _read_env_file(env_file) if env_file else {}
    return file_values | dict(os.environ)


def _client_resolver(client_keys: dict[str, str]):
    async def resolve_client(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer client key required")
        token = authorization[7:].encode("utf-8", "surrogateescape")
        matched = None
        for client_id, secret in client_keys.items():
            if hmac.compare_digest(token, secret.encode("utf-8")):
                matched = client_id
        if matched is None:
            raise HTTPException(status_code=401, detail="Invalid FAIR client key")
        return matched

    return resolve_client


def _admin_resolver(admin_key: str | None):
    async def resolve_admin(authorization: str | None = Header(default=None)) -> bool:
        if not admin_key:
            raise HTTPException(status_code=404, detail="Admin API disabled")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer admin key required")
        token = authorization[7:].encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(token, admin_key.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid FAIR admin key")
        return True

    return resolve_admin


def _chat_task(messages: list[ChatMessage]) -> str:
    return "\n\n".join(f"{message.role.upper()}: {message.content}" for message in messages)


def _expected_schema(response_format: dict | None) -> dict | None:
    if response_format is None:
        return None
    kind = response_format.get("type")
    if kind == "json_object":
        return {"type": "object"}
    if kind != "json_schema":
        raise HTTPException(status_code=400, detail="Unsupported response_format")
    descriptor = response_format.get("json_schema")
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("schema"), dict):
        raise HTTPException(status_code=400, detail="json_schema.schema is required")
    return descriptor["schema"]


def _service_error(status_code: int, error_type: str, result) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": result.reason_code,
                "type": error_type,
                "code": result.reason_code,
                "fair_request_id": result.request_id,
            }
        },
    )


def create_app(fair: FAIR, client_keys: dict[str, str], *, admin_key: str | None = None) -> FastAPI:
    client_keys = parse_client_keys(json.dumps(client_keys))
    if admin_key is not None and (len(admin_key) < 24 or admin_key.startswith("replace-with")):
        raise ValueError("FAIR_SERVICE_ADMIN_KEY must be a non-placeholder key of 24+ characters")
    if admin_key is not None and admin_key in client_keys.values():
        raise ValueError("FAIR_SERVICE_ADMIN_KEY must be distinct from every client key")
    resolve_client = _client_resolver(client_keys)
    resolve_admin = _admin_resolver(admin_key)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await fair.close()

    app = FastAPI(
        title="FAIR Free AI Router",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health():
        listed = await fair.providers_async()
        admissible = sum(provider["status"] != "REVIEW_EXPIRED" for provider in listed)
        routable = sum(provider["status"] in {"ACTIVE", "QUOTA_PRESSURE"} for provider in listed)
        payload = {
            "status": (
                "stopped"
                if fair.stopped
                else "no_admissible_providers"
                if not admissible
                else "no_routable_providers"
                if not routable
                else "ok"
            ),
            "providers": len(listed),
            "admissible_providers": admissible,
            "routable_providers": routable,
        }
        if fair.stopped or not routable:
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.get("/status", include_in_schema=False)
    async def status_page():
        # The page carries no data and asks for none without a client key, so
        # like /health it is served without one. Everything it shows comes from
        # the authenticated endpoints below.
        return HTMLResponse(STATUS_PAGE, headers=STATUS_HEADERS)

    @app.get("/v1/models")
    async def models(_client_id: str = Depends(resolve_client)):
        providers = await fair.providers_async()
        return {
            "object": "list",
            "data": [
                {
                    "id": SERVICE_MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": "fair",
                }
            ],
            "fair": {"providers": providers},
        }

    @app.get("/v1/fair/providers")
    async def providers(_client_id: str = Depends(resolve_client)):
        return {"providers": await fair.providers_async(), "skipped": fair.skipped}

    @app.get("/v1/fair/quota")
    async def quota(_client_id: str = Depends(resolve_client)):
        return await fair.quota_usage_async()

    @app.post("/v1/fair/admin/providers/{provider_id}/resume")
    async def resume_provider(provider_id: str, _admin: bool = Depends(resolve_admin)):
        try:
            return await fair.resume_provider_async(provider_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Unknown provider") from error

    @app.post("/v1/fair/solve")
    async def solve(payload: FairSolvePayload, client_id: str = Depends(resolve_client)):
        try:
            result = await fair.solve(
                payload.task,
                task_type=payload.task_type,
                quality_level=payload.quality_level,
                privacy_class=payload.privacy_class,
                required_capabilities=payload.required_capabilities,
                freshness_required=payload.freshness_required,
                expected_schema=payload.expected_schema,
                validation=payload.validation,
                evidence=payload.evidence,
                source_policy=payload.source_policy,
                cross_check_required=payload.cross_check_required,
                max_output_tokens=payload.max_output_tokens,
                client_id=client_id,
                priority=payload.priority,
                cache_mode=payload.cache_mode,
            )
        except ValidationError:
            return JSONResponse(
                status_code=422,
                content={"status": "FAILED", "reason_code": "INVALID_FAIR_REQUEST"},
            )
        except BillingViolation:
            return JSONResponse(
                status_code=503,
                content={"status": "FAILED", "reason_code": "PROVIDER_COST_POLICY_VIOLATION"},
            )
        return result.model_dump(mode="json")

    @app.post("/v1/chat/completions")
    async def chat(payload: ChatCompletionPayload, client_id: str = Depends(resolve_client)):
        if payload.model != SERVICE_MODEL_ID:
            raise HTTPException(status_code=400, detail=f"Use model '{SERVICE_MODEL_ID}'")
        if payload.stream:
            raise HTTPException(status_code=400, detail="Streaming is not supported yet")
        expected_schema = _expected_schema(payload.response_format)
        max_tokens = payload.max_completion_tokens or payload.max_tokens or 1024
        options = payload.fair
        try:
            result = await fair.solve(
                _chat_task(payload.messages),
                quality_level=options.quality_level,
                privacy_class=options.privacy_class,
                required_capabilities=options.required_capabilities,
                freshness_required=options.freshness_required,
                expected_schema=expected_schema,
                validation=options.validation,
                evidence=options.evidence,
                source_policy=options.source_policy,
                cross_check_required=options.cross_check_required,
                max_output_tokens=max_tokens,
                client_id=client_id,
                cache_mode=options.cache_mode,
            )
        except ValidationError:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "message": "INVALID_FAIR_REQUEST",
                        "type": "fair_invalid_request",
                        "code": "INVALID_FAIR_REQUEST",
                    }
                },
            )
        except BillingViolation:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "PROVIDER_COST_POLICY_VIOLATION",
                        "type": "fair_billing_violation",
                        "code": "PROVIDER_COST_POLICY_VIOLATION",
                    }
                },
            )
        if result.status == "ESCALATION_REQUIRED":
            return _service_error(422, "fair_escalation_required", result)
        if result.status != "ACCEPTED" or result.output is None:
            return _service_error(503, "fair_service_failure", result)
        return {
            "id": f"chatcmpl-{result.request_id}",
            "object": "chat.completion",
            "created": int(time()),
            "model": SERVICE_MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.output},
                    "finish_reason": "stop",
                }
            ],
            "usage": None,
            "fair": {
                "request_id": result.request_id,
                "provider_id": result.provider_id,
                "model_id": result.model_id,
                "verification_state": result.verification_state,
                "reason_code": result.reason_code,
            },
        }

    return app


def create_app_from_env() -> FastAPI:
    env_file = os.environ.get("FAIR_ENV_FILE")
    if env_file is None and Path(".env").is_file():
        env_file = ".env"
    env = _combined_env(env_file)
    clients = parse_client_keys(env.get("FAIR_SERVICE_CLIENTS"))
    admin_key = env.get("FAIR_SERVICE_ADMIN_KEY") or None
    confirmed = {
        item.strip()
        for item in env.get("FAIR_CONFIRMED_FREE_PROVIDERS", "").split(",")
        if item.strip()
    }
    quota_pool_ids = _parse_quota_pool_ids(env.get("FAIR_QUOTA_POOL_IDS"))
    fair = FAIR(
        env_file=env_file,
        confirmed_free_providers=confirmed,
        application_id=env.get("FAIR_APPLICATION_ID") or "fair-service",
        shared_quota_path=env.get("FAIR_SHARED_QUOTA_PATH"),
        quota_pool_ids=quota_pool_ids,
    )
    return create_app(fair, clients, admin_key=admin_key)
