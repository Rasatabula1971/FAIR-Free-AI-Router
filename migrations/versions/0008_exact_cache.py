"""Client-isolated exact cache references."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cache_entries",
        sa.Column("client_id", sa.String(128), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column(
            "source_request_id", sa.String(36), sa.ForeignKey("task_requests.id"), nullable=False
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_cache_entries_expires_at", "cache_entries", ["expires_at"])


def downgrade():
    op.drop_index("ix_cache_entries_expires_at", table_name="cache_entries")
    op.drop_table("cache_entries")
