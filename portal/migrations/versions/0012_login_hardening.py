"""Login hardening (task SEC-1): users.email_verified; the OAuth state of a login in progress moves to Redis

`email_verified` keeps what the provider said at the last login, so the admin role from ADMIN_EMAILS is re-evaluated on
every request (app.auth.roles); existing users have NULL (unknown, not verified) until they log in again. The table
`oauth_states` is dropped: an anonymous /auth/login no longer writes to Postgres (app.auth.routes keeps the state in Redis
for 10 minutes). Logins in progress during the upgrade have to be started again.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=True))
    op.drop_table("oauth_states")


def downgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(255), primary_key=True),
        sa.Column("nonce", sa.String(255), nullable=False),
        sa.Column("code_verifier", sa.String(255), nullable=False),
        sa.Column("return_to", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.drop_column("users", "email_verified")
