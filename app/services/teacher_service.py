from collections import defaultdict

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.award_catalog import serialize_award_rule
from app.core.config import settings
from app.core.constants import (
    MANAGE_REVIEW_ROLES,
    TEACHER_RECHECKABLE_STATUSES,
)
from app.core.term_utils import apply_datetime_term_filter
from app.core.utils import json_loads, utcnow
from app.models.application import Application
from app.models.review_record import ReviewRecord
from app.models.user import User
from app.schemas.review import TeacherRecheckRequest
from app.services.errors import ServiceError
from app.services.class_service import get_class_grade, get_class_ids_by_grade, is_graduating_class
from app.services.notification_service import enqueue_reject_email_for_application
from app.services.score_summary_service import (
    get_student_score_summary_map,
    mark_application_archived_score_recorded,
    mark_application_score_recorded,
    recalculate_student_score,
    serialize_score_summary,
)
from app.services.system_log_service import write_system_log

PENDING_STATUSES = {"pending_ai", "pending_review", "ai_abnormal"}
ARCHIVABLE_STATUSES = {"approved", "rejected"}


def list_teacher_applications(
    db: Session,
    user: User,
    *,
    grade: int | None,
    class_id: int | None,
    status: str | None,
    category: str | None,
    sub_type: str | None,
    keyword: str | None,
    page: int,
    size: int,
) -> dict:
    _require_teacher(user)
    stmt = select(Application, User).join(User, Application.applicant_id == User.id).where(Application.is_deleted.is_(False))
    stmt = apply_datetime_term_filter(stmt, Application.created_at, settings.default_term)
    stmt = stmt.where(User.is_deleted.is_(False))
    if class_id:
        stmt = stmt.where(User.class_id == class_id)
    if grade:
        class_ids = get_class_ids_by_grade(db, grade, include_graduating=False)
        if class_ids:
            stmt = stmt.where(User.class_id.in_(class_ids))
        else:
            stmt = stmt.where(User.class_id == -1)
    graduating_class_ids = [row.class_id for row in db.exec(select(User.class_id).where(User.role == "student")).all() if is_graduating_class(db, row)]
    if graduating_class_ids:
        stmt = stmt.where(User.class_id.notin_(list(dict.fromkeys(graduating_class_ids))))
    if status:
        stmt = stmt.where(Application.status == status)
    if category:
        stmt = stmt.where(Application.category == category)
    if sub_type:
        stmt = stmt.where(Application.sub_type == sub_type)
    if keyword:
        like_value = f"%{keyword}%"
        stmt = stmt.where(or_(Application.title.ilike(like_value), User.name.ilike(like_value), User.account.ilike(like_value)))
    total = db.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = db.exec(stmt.order_by(Application.updated_at.desc()).offset((page - 1) * size).limit(size)).all()
    data = []
    for application, student in rows:
        award_rule = serialize_award_rule(application.award_uid)
        data.append(
            {
                "application_id": application.id,
                "grade": get_class_grade(db, student.class_id),
                "class_id": student.class_id,
                "student_id": student.id,
                "student_account": student.account,
                "student_name": student.name,
                "title": application.title,
                "award_uid": application.award_uid,
                "award_rule": award_rule,
                "award_rule_name": award_rule["rule_name"] if award_rule else None,
                "category": application.category,
                "sub_type": application.sub_type,
                "project": award_rule["rule_name"] if award_rule else f"{application.category} / {application.sub_type}",
                "description": application.description,
                "status": application.status,
                "score": application.item_score,
                "comment": application.comment,
                "tags": _application_tags(application),
                "created_at": application.created_at.isoformat(),
                "updated_at": application.updated_at.isoformat(),
            }
        )
    return {"page": page, "size": size, "total": total, "list": data}


