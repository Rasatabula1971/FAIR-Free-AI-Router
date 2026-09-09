import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import select

from fair.config import RoutingSettings, config_dir, load_yaml
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.sandbox import DockerSandbox
from fair.quality.source_reviews import SourceReviewRegistry
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest, SolveResponse
from fair.schemas.db import AuditEvent, ModelTaskPerformance, database
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
                    registry.adapters[name].models = registry.providers[name].models
            review_path = Path(
                os.environ.get("FAIR_SOURCE_REVIEWS_FILE", config_dir() / "source_reviews.yaml")
            )
            source_reviews = (
                SourceReviewRegistry.from_file(review_path)
                if review_path.exists() or os.environ.get("FAIR_SOURCE_REVIEWS_FILE")
                else SourceReviewRegistry()
            )
            app.state.router = Router(
                registry,
                RoutingSettings(**load_yaml("routing.yaml")),
                load_yaml("quality_thresholds.yaml"),
                sessions,
                sandbox=DockerSandbox(os.environ["FAIR_SANDBOX_IMAGE"])
                if os.environ.get("FAIR_SANDBOX_IMAGE")
                else None,
                source_reviews=source_reviews,
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
        return {"status": "ok", "milestone": "B", "live_inference_enabled": False}

    @app.post("/v1/solve", response_model=SolveResponse)
    async def solve(request: SolveRequest, identity=Depends(client)):
        if request.client_id != identity:
            raise HTTPException(403, "Client identity mismatch")
        return await app.state.router.solve(request)

    @app.get("/v1/providers")
    def providers(identity=Depends(client)):
        return [
            {
                "provider_id": p.provider_id,
                "status": app.state.router.quota.effective_status(p),
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

    @app.get("/v1/models/performance", dependencies=[Depends(administrator)])
    def performance():
        with app.state.router.sessions() as session:
            rows = session.scalars(
                select(ModelTaskPerformance)
                .order_by(
                    ModelTaskPerformance.provider_id,
                    ModelTaskPerformance.model_id,
                    ModelTaskPerformance.task_class,
                )
                .limit(1000)
            )
            return [
                {
                    "provider_id": row.provider_id,
                    "model_id": row.model_id,
                    "task_class": row.task_class,
                    "attempts": row.attempts,
                    "accepted": row.accepted,
                    "quality_failures": row.quality_failures,
                    "infra_failures": row.infra_failures,
                    "quota_failures": row.quota_failures,
                    "unverified": row.unverified,
                    "quality_samples": row.quality_samples,
                    "average_quality": row.quality_sum / row.quality_samples
                    if row.quality_samples
                    else None,
                    "hallucination_events": row.hallucination_events,
                    "recent_quality": row.recent_quality,
                }
                for row in rows
            ]

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
        active.stopped = stopped
        return {"stopped": stopped, "scope": "database"}

    @app.get("/v1/providers/{provider_id}/health")
    def provider_health(provider_id: str, identity=Depends(client)):
        active = app.state.router
        spec = active.registry.providers.get(provider_id)
        if spec is None:
            raise HTTPException(404, "Provider not found")
        state = active.quota.state(provider_id)
        return {
            "provider_id": provider_id,
            "status": active.quota.effective_status(spec),
            "circuit_state": state.circuit_state,
            "requests_used": state.used,
            "request_limit": spec.request_limit,
            "reset_at": state.reset_at,
            "blocked_until": state.blocked_until,
            "source": "persisted_observations",
        }

    @app.post("/v1/system/stop", dependencies=[Depends(administrator)])
    async def stop():
        return set_stop(True)

    @app.post("/v1/system/resume", dependencies=[Depends(administrator)])
    async def resume():
        return set_stop(False)

    return app


app = create_app()
