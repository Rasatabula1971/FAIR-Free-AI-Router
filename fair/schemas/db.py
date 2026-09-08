from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    """Configuration snapshot. Runtime health/quota are stored separately."""

    __tablename__ = "providers"
    provider_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    access_class: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    current_access_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_paid_subscription: Mapped[bool] = mapped_column(Boolean)
    requires_credit_purchase: Mapped[bool] = mapped_column(Boolean)
    auto_billing_required: Mapped[bool] = mapped_column(Boolean)
    programmatic_access: Mapped[bool] = mapped_column(Boolean)
    production_eligibility: Mapped[bool] = mapped_column(Boolean)
    terms_last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_data_class: Mapped[str] = mapped_column(String(32))
    request_limit: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Model(Base):
    __tablename__ = "models"
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.provider_id"), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    context_window: Mapped[int] = mapped_column(Integer)
    capabilities: Mapped[list] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean)


class ProviderQuotaState(Base):
    __tablename__ = "provider_quota_states"
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.provider_id"), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    security_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # UTC Unix seconds survive restarts; monotonic values must never be persisted.
    blocked_until: Mapped[float] = mapped_column(Float, default=0)
    reset_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    circuit_state: Mapped[str] = mapped_column(String(16), default="CLOSED")
    probe_until: Mapped[float] = mapped_column(Float, default=0)
    failures: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderHealthEvent(Base):
    __tablename__ = "provider_health_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.provider_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    circuit_state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemState(Base):
    __tablename__ = "system_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    stopped: Mapped[bool] = mapped_column(Boolean, default=False)


class TaskRequest(Base):
    __tablename__ = "task_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    profile_json: Mapped[dict] = mapped_column(JSON)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RoutingAttempt(Base):
    __tablename__ = "routing_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"), index=True)
    detail_json: Mapped[dict] = mapped_column(JSON)


class TaskProfile(Base):
    __tablename__ = "task_profiles"
    request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"), primary_key=True)
    task_class: Mapped[str] = mapped_column(String(64))
    required_capabilities: Mapped[list] = mapped_column(JSON)
    context_tokens_estimate: Mapped[int] = mapped_column(Integer)
    minimum_quality_score: Mapped[float] = mapped_column(Float)
    requires_grounding: Mapped[bool] = mapped_column(Boolean)
    profile_source: Mapped[str] = mapped_column(String(16))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str | None] = mapped_column(ForeignKey("task_requests.id"), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


@event.listens_for(AuditEvent, "before_update")
@event.listens_for(AuditEvent, "before_delete")
def prevent_audit_mutation(*args):
    raise ValueError("Audit events are append-only")


def database(url):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(engine, expire_on_commit=False)
