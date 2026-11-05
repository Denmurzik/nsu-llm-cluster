"""Node identity, fix round 2 of task SEC-1: several claims per name (table node_claims)

A claim on a known name used to be one set of columns of node_identities (0013): the first claim won and a later one,
maybe the real node's, was refused unseen. Now every claim is a row of its own; the admin compares their addresses with
the ones the portal knows for the name and picks one.

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

OLD = ("claim_hash", "claim_backend_key", "claim_ip", "claim_at", "claim_nonce_hash")


def upgrade() -> None:
    op.create_table(
        "node_claims",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("backend_key", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("nonce_hash", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_node_claims_name", "node_claims", ["name"])
    op.execute(
        "INSERT INTO node_claims (name, key_hash, backend_key, ip, nonce_hash, at)"
        " SELECT name, claim_hash, claim_backend_key, claim_ip, claim_nonce_hash, COALESCE(claim_at, now())"
        " FROM node_identities WHERE claim_hash IS NOT NULL"
    )
    for column in OLD:
        op.drop_column("node_identities", column)


def downgrade() -> None:
    op.add_column("node_identities", sa.Column("claim_hash", sa.String(64), nullable=True))
    op.add_column("node_identities", sa.Column("claim_backend_key", sa.String(64), nullable=True))
    op.add_column("node_identities", sa.Column("claim_ip", sa.String(64), nullable=True))
    op.add_column("node_identities", sa.Column("claim_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("node_identities", sa.Column("claim_nonce_hash", sa.String(64), nullable=True))
    op.drop_index("ix_node_claims_name", table_name="node_claims")
    op.drop_table("node_claims")