def recheck_application(db: Session, user: User, application_id: int, payload: TeacherRecheckRequest) -> dict:
    _require_teacher(user)
    application = db.get(Application, application_id)
    if not application or application.is_deleted:
        raise ServiceError("application not found", 1002)
    student = db.get(User, application.applicant_id)
    if application.status == "archived":
        if payload.decision != "rejected":
            raise ServiceError("archived application cannot be approved again", 1000)
    elif application.status not in TEACHER_RECHECKABLE_STATUSES:
        raise ServiceError("teacher cannot review this status", 1000)
    application.status = "approved" if payload.decision == "approved" else "rejected"
    mark_application_score_recorded(application)
    application.comment = payload.comment
    if payload.score is not None:
        application.item_score = payload.score
        application.total_score = payload.score
    application.updated_at = utcnow()
    application.version += 1
    db.add(application)

    record = ReviewRecord(
        application_id=application.id,
        reviewer_id=user.id,
        reviewer_role="teacher",
        decision=payload.decision,
        result=application.status,
        comment=payload.comment,
    )
    db.add(record)
    db.flush()
    recalculate_student_score(db, application.applicant_id)
    db.commit()
    if application.status == "rejected" and student and student.email:
        enqueue_reject_email_for_application(db, actor=user, application=application, to_email=student.email)
    write_system_log(
        db,
        action="teacher.recheck",
        actor_id=user.id,
        target_type="application",
        target_id=str(application.id),
        detail={"decision": payload.decision, "score": payload.score},
    )
    return {
        "application_id": application.id,
        "status": application.status,
        "comment": application.comment,
        "updated_at": application.updated_at.isoformat(),
    }


def archive_applications(db: Session, user: User, application_ids: list[int]) -> dict:
    _require_teacher(user)
    unique_ids = list(dict.fromkeys(application_ids))
    archived = []
    skipped = []
    for application_id in unique_ids:
        application = db.get(Application, application_id)
        if not application or application.is_deleted:
            skipped.append({"application_id": application_id, "reason": "not_found"})
            continue
        if application.status == "archived":
            skipped.append({"application_id": application_id, "reason": "already_archived"})
            continue
        if application.status not in ARCHIVABLE_STATUSES:
            skipped.append({"application_id": application_id, "reason": "invalid_status"})
            continue

        previous_status = application.status
        mark_application_archived_score_recorded(application, previous_status)
        application.status = "archived"
        application.updated_at = utcnow()
        application.version += 1
        db.add(application)
        if previous_status == "approved":
            recalculate_student_score(db, application.applicant_id)
        archived.append(application_id)
    db.commit()
    write_system_log(
        db,
        action="teacher.archive",
        actor_id=user.id,
        target_type="application_batch",
        target_id=",".join(str(item) for item in archived),
        detail={"skipped": skipped},
    )
    return {
        "total": len(unique_ids),
        "success_count": len(archived),
        "skipped_count": len(skipped),
        "archived_application_ids": archived,
        "skipped": skipped,
    }


def get_statistics(db: Session, user: User, *, grade: int | None, class_id: int | None) -> dict:
    _require_teacher(user)
    rows = list_teacher_applications(
        db,
        user,
        grade=grade,
        class_id=class_id,
        status=None,
        category=None,
        sub_type=None,
        keyword=None,
        page=1,
        size=10000,
    )["list"]
    by_status = defaultdict(int)
    by_category = defaultdict(int)
    student_ids = []
    for row in rows:
        by_status[row["status"]] += 1
        by_category[row["category"]] += 1
        student_ids.append(row["student_id"])
    score_summary_map = get_student_score_summary_map(db, student_ids)
    unique_student_ids = list(dict.fromkeys(student_ids))
    total_score = sum(float(score_summary_map.get(student_id).actual_score or 0.0) for student_id in unique_student_ids if score_summary_map.get(student_id))
    return {
        "total_count": len(rows),
        "student_count": len(unique_student_ids),
        "status_summary": dict(by_status),
        "category_summary": dict(by_category),
        "total_score": round(total_score, 4),
        "average_score": round(total_score / len(unique_student_ids), 4) if unique_student_ids else 0.0,
    }


