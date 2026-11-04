"""own model files beside Hugging Face: checked files of models/local/ and resumable uploads

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_model_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model", sa.String(96), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.String(512), nullable=False, server_default=""),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("model", "path", name="uq_local_model_files_model_path"),
        sa.CheckConstraint("origin in ('upload', 'folder')", name="ck_local_model_files_origin"),
        sa.CheckConstraint("status in ('ready', 'invalid')", name="ck_local_model_files_status"),
    )
    op.create_table(
        "model_uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("model", sa.String(96), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("received_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256_expected", sa.String(64), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("verified_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(320), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('uploading', 'verifying', 'done', 'failed', 'cancelled')", name="ck_model_uploads_status"
        ),
    )
    op.create_index(
        "uq_model_uploads_active", "model_uploads", ["model", "path"], unique=True,
        postgresql_where=sa.text("status in ('uploading', 'verifying')"),
    )


def downgrade() -> None:
    op.drop_index("uq_model_uploads_active", table_name="model_uploads")
    op.drop_table("model_uploads")
    op.drop_table("local_model_files")
