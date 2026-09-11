"""Add indexes on frequently queried columns."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_task_requests_status", "task_requests", ["status"])
    op.create_index("ix_task_requests_created_at", "task_requests", ["created_at"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def downgrade():
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_task_requests_created_at", table_name="task_requests")
    op.drop_index("ix_task_requests_status", table_name="task_requests")
