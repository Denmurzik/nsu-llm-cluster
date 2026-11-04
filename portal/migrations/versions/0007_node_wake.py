"""the NIC a node reported for Wake-on-LAN, and a cleanly stopped standalone node (task 2-18)

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_wake",
        sa.Column("node_name", sa.String(255), primary_key=True),
        sa.Column("mac", sa.String(17), nullable=False),
        sa.Column("ip", sa.String(15), nullable=False),
        sa.Column("prefix", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("standalone_nodes", sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("standalone_nodes", "stopped_at")
    op.drop_table("node_wake")
