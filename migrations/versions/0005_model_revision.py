"""Bind benchmark qualification to an explicitly configured model revision."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("models", sa.Column("model_revision", sa.String(128), nullable=True))


def downgrade():
    op.drop_column("models", "model_revision")
