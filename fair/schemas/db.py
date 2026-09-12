from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


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
    independence_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)


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
    last_reserved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_quota_reset_at: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    execution_kind: Mapped[str] = mapped_column(
        String(16), default="PRIMARY", server_default="PRIMARY"
    )
    parent_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_requests.id"), nullable=True, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    profile_json: Mapped[dict] = mapped_column(JSON)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RoutingAttempt(Base):
    __tablename__ = "routing_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"), index=True)
    detail_json: Mapped[dict] = mapped_column(JSON)


class CacheEntry(Base):
    __tablename__ = "cache_entries"
    client_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class TaskProfile(Base):
    __tablename__ = "task_profiles"
    request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"), primary_key=True)
    task_class: Mapped[str] = mapped_column(String(64))
    required_capabilities: Mapped[list] = mapped_column(JSON)
    context_tokens_estimate: Mapped[int] = mapped_column(Integer)
    minimum_quality_score: Mapped[float] = mapped_column(Float)
    requires_grounding: Mapped[bool] = mapped_column(Boolean)
    profile_source: Mapped[str] = mapped_column(String(16))


class QualityRecord(Base):
    __tablename__ = "quality_reports"
    attempt_id: Mapped[str] = mapped_column(ForeignKey("routing_attempts.id"), primary_key=True)
    overall_score: Mapped[float | None] = mapped_column(Float)
    hard_reject: Mapped[bool] = mapped_column(Boolean)
    verification_state: Mapped[str] = mapped_column(String(32))
    report_json: Mapped[dict] = mapped_column(JSON)


class ModelTaskPerformance(Base):
    __tablename__ = "model_task_performance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_id", "model_id"], ["models.provider_id", "models.model_id"]
        ),
    )
    provider_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_class: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    quality_failures: Mapped[int] = mapped_column(Integer, default=0)
    infra_failures: Mapped[int] = mapped_column(Integer, default=0)
    quota_failures: Mapped[int] = mapped_column(Integer, default=0)
    unverified: Mapped[int] = mapped_column(Integer, default=0)
    hallucination_events: Mapped[int] = mapped_column(Integer, default=0)
    quality_samples: Mapped[int] = mapped_column(Integer, default=0)
    quality_sum: Mapped[float] = mapped_column(Float, default=0)
    latency_sum_ms: Mapped[float] = mapped_column(Float, default=0)
    recent_quality: Mapped[list] = mapped_column(JSON, default=list)
    recent_outcomes: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    last_quality_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_id", "model_id"], ["models.provider_id", "models.model_id"]
        ),
        Index(
            "ix_feedback_scoring",
            "client_id", "provider_id", "model_id", "task_class", "created_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"), unique=True)
    client_id: Mapped[str] = mapped_column(String(128), index=True)
    provider_id: Mapped[str] = mapped_column(String(128))
    model_id: Mapped[str] = mapped_column(String(256))
    task_class: Mapped[str] = mapped_column(String(64))
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    fingerprint: Mapped[str] = mapped_column(String(64))
    reason_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correction_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EscalationRecord(Base):
    __tablename__ = "escalation_requests"
    request_id: Mapped[str] = mapped_column(ForeignKey("task_requests.id"), primary_key=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    detail_json: Mapped[dict] = mapped_column(JSON)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_type_created", "event_type", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str | None] = mapped_column(ForeignKey("task_requests.id"), nullable=True, index=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
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
            import sqlite3

            from sqlalchemy.pool import QueuePool

            name = uuid4().hex
            kwargs["creator"] = lambda: sqlite3.connect(
                f"file:{name}?mode=memory&cache=shared",
                uri=True,
                check_same_thread=False,
            )
            kwargs["poolclass"] = QueuePool
            kwargs["pool_size"] = 1
            kwargs["max_overflow"] = 0
            url = "sqlite://"
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(engine, expire_on_commit=False)
