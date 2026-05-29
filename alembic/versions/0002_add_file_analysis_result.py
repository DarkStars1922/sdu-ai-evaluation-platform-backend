"""add file analysis result table

Revision ID: 0002_add_file_analysis_result
Revises: 0001_initial_schema
Create Date: 2026-04-09 11:40:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = "0002_add_file_analysis_result"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "file_analysis_result" in inspector.get_table_names():
        return

    op.create_table(
        "file_analysis_result",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("file_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ocr_text", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True),
        sa.Column("analysis_json", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["file_info.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("file_id"),
    )
    op.create_index("ix_file_analysis_result_id", "file_analysis_result", ["id"], unique=False)
    op.create_index("ix_file_analysis_result_file_id", "file_analysis_result", ["file_id"], unique=False)
    op.create_index("ix_file_analysis_result_status", "file_analysis_result", ["status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "file_analysis_result" not in inspector.get_table_names():
        return

    op.drop_index("ix_file_analysis_result_status", table_name="file_analysis_result")
    op.drop_index("ix_file_analysis_result_file_id", table_name="file_analysis_result")
    op.drop_index("ix_file_analysis_result_id", table_name="file_analysis_result")
    op.drop_table("file_analysis_result")
