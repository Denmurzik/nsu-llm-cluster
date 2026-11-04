"""operation of the class: node maintenance, spare nodes, regroup grace and schedule of deployments, controller timers

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_deployments", sa.Column("spare_nodes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("model_deployments", sa.Column("regroup_grace_s", sa.Integer(), nullable=True))
    op.add_column("model_deployments", sa.Column("schedule", sa.JSON(), nullable=True))
    op.add_column("node_assignments", sa.Column("maintenance", sa.Boolean(), nullable=False, server_default="false"))
    op.create_table(
        "controller_timers",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("since", sa.Float(), nullable=False),
        sa.Column("members", sa.Text(), nullable=False, server_default=""),
        sa.Column("leader_lost", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_table("controller_timers")
    op.drop_column("node_assignments", "maintenance")
    op.drop_column("model_deployments", "schedule")
    op.drop_column("model_deployments", "regroup_grace_s")
    op.drop_column("model_deployments", "spare_nodes")
