from datetime import date

from pydantic import BaseModel, Field, model_validator

from app.core.score_rules import is_valid_score_category


class AttachmentPayload(BaseModel):
    file_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_attachment(self):
        if not self.file_id:
            raise ValueError("attachments[].file_id 必填")
        return self


class ApplicationCreateRequest(BaseModel):
    award_uid: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    occurred_at: date
    attachments: list[AttachmentPayload] = Field(default_factory=list)
    category: str = Field(min_length=1, max_length=32)
    sub_type: str = Field(min_length=1, max_length=64)
    score: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_score_category(self):
        if not is_valid_score_category(self.category, self.sub_type):
            raise ValueError("category/sub_type must be one of the configured score categories")
        return self


class ApplicationUpdateRequest(ApplicationCreateRequest):
    version: int | None = Field(default=None, ge=1)


class AutoFillJobCreateRequest(BaseModel):
    attachments: list[AttachmentPayload] = Field(min_length=1, max_length=12)


class AutoFillConfirmRequest(ApplicationCreateRequest):
    pass
