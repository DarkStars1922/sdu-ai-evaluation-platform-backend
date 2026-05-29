from app.core.config import settings
from app.core.award_catalog import serialize_award_rule
from app.core.utils import json_loads
from app.models.ai_audit_report import AIAuditReport
from app.models.announcement import Announcement
from app.models.appeal import Appeal
from app.models.application import Application
from app.models.archive_record import ArchiveRecord
from app.models.email_record import EmailRecord
from app.models.export_task import ExportTask
from app.models.file_analysis_result import FileAnalysisResult
from app.models.file_info import FileInfo
from app.models.review_record import ReviewRecord
from app.models.reviewer_token import ReviewerToken
from app.models.system_config import SystemConfig
from app.models.system_log import SystemLog
from app.models.user import User


def serialize_user(user: User, *, tokens: list[dict] | None = None) -> dict:
    payload = {
        "id": user.id,
        "account": user.account,
        "name": user.name,
        "role": user.role,
        "class_id": user.class_id,
        "is_reviewer": bool(user.is_reviewer),
        "reviewer_token_id": user.reviewer_token_id,
        "email": user.email,
        "phone": user.phone,
        "is_deleted": bool(getattr(user, "is_deleted", False)),
    }
    if tokens is not None:
        payload["tokens"] = tokens
    return payload


def serialize_file(file: FileInfo, *, analysis: FileAnalysisResult | None = None) -> dict:
    payload = {
        "file_id": file.id,
        "filename": file.original_name,
        "content_type": file.content_type,
        "size": file.size,
        "url": f"/api/v1/files/{file.id}",
        "created_at": file.created_at.isoformat(),
    }
    if analysis:
        payload["analysis_status"] = analysis.status
        payload["analysis"] = serialize_file_analysis(analysis)
    return payload


def serialize_application(
    application: Application,
    *,
    attachments: list[dict] | None = None,
    include_detail: bool = False,
) -> dict:
    award_rule = serialize_award_rule(application.award_uid)
    tags = json_loads(getattr(application, "tags_json", None), [])
    if not isinstance(tags, list):
        tags = []
    payload = {
        "application_id": application.id,
        "id": application.id,
        "category": application.category,
        "sub_type": application.sub_type,
        "award_uid": application.award_uid,
        "award_rule": award_rule,
        "award_rule_name": award_rule["rule_name"] if award_rule else None,
        "title": application.title,
        "status": application.status,
        "score": application.item_score,
        "item_score": application.item_score,
        "total_score": application.total_score,
        "comment": application.comment,
        "tags": [str(tag) for tag in tags if str(tag).strip()],
        "version": application.version,
        "created_at": application.created_at.isoformat(),
        "updated_at": application.updated_at.isoformat(),
    }
    if include_detail:
        payload.update(
            {
                "description": application.description,
                "occurred_at": application.occurred_at.isoformat(),
                "attachments": attachments or [],
                "score_rule_version": application.score_rule_version,
            }
        )
    return payload


def serialize_reviewer_token(token: ReviewerToken) -> dict:
    class_ids = json_loads(token.class_ids_json, [])
    return {
        "id": token.id,
        "token_id": token.id,
        "token": token.token,
        "type": token.token_type,
        "class_ids": class_ids,
        "status": token.status,
        "expired_at": token.expires_at.isoformat() if token.expires_at else None,
        "activated_at": token.activated_at.isoformat() if token.activated_at else None,
        "activated_user_id": token.activated_user_id,
        "created_at": token.created_at.isoformat(),
    }


def serialize_review_record(record: ReviewRecord) -> dict:
    return {
        "review_id": record.id,
        "application_id": record.application_id,
        "reviewer_id": record.reviewer_id,
        "reviewer_role": record.reviewer_role,
        "decision": record.decision,
        "result": record.result,
        "comment": record.comment,
        "reviewed_at": record.created_at.isoformat(),
    }


def serialize_export_task(task: ExportTask) -> dict:
    download_url = None
    if task.status == "completed":
        download_url = f"{settings.export_download_base_path}/{task.task_id}/download"
    return {
        "task_id": task.task_id,
        "scope": task.scope,
        "format": task.format,
        "filters": json_loads(task.filters_json, {}),
        "status": task.status,
        "file_name": task.file_name,
        "file_url": download_url,
        "created_at": task.created_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "error_message": task.error_message,
    }


def serialize_archive(record: ArchiveRecord) -> dict:
    class_ids = json_loads(record.class_ids_json, [])
    return {
        "archive_id": record.archive_id,
        "archive_name": record.archive_name,
        "term": record.term,
        "grade": record.grade,
        "class_ids": class_ids,
        "is_announced": record.is_announced,
        "export_task_id": record.export_task_id,
        "download_url": f"/api/v1/archives/exports/{record.archive_id}/download",
        "created_at": record.created_at.isoformat(),
    }


