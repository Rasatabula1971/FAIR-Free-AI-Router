"""Retain quota boundaries so operator recovery cannot replay an old reset."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_quota_states", sa.Column("last_reserved_at", sa.Float(), nullable=True))
    op.add_column(
        "provider_quota_states", sa.Column("last_quota_reset_at", sa.Float(), nullable=True)
    )
    # Legacy usage has no reservation timestamp. Conservatively require a reset after migration.
    connection = op.get_bind()
    from time import time

    connection.execute(
        sa.text("UPDATE provider_quota_states SET last_reserved_at = :now WHERE used > 0"),
        {"now": time()},
    )


def downgrade():
    op.drop_column("provider_quota_states", "last_quota_reset_at")
    op.drop_column("provider_quota_states", "last_reserved_at")
