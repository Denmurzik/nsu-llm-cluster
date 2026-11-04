"""Users with a source of the role, blocking and own limits; audit log; runtime settings; monitoring samples (task 2-31a)

Existing users keep the role they have: role_source `auto` is today's behaviour (the role follows ADMIN_EMAILS).

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role_source", sa.String(8), nullable=False, server_default="auto"))
    op.add_column("users", sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("blocked_reason", sa.String(500), nullable=True))
    op.add_column("users", sa.Column("rpm_limit", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("tpm_limit", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("max_keys", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_users_role_source", "users", "role_source in ('auto', 'manual')")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_email", sa.String(320), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(320), nullable=True),
    )

    op.create_table(
        "metric_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.CheckConstraint("kind in ('node', 'model')", name="ck_metric_samples_kind"),
    )
    op.create_index("ix_metric_samples_kind_name_at", "metric_samples", ["kind", "name", "at"])


def downgrade() -> None:
    op.drop_table("metric_samples")
    op.drop_table("system_settings")
    op.drop_table("audit_log")
    op.drop_constraint("ck_users_role_source", "users", type_="check")
    for column in ("max_keys", "tpm_limit", "rpm_limit", "blocked_reason", "blocked", "role_source"):
        op.drop_column("users", column)
