from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlmodel import Field, SQLModel


class Application(SQLModel, table=True):
    __tablename__ = "comprehensive_apply"

    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, index=True))
    applicant_id: int = Field(
        sa_column=Column(Integer, ForeignKey("user_info.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    category: str = Field(sa_column=Column(String(32), nullable=False, index=True))
    sub_type: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    award_uid: int = Field(sa_column=Column(Integer, nullable=False, index=True))
    title: str = Field(sa_column=Column(String(255), nullable=False))
    description: str = Field(sa_column=Column(Text, nullable=False))
    occurred_at: date = Field(sa_column=Column(Date, nullable=False))
    status: str = Field(sa_column=Column(String(32), nullable=False, index=True, default="pending_ai"))
    item_score: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    total_score: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    actual_score_recorded: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, default=False, index=True),
    )
    comment: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    score_rule_version: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    tags_json: str = Field(default="[]", sa_column=Column(Text, nullable=False, default="[]"))
    version: int = Field(default=1, sa_column=Column(Integer, nullable=False, default=1))
    is_deleted: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False, index=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