def get_class_statistics(db: Session, user: User, *, grade: int | None, class_id: int | None) -> dict:
    _require_teacher(user)
    rows = list_teacher_applications(
        db,
        user,
        grade=grade,
        class_id=class_id,
        status=None,
        category=None,
        sub_type=None,
        keyword=None,
        page=1,
        size=10000,
    )["list"]
    summary: dict[int, dict] = {}
    class_student_ids: dict[int, set[int]] = defaultdict(set)
    for row in rows:
        cid = row["class_id"]
        if cid not in summary:
            summary[cid] = {
                "grade": row["grade"],
                "class_id": cid,
                "total_count": 0,
                "rejected_count": 0,
                "pending_count": 0,
                "total_score": 0.0,
            }
        item = summary[cid]
        class_student_ids[cid].add(row["student_id"])
        item["total_count"] += 1
        if row["status"] == "rejected":
            item["rejected_count"] += 1
        if row["status"] in PENDING_STATUSES:
            item["pending_count"] += 1

    score_summary_map = get_student_score_summary_map(db, [row["student_id"] for row in rows])
    result = []
    for item in summary.values():
        student_ids = class_student_ids.get(item["class_id"], set())
        total_score = sum(
            float(score_summary_map.get(student_id).actual_score or 0.0)
            for student_id in student_ids
            if score_summary_map.get(student_id)
        )
        result.append(
            {
                **item,
                "student_count": len(student_ids),
                "average_score": round(total_score / len(student_ids), 4) if student_ids else 0.0,
                "total_score": round(total_score, 4),
            }
        )
    result.sort(key=lambda item: (item["grade"] or 0, item["class_id"] or 0))
    return {"list": result}


def get_student_statistics(db: Session, user: User, *, grade: int | None, class_id: int | None) -> dict:
    _require_teacher(user)
    students = _query_students_for_statistics(db, grade=grade, class_id=class_id)
    summary: dict[int, dict] = {
        student.id: {
            "grade": get_class_grade(db, student.class_id),
            "class_id": student.class_id,
            "student_id": student.id,
            "student_account": student.account,
            "student_name": student.name,
            "total_count": 0,
            "rejected_count": 0,
            "pending_count": 0,
            "total_score": 0.0,
        }
        for student in students
        if student.id is not None
    }
    rows = list_teacher_applications(
        db,
        user,
        grade=grade,
        class_id=class_id,
        status=None,
        category=None,
        sub_type=None,
        keyword=None,
        page=1,
        size=10000,
    )["list"]
    for row in rows:
        student_id = row["student_id"]
        if student_id not in summary:
            summary[student_id] = {
                "grade": row["grade"],
                "class_id": row["class_id"],
                "student_id": student_id,
                "student_account": row["student_account"],
                "student_name": row["student_name"],
                "total_count": 0,
                "rejected_count": 0,
                "pending_count": 0,
                "total_score": 0.0,
            }
        item = summary[student_id]
        item["total_count"] += 1
        if row["status"] == "rejected":
            item["rejected_count"] += 1
        if row["status"] in PENDING_STATUSES:
            item["pending_count"] += 1

    score_summary_map = get_student_score_summary_map(db, list(summary.keys()))

    result = []
    for item in summary.values():
        total_count = int(item["total_count"] or 0)
        score_summary = serialize_score_summary(score_summary_map.get(item["student_id"]), student_id=item["student_id"])
        result.append(
            {
                **item,
                "total_score": score_summary["actual_score"],
                "raw_total_score": score_summary["raw_total_score"],
                "overflow_score": score_summary["overflow_score"],
                "average_score": round(score_summary["actual_score"] / total_count, 4) if total_count else 0.0,
                "actual_score": score_summary["actual_score"],
                "score_summary": score_summary,
            }
        )
    result.sort(key=lambda item: (item["grade"] or 0, item["class_id"] or 0, item["student_account"] or ""))
    return {"list": result}


def _query_students_for_statistics(db: Session, *, grade: int | None, class_id: int | None) -> list[User]:
    stmt = select(User).where(User.role == "student", User.is_deleted.is_(False))
    if class_id:
        stmt = stmt.where(User.class_id == class_id)
    if grade:
        class_ids = get_class_ids_by_grade(db, grade, include_graduating=False)
        stmt = stmt.where(User.class_id.in_(class_ids) if class_ids else User.class_id == -1)
    graduating_class_ids = [
        row.class_id
        for row in db.exec(select(User.class_id).where(User.role == "student")).all()
        if is_graduating_class(db, row)
    ]
    if graduating_class_ids:
        stmt = stmt.where(User.class_id.notin_(list(dict.fromkeys(graduating_class_ids))))
    return db.exec(stmt.order_by(User.class_id.asc(), User.account.asc())).all()


def _require_teacher(user: User) -> None:
    if user.role not in MANAGE_REVIEW_ROLES:
        raise ServiceError("permission denied", 1003)


def _application_tags(application: Application) -> list[str]:
    tags = json_loads(getattr(application, "tags_json", None), [])
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags if str(tag).strip()]
