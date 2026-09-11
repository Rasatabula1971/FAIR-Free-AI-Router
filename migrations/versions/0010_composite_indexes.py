"""Add composite indexes for performance scoring and status snapshot queries."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_feedback_scoring",
        "feedback_events",
        ["client_id", "provider_id", "model_id", "task_class", "created_at"],
    )
    op.create_index(
        "ix_audit_type_created",
        "audit_events",
        ["event_type", "created_at"],
    )


def downgrade():
    op.drop_index("ix_audit_type_created", table_name="audit_events")
    op.drop_index("ix_feedback_scoring", table_name="feedback_events")
