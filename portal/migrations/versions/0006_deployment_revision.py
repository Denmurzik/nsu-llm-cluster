"""the Hugging Face commit a deployment is pinned to (NULL: main, as before)

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_deployments", sa.Column("revision", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("model_deployments", "revision")
