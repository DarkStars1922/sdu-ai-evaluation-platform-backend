"""add application tags

Revision ID: 0013_add_application_tags
Revises: 0012_add_report_and_insight_cache
Create Date: 2026-04-28 10:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_add_application_tags"
down_revision = "0012_add_report_and_insight_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "comprehensive_apply" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("comprehensive_apply")}
    if "tags_json" not in columns:
        op.add_column(
            "comprehensive_apply",
            sa.Column("tags_json", sa.Text(), nullable=True),
        )
        op.execute("UPDATE comprehensive_apply SET tags_json = '[]' WHERE tags_json IS NULL")
        with op.batch_alter_table("comprehensive_apply") as batch_op:
            batch_op.alter_column("tags_json", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "comprehensive_apply" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("comprehensive_apply")}
    if "tags_json" in columns:
        op.drop_column("comprehensive_apply", "tags_json")
