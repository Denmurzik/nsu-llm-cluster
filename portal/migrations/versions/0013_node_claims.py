"""Node identity, fix round 1 of task SEC-1: claims and lost replies

- claim_*: a registration of a known name that has no key (a legacy node registering from an unknown address or after the
  migration window, a name whose key the admin reset) is kept apart as a claim with a key of its own; the name's row and
  its registry record stay as they are until the admin approves the claim.
- key_nonce_hash / key_used_at: a registration whose reply got lost may be repeated with the same client nonce while the
  key it was given has never been used; it then gets a new key instead of 409.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

COLUMNS = (
    ("claim_hash", sa.String(64)),
    ("claim_backend_key", sa.String(64)),
    ("claim_ip", sa.String(64)),
    ("claim_at", sa.DateTime(timezone=True)),
    ("claim_nonce_hash", sa.String(64)),
    ("key_nonce_hash", sa.String(64)),
    ("key_used_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column("node_identities", sa.Column(name, kind, nullable=True))
    # keys issued before this version were all in use already (a node only got one by talking to the portal)
    op.execute("UPDATE node_identities SET key_used_at = now() WHERE key_hash IS NOT NULL")


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("node_identities", name)
