"""initial schema

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sub", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(512), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("role in ('user', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "web_sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("id_token", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_sessions_user_id", "web_sessions", ["user_id"])
    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(255), primary_key=True),
        sa.Column("nonce", sa.String(255), nullable=False),
        sa.Column("code_verifier", sa.String(255), nullable=False),
        sa.Column("return_to", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("litellm_token", sa.String(255), nullable=False, unique=True),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("masked", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_playground", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_table(
        "model_deployments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("engine", sa.String(16), nullable=False),
        sa.Column("repo", sa.String(512), nullable=False),
        sa.Column("file", sa.String(512), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("group_size", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("accel", sa.String(16), nullable=False),
        sa.Column("ctx_per_request", sa.Integer(), nullable=False),
        sa.Column("parallel", sa.Integer(), nullable=False),
        sa.Column("auto_assign", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("engine in ('llamacpp', 'vllm')", name="ck_model_deployments_engine"),
        sa.CheckConstraint("mode in ('replicas', 'split')", name="ck_model_deployments_mode"),
        sa.CheckConstraint("accel in ('cpu', 'nvidia', 'any')", name="ck_model_deployments_accel"),
    )
    op.create_table(
        "node_assignments",
        sa.Column("node_name", sa.String(255), primary_key=True),
        sa.Column(
            "deployment_id", sa.Integer(), sa.ForeignKey("model_deployments.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("source in ('k8s', 'standalone')", name="ck_node_assignments_source"),
    )
    op.create_table(
        "standalone_nodes",
        sa.Column("name", sa.String(255), primary_key=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("accel", sa.String(16), nullable=False),
        sa.Column("vram_mib", sa.BigInteger(), nullable=True),
        sa.Column("ram_mib", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("applied_spec_hash", sa.String(64), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )


def downgrade() -> None:
    for table in (
        "standalone_nodes",
        "node_assignments",
        "model_deployments",
        "api_keys",
        "oauth_states",
        "web_sessions",
        "users",
    ):
        op.drop_table(table)
