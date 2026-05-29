from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.award_catalog import load_award_tree
from app.core.config import settings
from app.core.constants import EDITABLE_APPLICATION_STATUSES, REVIEWER_REVIEWABLE_STATUSES, ROLE_STUDENT
from app.core.score_rules import SCORE_RULE_VERSION, is_valid_score_category
from app.core.term_utils import apply_datetime_term_filter
from app.core.utils import json_dumps, utcnow
from app.models.ai_audit_report import AIAuditReport
from app.models.application import Application
from app.models.application_attachment import ApplicationAttachment
from app.models.award_dict import AwardDict
from app.models.file_analysis_result import FileAnalysisResult
from app.models.file_info import FileInfo
from app.models.user import User
from app.schemas.application import ApplicationCreateRequest, ApplicationUpdateRequest
from app.services.errors import ServiceError
from app.services.reviewer_scope_service import get_active_reviewer_class_ids
from app.services.score_summary_service import get_student_score_summary, recalculate_student_score, serialize_score_summary
from app.services.serializers import serialize_application, serialize_file
from app.services.system_log_service import write_system_log
from app.tasks.jobs import enqueue_ai_audit


def list_categories() -> list[dict]:
    return load_award_tree()


def create_application(
    db: Session,
    user: User,
    payload: ApplicationCreateRequest,
    *,
    tags: list[str] | None = None,
) -> dict:
    _require_student(user)
    _validate_score_category(payload.category, payload.sub_type)
    award = _get_award(db, payload.award_uid)
    item_score = _resolve_score(payload.score, award.max_score, award.score)
    clean_tags = _normalize_application_tags(tags)

    application = Application(
        applicant_id=user.id,
        category=payload.category,
        sub_type=payload.sub_type,
        award_uid=payload.award_uid,
        title=payload.title,
        description=payload.description,
        occurred_at=payload.occurred_at,
        status="pending_ai",
        item_score=item_score,
        total_score=item_score,
        score_rule_version=SCORE_RULE_VERSION,
        tags_json=json_dumps(clean_tags),
        updated_at=utcnow(),
    )
    db.add(application)
    db.flush()
    _replace_attachments(db, user, application.id, [item.file_id for item in payload.attachments], auto_commit=False)
    _upsert_ai_report(db, application.id, auto_commit=False)
    db.commit()
    db.refresh(application)
    write_system_log(
        db,
        action="application.create",
        actor_id=user.id,
        target_type="application",
        target_id=str(application.id),
    )
    enqueue_ai_audit(application.id)
    return {
        "id": application.id,
        "application_id": application.id,
        "status": application.status,
        "score": application.item_score,
        "item_score": application.item_score,
        "total_score": application.total_score,
        "score_rule_version": application.score_rule_version,
        "award_uid": application.award_uid,
        "tags": clean_tags,
        "created_at": application.created_at.isoformat(),
    }


