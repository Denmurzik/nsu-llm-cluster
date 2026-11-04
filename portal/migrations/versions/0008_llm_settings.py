"""llama.cpp settings of a deployment: reasoning, KV cache type, sampling, draft model, images, own flags, prompt

Rows made before keep the old behaviour: reasoning off, KV cache f16, nothing else set.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_COLUMNS = (
    "reasoning", "kv_cache_type", "sampling", "draft_repo", "draft_file", "draft_revision", "mmproj_file",
    "extra_args", "system_prompt", "experimental",
)


def upgrade() -> None:
    table = "model_deployments"
    op.add_column(table, sa.Column("reasoning", sa.String(8), nullable=False, server_default="off"))
    op.add_column(table, sa.Column("kv_cache_type", sa.String(8), nullable=False, server_default="f16"))
    op.add_column(table, sa.Column("sampling", sa.JSON(), nullable=True))
    op.add_column(table, sa.Column("draft_repo", sa.String(512), nullable=True))
    op.add_column(table, sa.Column("draft_file", sa.String(512), nullable=True))
    op.add_column(table, sa.Column("draft_revision", sa.String(40), nullable=True))
    op.add_column(table, sa.Column("mmproj_file", sa.String(512), nullable=True))
    op.add_column(table, sa.Column("extra_args", sa.String(2000), nullable=True))
    op.add_column(table, sa.Column("system_prompt", sa.Text(), nullable=True))
    op.add_column(table, sa.Column("experimental", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("model_deployments", column)
