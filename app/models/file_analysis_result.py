from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects import mysql
from sqlmodel import Field, SQLModel


class FileAnalysisResult(SQLModel, table=True):
    __tablename__ = "file_analysis_result"

    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True, index=True))
    file_id: str = Field(
        sa_column=Column(String(128), ForeignKey("file_info.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    )
    provider: str = Field(sa_column=Column(String(50), nullable=False, default="paddleocr"))
    status: str = Field(sa_column=Column(String(20), nullable=False, default="queued", index=True))
    ocr_text: str | None = Field(default=None, sa_column=Column(Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True))
    analysis_json: str = Field(sa_column=Column(Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False, default="{}"))
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    analyzed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
