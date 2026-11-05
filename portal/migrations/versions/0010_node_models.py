"""how much the models volume of a node holds, for the «Ноды» page and the cleanup (task 2-26)

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_models",
        sa.Column("node_name", sa.String(255), primary_key=True),
        sa.Column("models_bytes", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("node_models")