def serialize_announcement(
    announcement: Announcement,
    archive_public_id: str,
    *,
    scopes: list[dict] | None = None,
) -> dict:
    scope = json_loads(announcement.scope_json, {})
    show_fields = json_loads(announcement.show_fields_json, [])
    scope_rows = scopes or []
    archive_ids = []
    class_ids = []
    grade = scope.get("grade")
    for item in scope_rows:
        archive_id = item.get("archive_id")
        if archive_id and archive_id not in archive_ids:
            archive_ids.append(archive_id)
        if grade is None and item.get("grade") is not None:
            grade = item["grade"]
        class_id = item.get("class_id")
        if class_id is not None and class_id not in class_ids:
            class_ids.append(class_id)
    if scope_rows:
        scope = {"grade": grade, "class_ids": sorted(class_ids)}
    if archive_public_id and archive_public_id not in archive_ids:
        archive_ids.insert(0, archive_public_id)
    return {
        "id": announcement.id,
        "announcement_id": announcement.id,
        "title": announcement.title,
        "archive_id": archive_public_id,
        "archive_ids": archive_ids,
        "scope": scope,
        "scopes": scope_rows,
        "show_fields": show_fields,
        "status": announcement.status,
        "start_at": announcement.start_at.isoformat(),
        "end_at": announcement.end_at.isoformat() if announcement.end_at else None,
        "download_url": f"/api/v1/announcements/{announcement.id}/download",
        "created_at": announcement.created_at.isoformat(),
    }


def serialize_appeal(
    appeal: Appeal,
    *,
    student: User | None = None,
    attachments: list[dict] | None = None,
    viewer: User | None = None,
) -> dict:
    anonymous_for_viewer = bool(appeal.is_anonymous) and viewer is not None and viewer.id != appeal.student_id
    return {
        "id": appeal.id,
        "announcement_id": appeal.announcement_id,
        "student_id": None if anonymous_for_viewer else appeal.student_id,
        "student_name": "匿名学生" if anonymous_for_viewer else (student.name if student else None),
        "student_account": None if anonymous_for_viewer else (student.account if student else None),
        "student_email": None if anonymous_for_viewer else (student.email if student else None),
        "application_id": appeal.application_id,
        "is_anonymous": bool(appeal.is_anonymous),
        "content": appeal.content,
        "attachments": attachments or [],
        "status": appeal.status,
        "result": appeal.result,
        "result_comment": appeal.result_comment,
        "score_action": appeal.score_action,
        "adjusted_score": appeal.adjusted_score,
        "created_at": appeal.created_at.isoformat(),
        "processed_at": appeal.processed_at.isoformat() if appeal.processed_at else None,
    }


def serialize_email(record: EmailRecord) -> dict:
    return {
        "id": record.id,
        "application_id": record.application_id,
        "appeal_id": record.appeal_id,
        "to": record.to_email,
        "subject": record.subject,
        "status": record.status,
        "provider": record.provider,
        "error_message": record.error_message,
        "created_at": record.created_at.isoformat(),
        "sent_at": record.sent_at.isoformat() if record.sent_at else None,
    }


def serialize_ai_audit(report: AIAuditReport) -> dict:
    return {
        "application_id": report.application_id,
        "provider": report.provider,
        "status": report.status,
        "result": report.result,
        "ocr_text": report.ocr_text,
        "identity_check": json_loads(report.identity_check_json, {}),
        "consistency_check": json_loads(report.consistency_check_json, {}),
        "risk_points": json_loads(report.risk_points_json, []),
        "score_breakdown": json_loads(report.score_breakdown_json, []),
        "score": report.item_score,
        "total_score": report.total_score,
        "error_message": report.error_message,
        "audited_at": report.audited_at.isoformat() if report.audited_at else None,
        "created_at": report.created_at.isoformat(),
    }


def serialize_file_analysis(record: FileAnalysisResult) -> dict:
    payload = json_loads(record.analysis_json, {})
    return {
        "status": record.status,
        "provider": record.provider,
        "document_title": payload.get("document_title"),
        "ocr_summary": payload.get("ocr_summary"),
        "recognized_levels": payload.get("recognized_levels", []),
        "uploader_name_match": payload.get("uploader_name_match", {}),
        "filename_vs_document_title": payload.get("filename_vs_document_title", {}),
        "seal": payload.get("seal", {"detected": False, "items": []}),
        "signature": payload.get("signature", {"detected": False, "items": []}),
        "error_message": record.error_message,
        "analyzed_at": record.analyzed_at.isoformat() if record.analyzed_at else None,
    }


def serialize_system_log(log: SystemLog) -> dict:
    return {
        "id": log.id,
        "actor_id": log.actor_id,
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "detail": json_loads(log.detail_json, {}),
        "created_at": log.created_at.isoformat(),
    }


def serialize_system_config(config: SystemConfig) -> dict:
    return {
        "config_key": config.config_key,
        "config_value": json_loads(config.config_value_json, {}),
        "description": config.description,
        "updated_at": config.updated_at.isoformat(),
    }
