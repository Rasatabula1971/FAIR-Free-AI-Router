import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select

from fair.benchmarks.registry import BenchmarkRegistry
from fair.config import RoutingSettings, config_dir, load_yaml
from fair.governor.recovery import ProviderRecovery, Recovery, RecoveryDenied, RequestRecovery
from fair.operations.status import readiness, snapshot
from fair.performance.feedback import FeedbackDenied, FeedbackRegistry, FeedbackRequest
from fair.providers.base import BillingViolation
from fair.providers.live import LiveSettings, register_live
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.sandbox import DockerSandbox, SandboxUnavailable
from fair.quality.source_reviews import SourceReviewRegistry
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest, SolveResponse
from fair.schemas.db import AuditEvent, ModelTaskPerformance, database
from fair.schemas.db import TaskRequest as TaskRow
from fair.schemas.domain import ProviderSpec
from fair.security.credentials import APIKeys

logger = logging.getLogger(__name__)


def create_app(router=None, client_keys=None, admin_key=None):
    credentials = APIKeys.load(client_keys, admin_key)

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
            live_path = config_dir() / "live_adapters.yaml"
            live_settings = (
                LiveSettings(**load_yaml("live_adapters.yaml"))
                if live_path.exists()
                else LiveSettings()
            )
            register_live(
                registry,
                [
                    ProviderSpec.model_validate(entry)
                    for entry in load_yaml("providers.yaml")["providers"]
                ],
                live_settings,
            )
            if live_settings.enabled and os.environ.get("FAIR_DEMO_MODE") == "1":
                raise ValueError("Live adapters and demo mode cannot be combined")
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
            benchmark_path = os.environ.get("FAIR_BENCHMARKS_FILE")
            benchmarks = (
                BenchmarkRegistry.from_file(benchmark_path)
                if benchmark_path
                else BenchmarkRegistry()
            )
            app.state.router = Router(
                registry,
                RoutingSettings(**load_yaml("routing.yaml")),
                load_yaml("quality_thresholds.yaml"),
                sessions,
                sandbox=DockerSandbox(
                    os.environ["FAIR_SANDBOX_IMAGE"], owner=os.environ.get("FAIR_SANDBOX_OWNER")
                )
                if os.environ.get("FAIR_SANDBOX_IMAGE")
                else None,
                source_reviews=source_reviews,
                benchmarks=benchmarks,
            )
        else:
            app.state.router = router
        try:
            if app.state.router.sandbox is not None and app.state.router.sandbox.owner is not None:
                await app.state.router.sandbox.recover()
            yield
        finally:
            try:
                await app.state.router.close()
            finally:
                if engine:
                    engine.dispose()

    app = FastAPI(title="FAIR Free AI Router", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Framework validation errors otherwise echo input values, including secret extras.
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.exception_handler(BillingViolation)
    async def unexpected_cost(request, error):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "PROVIDER_COST_POLICY_VIOLATION",
                "system_stopped": True,
                "paid_inference_executed": None,
            },
        )

    @app.exception_handler(RecoveryDenied)
    @app.exception_handler(FeedbackDenied)
    async def recovery_denied(request, error):
        return JSONResponse(status_code=error.status, content={"detail": error.reason})

    def client(x_api_key: list[str] = Header(default=[])):
        identity = credentials.authenticate(x_api_key)
        if identity is not None:
            return identity
        raise HTTPException(401, "Valid client API key required")

    def administrator(x_api_key: list[str] = Header(default=[])):
        if credentials.authenticate(x_api_key, administrator=True) is None:
            raise HTTPException(403, "Administrator API key required")
        return "admin"

    @app.get("/healthz")
    def health():
        active = app.state.router
        return {
            "status": "ok",
            "milestone": "B",
            "live_inference_enabled": any(
                getattr(adapter, "live_inference", False)
                for adapter in active.registry.adapters.values()
            )
            and not active.stopped,
        }

    @app.get("/livez")
    def liveness():
        return {"status": "alive"}

    @app.get("/readyz")
    def ready():
        result = readiness(app.state.router, credentials)
        return JSONResponse(status_code=200 if result["status"] == "ready" else 503, content=result)

    @app.get("/v1/system/status", dependencies=[Depends(administrator)])
    def operational_status():
        try:
            return snapshot(app.state.router, credentials)
        except Exception:
            logger.exception("Operational status snapshot failed")
            return JSONResponse(
                status_code=503, content={"detail": "OPERATIONAL_STATUS_UNAVAILABLE"}
            )

    @app.post("/v1/solve", response_model=SolveResponse)
    async def solve(request: SolveRequest, connection: Request, identity=Depends(client)):
        if request.client_id != identity:
            raise HTTPException(403, "Client identity mismatch")

        async def disconnected():
            while (await connection.receive())["type"] != "http.disconnect":
                pass

        work = asyncio.create_task(app.state.router.solve(request))
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
            if work not in done:
                work.cancel()
            return await asyncio.shield(work)
        finally:
            watcher.cancel()
            if not work.done() and not work.cancelling():
                work.cancel()
            cleanup = asyncio.gather(work, watcher, return_exceptions=True)
            # Keep the scheduler slot until provider/native cancellation cleanup has finished.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue

    @app.get("/v1/providers")
    async def providers(identity=Depends(client)):
        results = []
        for p in app.state.router.registry.providers.values():
            results.append({
                "provider_id": p.provider_id,
                "status": await app.state.router.quota.effective_status(p),
                "access_class": p.access_class,
                "models": [m.model_id for m in p.models],
            })
        return results

    def owned(session, request_id, identity):
        row = session.get(TaskRow, request_id)
        if row is None or row.client_id != identity:
            raise HTTPException(404, "Request not found")
        return row

    @app.get("/v1/models/performance", dependencies=[Depends(administrator)])
    def performance(limit: int = 100, offset: int = 0):
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        with app.state.router.sessions() as session:
            rows = session.scalars(
                select(ModelTaskPerformance)
                .order_by(
                    ModelTaskPerformance.provider_id,
                    ModelTaskPerformance.model_id,
                    ModelTaskPerformance.task_class,
                )
                .limit(limit)
                .offset(offset)
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
                    **app.state.router.performance.describe(row),
                }
                for row in rows
            ]

    @app.get("/v1/requests/{request_id}")
    def request_detail(request_id: str, identity=Depends(client)):
        with app.state.router.sessions() as session:
            row = owned(session, request_id, identity)
            return row.result_json or {"request_id": row.id, "status": row.status}

    @app.post("/v1/feedback")
    async def feedback(request: FeedbackRequest, identity=Depends(client)):
        return FeedbackRegistry(app.state.router.sessions).submit(identity, request)

    @app.delete("/v1/cache")
    async def clear_cache(identity=Depends(client)):
        return await app.state.router.cache.clear(identity)

    @app.get("/v1/requests/{request_id}/feedback")
    def request_feedback(request_id: str, identity=Depends(client)):
        return FeedbackRegistry(app.state.router.sessions).read(identity, request_id)

    @app.get("/v1/requests/{request_id}/audit")
    def request_audit(request_id: str, limit: int = 200, offset: int = 0, identity=Depends(client)):
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        with app.state.router.sessions() as session:
            owned(session, request_id, identity)
            rows = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.request_id == request_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
                .limit(limit)
                .offset(offset)
            )
            return [
                {"event_type": r.event_type, "payload": r.payload_json, "created_at": r.created_at}
                for r in rows
            ]

    def set_stop(stopped):
        active = app.state.router
        active.stopped = stopped
        return {"stopped": stopped, "scope": "database"}

    @app.get("/v1/system/scheduler", dependencies=[Depends(administrator)])
    async def scheduler_status():
        return app.state.router.scheduler.snapshot() | {"inflight": len(app.state.router.inflight)}

    @app.get("/v1/system/providers/{provider_id}/recovery", dependencies=[Depends(administrator)])
    async def inspect_recovery(provider_id: str):
        return Recovery(app.state.router).inspect_provider(provider_id)

    @app.post("/v1/system/providers/{provider_id}/recover", dependencies=[Depends(administrator)])
    async def recover_provider(provider_id: str, request: ProviderRecovery):
        return Recovery(app.state.router).provider(provider_id, request)

    @app.post("/v1/system/requests/recover", dependencies=[Depends(administrator)])
    async def recover_requests(request: RequestRecovery):
        return Recovery(app.state.router).requests(request)

    @app.get("/v1/providers/{provider_id}/health")
    async def provider_health(provider_id: str, identity=Depends(client)):
        active = app.state.router
        spec = active.registry.providers.get(provider_id)
        if spec is None:
            raise HTTPException(404, "Provider not found")
        state = await active.quota.state(provider_id)
        return {
            "provider_id": provider_id,
            "status": await active.quota.effective_status(spec),
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

    @app.post("/v1/system/sandbox/recover", dependencies=[Depends(administrator)])
    async def recover_sandbox():
        active = app.state.router
        if active.sandbox is None or active.sandbox.owner is None:
            raise HTTPException(409, "Owned sandbox recovery is not configured")
        try:
            result = await active.sandbox.recover()
        except SandboxUnavailable:
            result = {"healthy": False, "reason_code": "SANDBOX_RECOVERY_FAILED"}
        with active.sessions.begin() as session:
            active.audit(session, None, "admin", "SANDBOX_RECOVERY", result)
        if not result["healthy"]:
            raise HTTPException(503, "Sandbox recovery incomplete")
        return result

    return app


app = create_app()
