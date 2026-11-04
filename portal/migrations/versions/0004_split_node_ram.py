"""split over VRAM and RAM of the nodes: per deployment, whether the RAM is used and which share of it

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_deployments", sa.Column("use_node_ram", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("model_deployments", sa.Column("ram_budget_pct", sa.Integer(), nullable=False, server_default="70"))


def downgrade() -> None:
    op.drop_column("model_deployments", "ram_budget_pct")
    op.drop_column("model_deployments", "use_node_ram")