def list_my_applications(
    db: Session,
    user: User,
    *,
    status: str | None,
    award_type: str | None,
    category: str | None,
    keyword: str | None,
    page: int,
    size: int,
) -> dict:
    _require_student(user)
    stmt = select(Application).where(Application.applicant_id == user.id, Application.is_deleted.is_(False))
    if status:
        stmt = stmt.where(Application.status == status)
    if category:
        stmt = stmt.where(Application.category == category)
    if award_type:
        stmt = stmt.where(Application.sub_type == award_type)
    if keyword:
        like_value = f"%{keyword}%"
        stmt = stmt.where(or_(Application.title.ilike(like_value), Application.description.ilike(like_value)))

    total = db.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = db.exec(stmt.order_by(Application.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    return {
        "page": page,
        "size": size,
        "total": total,
        "list": [serialize_application(row) for row in rows],
    }


def get_my_category_summary(db: Session, user: User, *, term: str | None) -> dict:
    _require_student(user)
    stmt = select(Application).where(Application.applicant_id == user.id, Application.is_deleted.is_(False))
    stmt = apply_datetime_term_filter(stmt, Application.created_at, term or settings.default_term)
    rows = db.exec(stmt).all()
    categories: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.category, row.sub_type)
        entry = categories.setdefault(
            key,
            {
                "category": row.category,
                "sub_type": row.sub_type,
                "count": 0,
                "approved": 0,
                "pending": 0,
                "rejected": 0,
                "category_score": 0.0,
            },
        )
        entry["count"] += 1
        is_archived_approved = row.status == "archived" and bool(row.actual_score_recorded)
        is_archived_rejected = row.status == "archived" and not bool(row.actual_score_recorded)
        if (row.status == "approved" and bool(row.actual_score_recorded)) or is_archived_approved:
            entry["approved"] += 1
        elif row.status == "rejected" or is_archived_rejected:
            entry["rejected"] += 1
        else:
            entry["pending"] += 1
    score_summary = serialize_score_summary(get_student_score_summary(db, user.id), student_id=user.id)
    sub_scores = score_summary["sub_scores"]
    for entry in categories.values():
        entry["category_score"] = sub_scores.get(f"{entry['category']}_{entry['sub_type']}", 0.0)
    return {
        "term": term,
        "categories": list(categories.values()),
        "total_score": score_summary["actual_score"],
        "raw_total_score": score_summary["raw_total_score"],
        "overflow_score": score_summary["overflow_score"],
        "actual_score": score_summary["actual_score"],
        "score_summary": score_summary,
    }


def get_my_by_category(
    db: Session,
    user: User,
    *,
    category: str,
    sub_type: str | None,
    status: str | None,
    term: str | None,
    page: int,
    size: int,
) -> dict:
    _require_student(user)
    stmt = select(Application).where(
        Application.applicant_id == user.id,
        Application.is_deleted.is_(False),
        Application.category == category,
    )
    stmt = apply_datetime_term_filter(stmt, Application.created_at, term or settings.default_term)
    if sub_type:
        stmt = stmt.where(Application.sub_type == sub_type)
    if status:
        stmt = stmt.where(Application.status == status)
    total = db.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = db.exec(stmt.order_by(Application.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    return {
        "category": category,
        "term": term,
        "page": page,
        "size": size,
        "total": total,
        "list": [serialize_application(row) for row in rows],
    }


def get_application_detail(db: Session, user: User, application_id: int) -> dict:
    application = _get_viewable_application(db, user, application_id)
    attachments = get_application_attachments(db, application.id)
    return serialize_application(application, attachments=attachments, include_detail=True)


def update_application(db: Session, user: User, application_id: int, payload: ApplicationUpdateRequest) -> dict:
    _require_student(user)
    application = _get_owned_application(db, user, application_id)
    if application.status not in EDITABLE_APPLICATION_STATUSES and application.status != "rejected":
        raise ServiceError("application status is not editable", 1000)
    if payload.version is not None and payload.version != application.version:
        raise ServiceError("version conflict", 1007)

    _validate_score_category(payload.category, payload.sub_type)
    award = _get_award(db, payload.award_uid)
    item_score = _resolve_score(payload.score, award.max_score, award.score)

    application.category = payload.category
    application.sub_type = payload.sub_type
    application.award_uid = payload.award_uid
    application.title = payload.title
    application.description = payload.description
    application.occurred_at = payload.occurred_at
    application.item_score = item_score
    application.total_score = item_score
    application.actual_score_recorded = False
    application.status = "pending_ai"
    application.comment = None
    application.version += 1
    application.updated_at = utcnow()
    db.add(application)
    _replace_attachments(db, user, application.id, [item.file_id for item in payload.attachments], auto_commit=False)
    _upsert_ai_report(db, application.id, auto_commit=False)
    db.commit()
    write_system_log(
        db,
        action="application.update",
        actor_id=user.id,
        target_type="application",
        target_id=str(application.id),
    )
    enqueue_ai_audit(application.id)
    db.refresh(application)
    return {
        "id": application.id,
        "application_id": application.id,
        "status": application.status,
        "version": application.version,
        "score": application.item_score,
        "updated_at": application.updated_at.isoformat(),
    }


def withdraw_application(db: Session, user: User, application_id: int) -> dict:
    _require_student(user)
    application = _get_owned_application(db, user, application_id)
    if application.status not in EDITABLE_APPLICATION_STATUSES:
        raise ServiceError("application status cannot be withdrawn", 1000)
    application.status = "withdrawn"
    application.version += 1
    application.updated_at = utcnow()
    db.add(application)
    db.commit()
    write_system_log(
        db,
        action="application.withdraw",
        actor_id=user.id,
        target_type="application",
        target_id=str(application.id),
    )
    return {
        "id": application.id,
        "application_id": application.id,
        "status": application.status,
        "version": application.version,
    }


def soft_delete_application(db: Session, user: User, application_id: int) -> None:
    application = db.get(Application, application_id)
    if not application or application.is_deleted:
        raise ServiceError("resource not found", 1002)
    if user.role == ROLE_STUDENT:
        if application.applicant_id != user.id:
            raise ServiceError("permission denied", 1003)
        if application.status not in EDITABLE_APPLICATION_STATUSES:
            raise ServiceError("application status cannot be deleted", 1000)
    elif user.role not in {"admin"}:
        raise ServiceError("permission denied", 1003)
    application.is_deleted = True
    application.deleted_at = utcnow()
    application.version += 1
    should_recalculate = bool(application.actual_score_recorded)
    student_id = application.applicant_id
    application.actual_score_recorded = False
    db.add(application)
    if should_recalculate:
        db.flush()
        recalculate_student_score(db, student_id)
    db.commit()
    write_system_log(
        db,
        action="application.delete",
        actor_id=user.id,
        target_type="application",
        target_id=str(application.id),
    )


def get_application_attachments(db: Session, application_id: int) -> list[dict]:
    rows = db.exec(
        select(ApplicationAttachment, FileInfo, FileAnalysisResult)
        .join(FileInfo, ApplicationAttachment.file_id == FileInfo.id)
        .outerjoin(FileAnalysisResult, FileAnalysisResult.file_id == FileInfo.id)
        .where(ApplicationAttachment.application_id == application_id, FileInfo.status != "deleted")
    ).all()
    return [serialize_file(file, analysis=analysis) for _, file, analysis in rows]


def _replace_attachments(
    db: Session,
    user: User,
    application_id: int,
    file_ids: list[str],
    *,
    auto_commit: bool = True,
) -> None:
    old_rows = db.exec(select(ApplicationAttachment).where(ApplicationAttachment.application_id == application_id)).all()
    for row in old_rows:
        db.delete(row)
    for file_id in dict.fromkeys(file_ids):
        file = db.get(FileInfo, file_id)
        if not file or file.status == "deleted":
            raise ServiceError(f"attachment not found: {file_id}", 1002)
        if file.uploader_id != user.id:
            raise ServiceError("attachment owner mismatch", 1003)
        db.add(ApplicationAttachment(application_id=application_id, file_id=file_id))
    if auto_commit:
        db.commit()


def _upsert_ai_report(db: Session, application_id: int, *, auto_commit: bool = True) -> None:
    report = db.exec(select(AIAuditReport).where(AIAuditReport.application_id == application_id)).first()
    now = utcnow()
    if report:
        report.status = "queued"
        report.result = None
        report.error_message = None
        report.updated_at = now
        report.audited_at = None
        db.add(report)
    else:
        db.add(AIAuditReport(application_id=application_id, status="queued", updated_at=now))
    if auto_commit:
        db.commit()


def _get_award(db: Session, award_uid: int) -> AwardDict:
    award = db.exec(select(AwardDict).where(AwardDict.award_uid == award_uid, AwardDict.is_active.is_(True))).first()
    if not award:
        raise ServiceError("award_uid not found", 1001)
    return award


def _resolve_score(score: float | None, max_score: float, default_score: float) -> float:
    if score is None:
        return float(default_score)
    if score > max_score:
        raise ServiceError("score exceeds max_score", 1001)
    return float(score)


def _validate_score_category(category: str, sub_type: str) -> None:
    if not is_valid_score_category(category, sub_type):
        raise ServiceError("invalid category/sub_type", 1001)


def _normalize_application_tags(tags: list[str] | None) -> list[str]:
    normalized = []
    for tag in tags or []:
        value = str(tag or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized[:8]


def _require_student(user: User) -> None:
    if user.role != ROLE_STUDENT:
        raise ServiceError("permission denied", 1003)


def _get_owned_application(db: Session, user: User, application_id: int) -> Application:
    application = db.get(Application, application_id)
    if not application or application.is_deleted:
        raise ServiceError("resource not found", 1002)
    if application.applicant_id != user.id:
        raise ServiceError("permission denied", 1003)
    return application


def _get_viewable_application(db: Session, user: User, application_id: int) -> Application:
    application = db.get(Application, application_id)
    if not application or application.is_deleted:
        raise ServiceError("resource not found", 1002)
    if user.role in {"teacher", "admin"}:
        return application
    if application.applicant_id == user.id:
        return application
    if user.role == ROLE_STUDENT and user.is_reviewer:
        reviewer_class_ids = get_active_reviewer_class_ids(db, user)
        applicant = db.get(User, application.applicant_id)
        if applicant and applicant.class_id in reviewer_class_ids and application.status in REVIEWER_REVIEWABLE_STATUSES:
            return application
    raise ServiceError("permission denied", 1003)
