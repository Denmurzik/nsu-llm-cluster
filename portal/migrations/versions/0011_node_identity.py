"""Per-node identity and approval (task SEC-1): table node_identities

Every node the portal already knows (a registry row, an assignment, a reported NIC or models volume) becomes an approved
`legacy` identity without a key: its first call after the upgrade from the address the portal knows for it gets a key
(app.nodes.identity), a call from anywhere else makes it wait for the admin's approval again.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_identities",
        sa.Column("name", sa.String(255), primary_key=True),
        sa.Column("key_hash", sa.String(64), nullable=True),
        sa.Column("backend_key", sa.String(64), nullable=True),
        sa.Column("key_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(320), nullable=True),
        sa.Column("legacy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_ip", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # the known address: the host of a standalone node's url (http://<ip>:<port>), else the NIC it reported
    op.execute(
        """
        INSERT INTO node_identities (name, approved, approved_at, legacy, last_ip)
        SELECT n.name, true, now(), true,
               COALESCE(
                 (SELECT substring(s.url from '^http://([^:/]+)') FROM standalone_nodes s WHERE s.name = n.name),
                 (SELECT w.ip FROM node_wake w WHERE w.node_name = n.name))
        FROM (
          SELECT name FROM standalone_nodes
          UNION SELECT node_name FROM node_assignments
          UNION SELECT node_name FROM node_wake
          UNION SELECT node_name FROM node_models
        ) AS n(name)
        WHERE n.name <> 'llm-main'
        """
    )


def downgrade() -> None:
    op.drop_table("node_identities")
