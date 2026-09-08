import hmac
import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy import select

from fair.config import RoutingSettings, load_yaml
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest, SolveResponse
from fair.schemas.db import AuditEvent, database
from fair.schemas.db import TaskRequest as TaskRow
from fair.schemas.domain import ProviderSpec


def create_app(router=None, client_keys=None, admin_key=None):
    keys = (
        client_keys
        if client_keys is not None
        else json.loads(os.environ.get("FAIR_CLIENT_KEYS", "{}"))
    )
    admin = admin_key if admin_key is not None else os.environ.get("FAIR_ADMIN_KEY", "")

    @asynccontextmanager
    async def lifespan(app):
        engine = None
        if router is None:
            engine, sessions = database(
                os.environ.get(
                    "FAIR_DATABASE_URL", "postgresql+psycopg://fair:fair@localhost:5432/fair"
                )
            )
            registry = Registry()
            for entry in load_yaml("providers.yaml")["providers"]:
                registry.register(ProviderSpec.model_validate(entry))
            if os.environ.get("FAIR_DEMO_MODE") == "1":
                for name in ("mock_primary", "mock_secondary"):
                    registry.register(
                        ProviderSpec(
                            provider_id=name,
                            access_class="FREE_LOCAL",
                            status="ACTIVE",
                            current_access_cost_usd=0,
                            requires_paid_subscription=False,
                            requires_credit_purchase=False,
                            auto_billing_required=False,
                            programmatic_access=True,
                            production_eligibility=True,
                            models=[{"model_id": "offline-fixture", "context_window": 32768}],
                        ),
                        MockAdapter(name),
                    )
            app.state.router = Router(
                registry,
                RoutingSettings(**load_yaml("routing.yaml")),
                load_yaml("quality_thresholds.yaml"),
                sessions,
            )
        else:
            app.state.router = router
        yield
        if engine:
            engine.dispose()

    app = FastAPI(title="FAIR Free AI Router", version="0.1.0", lifespan=lifespan)

    def client(x_api_key: str = Header(default="")):
        for identity, key in keys.items():
            if key and hmac.compare_digest(x_api_key, key):
                return identity
        raise HTTPException(401, "Valid client API key required")

    def administrator(x_api_key: str = Header(default="")):
        if not admin or not hmac.compare_digest(x_api_key, admin):
            raise HTTPException(403, "Administrator API key required")
        return "admin"

    @app.get("/healthz")
    def health():
        return {"status": "ok", "milestone": "A", "live_inference_enabled": False}

    @app.post("/v1/solve", response_model=SolveResponse)
    async def solve(request: SolveRequest, identity=Depends(client)):
        if request.client_id != identity:
            raise HTTPException(403, "Client identity mismatch")
        if request.expected_schema is not None:
            try:
                Draft202012Validator.check_schema(request.expected_schema)
            except SchemaError:
                raise HTTPException(422, "Invalid expected JSON schema") from None
            # Do not let a supplied schema trigger retrieval of arbitrary URLs.
            if '"$ref"' in json.dumps(request.expected_schema) or '"$dynamicRef"' in json.dumps(
                request.expected_schema
            ):
                raise HTTPException(422, "Schema references are not supported in milestone A")
        return await app.state.router.solve(request)

    @app.get("/v1/providers")
    def providers(identity=Depends(client)):
        return [
            {
                "provider_id": p.provider_id,
                "status": p.status,
                "access_class": p.access_class,
                "models": [m.model_id for m in p.models],
            }
            for p in app.state.router.registry.providers.values()
        ]

    def owned(session, request_id, identity):
        row = session.get(TaskRow, request_id)
        if row is None or row.client_id != identity:
            raise HTTPException(404, "Request not found")
        return row

    @app.get("/v1/requests/{request_id}")
    def request_detail(request_id: str, identity=Depends(client)):
        with app.state.router.sessions() as session:
            row = owned(session, request_id, identity)
            return row.result_json or {"request_id": row.id, "status": row.status}

    @app.get("/v1/requests/{request_id}/audit")
    def request_audit(request_id: str, identity=Depends(client)):
        with app.state.router.sessions() as session:
            owned(session, request_id, identity)
            rows = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.request_id == request_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
            return [
                {"event_type": r.event_type, "payload": r.payload_json, "created_at": r.created_at}
                for r in rows
            ]

    def set_stop(stopped):
        active = app.state.router
        # Persist audit before acknowledging the change.
        with active.sessions.begin() as session:
            active.audit(session, None, "admin", "SYSTEM_STOP" if stopped else "SYSTEM_RESUME", {})
        active.stopped = stopped
        return {"stopped": stopped, "scope": "current_process"}

    @app.post("/v1/system/stop", dependencies=[Depends(administrator)])
    async def stop():
        return set_stop(True)

    @app.post("/v1/system/resume", dependencies=[Depends(administrator)])
    async def resume():
        return set_stop(False)

    return app


app = create_app()
