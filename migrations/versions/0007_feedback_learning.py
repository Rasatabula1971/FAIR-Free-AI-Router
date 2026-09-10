"""Client feedback, recent observations and shadow lineage."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "task_requests",
        sa.Column("execution_kind", sa.String(16), server_default="PRIMARY", nullable=False),
    )
    # Add in place: rebuilding task_requests would break existing audit/attempt foreign keys.
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE task_requests ADD COLUMN parent_request_id VARCHAR(36) REFERENCES task_requests(id)"
        )
    else:
        op.add_column(
            "task_requests",
            sa.Column(
                "parent_request_id", sa.String(36), sa.ForeignKey("task_requests.id"), nullable=True
            ),
        )
    op.create_index(
        "ix_task_requests_parent_request_id", "task_requests", ["parent_request_id"], unique=True
    )
    op.add_column(
        "model_task_performance",
        sa.Column("recent_outcomes", sa.JSON(), server_default="[]", nullable=False),
    )
    op.add_column(
        "model_task_performance",
        sa.Column("last_quality_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("task_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("model_id", sa.String(256), nullable=False),
        sa.Column("task_class", sa.String(64), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("reason_hash", sa.String(64), nullable=True),
        sa.Column("correction_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id", "model_id"], ["models.provider_id", "models.model_id"]
        ),
    )
    op.create_index("ix_feedback_events_client_id", "feedback_events", ["client_id"])


def downgrade():
    op.drop_table("feedback_events")
    op.drop_column("model_task_performance", "last_quality_at")
    op.drop_column("model_task_performance", "recent_outcomes")
    op.drop_index("ix_task_requests_parent_request_id", table_name="task_requests")
    op.drop_column("task_requests", "parent_request_id")
    op.drop_column("task_requests", "execution_kind")
