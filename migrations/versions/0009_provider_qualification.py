"""Nullable qualification evidence; existing routes receive no invented approvals."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("providers", sa.Column("qualification", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("providers", "qualification")
