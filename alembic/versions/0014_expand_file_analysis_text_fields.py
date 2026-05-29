"""expand file analysis text fields

Revision ID: 0014_expand_file_analysis_text_fields
Revises: 0013_add_application_tags
Create Date: 2026-04-28 13:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0014_expand_file_analysis_text_fields"
down_revision = "0013_add_application_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "file_analysis_result" not in inspector.get_table_names():
        return
    if bind.dialect.name == "mysql":
        with op.batch_alter_table("file_analysis_result") as batch_op:
            batch_op.alter_column("ocr_text", existing_type=sa.Text(), type_=mysql.LONGTEXT(), nullable=True)
            batch_op.alter_column("analysis_json", existing_type=sa.Text(), type_=mysql.LONGTEXT(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "file_analysis_result" not in inspector.get_table_names():
        return
    if bind.dialect.name == "mysql":
        with op.batch_alter_table("file_analysis_result") as batch_op:
            batch_op.alter_column("ocr_text", existing_type=mysql.LONGTEXT(), type_=sa.Text(), nullable=True)
            batch_op.alter_column("analysis_json", existing_type=mysql.LONGTEXT(), type_=sa.Text(), nullable=False)
