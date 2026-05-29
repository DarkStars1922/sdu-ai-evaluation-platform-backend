from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.term_utils import current_fill_term_label


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "综合测评平台"
    environment: str = "local"

    secret_key: str = "crying-1385432-shr124567755"
    algorithm: str = "HS256"
    access_token_expire_seconds: int = 7200
    refresh_token_expire_seconds: int = 604800

    database_url: str = "sqlite:///./platform.db"
    sql_echo: bool = False
    auto_create_tables: bool = True
    db_pool_size: int = 8
    db_max_overflow: int = 8
    db_pool_recycle_seconds: int = 1800
    db_pool_timeout_seconds: int = 30

    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_enabled: bool = True
    access_token_blacklist_prefix: str = "auth:blacklist:"
    idempotency_prefix: str = "idempotency:"
    cache_prefix: str = "cache:"
    export_status_prefix: str = "export:"

    celery_task_always_eager: bool = True
    celery_task_eager_propagates: bool = True
    celery_result_expires_seconds: int = 86400
    celery_worker_prefetch_multiplier: int = 1
    celery_task_acks_late: bool = True
    celery_task_reject_on_worker_lost: bool = True

    upload_dir: str = "./uploads"
    export_dir: str = "./exports"
    upload_max_file_size: int = 26214400
    allowed_upload_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "image/jpeg",
            "image/png",
            "image/webp",
        ]
    )

    email_provider: str = "mock"
    email_default_from: str = "noreply@zcpt.example.com"
    email_mock_success: bool = True

    ai_audit_provider: str = "paddleocr"
    ai_audit_fallback_to_manual: bool = True
    image_authenticity_provider: str = "hybrid"
    image_authenticity_api_url: str | None = None
    image_authenticity_api_key: str | None = None
    image_authenticity_model: str = "c2pa+synthid+trufor+universal_fake_detect"
    image_authenticity_timeout_seconds: float = 30.0
    image_authenticity_c2patool_path: str = "c2patool"
    file_analysis_enabled: bool = True
    paddle_model_source: str = "BOS"
    paddle_model_dir: str = "./models/paddleocr"
    paddleocr_device: str = "cpu"
    paddleocr_use_doc_orientation_classify: bool = True
    paddleocr_use_textline_orientation: bool = True
    paddleocr_text_detection_model_name: str = "PP-OCRv5_mobile_det"
    paddleocr_text_detection_model_dir: str | None = None
    paddleocr_text_recognition_model_name: str = "PP-OCRv5_mobile_rec"
    paddleocr_text_recognition_model_dir: str | None = None
    paddleocr_layout_detection_model_name: str = "PP-DocLayout-S"
    paddleocr_layout_detection_model_dir: str | None = None
    paddleocr_cpu_threads: int = 4
    paddleocr_enable_mkldnn: bool = False
    paddleocr_seal_score_threshold: float = 0.4

    default_term: str = Field(default_factory=current_fill_term_label)
    export_download_base_path: str = "/api/v1/teacher/exports"

    evaluation_llm_api_url: str | None = None
    evaluation_llm_api_key: str | None = None
    evaluation_llm_model: str = "gpt-4o-mini"
    evaluation_llm_timeout_seconds: float = 60.0
    evaluation_llm_temperature: float = 0.7
    evaluation_llm_max_tokens: int = 320
    report_story_llm_api_url: str | None = None
    report_story_llm_api_key: str | None = None
    report_story_llm_model: str | None = None
    report_story_llm_timeout_seconds: float = 120.0
    report_story_llm_temperature: float = 0.85
    report_story_llm_max_tokens: int = 1200
    teacher_analysis_llm_api_url: str | None = None
    teacher_analysis_llm_api_key: str | None = None
    teacher_analysis_llm_model: str | None = None
    teacher_analysis_llm_timeout_seconds: float = 120.0
    teacher_analysis_llm_temperature: float = 0.35
    teacher_analysis_llm_max_tokens: int = 1800
    auto_fill_llm_api_url: str | None = None
    auto_fill_llm_api_key: str | None = None
    auto_fill_llm_model: str | None = None
    auto_fill_llm_timeout_seconds: float = 120.0
    auto_fill_llm_temperature: float = 0.1
    auto_fill_llm_max_tokens: int = 1800
    auto_fill_job_ttl_seconds: int = 1800
    auto_fill_job_prefix: str = "auto_fill:job:"

    @property
    def upload_dir_path(self) -> Path:
        return Path(self.upload_dir)

    @property
    def export_dir_path(self) -> Path:
        return Path(self.export_dir)

    @property
    def paddle_model_dir_path(self) -> Path:
        return Path(self.paddle_model_dir)


settings = Settings()
