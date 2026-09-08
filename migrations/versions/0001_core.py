"""Initial request, attempt and audit lineage."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_requests_client_id", "task_requests", ["client_id"])
    op.create_table(
        "routing_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("task_requests.id"), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_routing_attempts_request_id", "routing_attempts", ["request_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("task_requests.id"), nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION fair_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Audit events are append-only'; END; $$""")
        op.execute("""CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION fair_audit_immutable()""")


def downgrade():
    op.drop_table("audit_events")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION fair_audit_immutable()")
    op.drop_table("routing_attempts")
    op.drop_table("task_requests")
