from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from sqlmodel import Session, select

from app.core.cache import set_json
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import get_engine
from app.core.term_utils import apply_datetime_term_filter
from app.core.utils import json_loads, utcnow
from app.models.application import Application
from app.models.email_record import EmailRecord
from app.models.export_task import ExportTask
from app.models.file_info import FileInfo
from app.models.user import User
from app.services.class_service import get_class_grade, get_class_ids_by_grade, is_graduating_class
from app.services.export_workbook_utils import EXPORT_SCORE_COLUMNS, autosize_workbook_columns, build_score_export_columns
from app.services.score_summary_service import get_student_score_summary_map, serialize_score_summary

PENDING_STATUSES = {"pending_ai", "pending_review", "ai_abnormal"}


def enqueue_ai_audit(application_id: int) -> None:
    run_ai_audit_task.delay(application_id)


def enqueue_file_analysis(file_id: str, uploader_id: int | None = None) -> None:
    run_file_analysis_task.delay(file_id, uploader_id)


def enqueue_auto_fill_job(job_id: str, user_id: int, file_ids: list[str]) -> None:
    run_auto_fill_task.delay(job_id, user_id, file_ids)


def enqueue_email_job(email_id: int) -> None:
    send_email_task.delay(email_id)


def enqueue_export_job(task_id: str) -> None:
    generate_export_task.delay(task_id)


@celery_app.task(name="app.tasks.jobs.run_ai_audit_task")
def run_ai_audit_task(application_id: int) -> None:
    from app.services.ai_audit_service import run_ai_audit

    with Session(get_engine()) as db:
        run_ai_audit(db, application_id)


@celery_app.task(name="app.tasks.jobs.run_file_analysis_task")
def run_file_analysis_task(file_id: str, uploader_id: int | None = None) -> None:
    from app.services.file_analysis_service import analyze_file

    with Session(get_engine()) as db:
        file = db.get(FileInfo, file_id)
        if not file or file.status == "deleted":
            return
        uploader = db.get(User, uploader_id) if uploader_id else None
        analyze_file(db, file, uploader=uploader)


@celery_app.task(name="app.tasks.jobs.run_auto_fill_task")
def run_auto_fill_task(job_id: str, user_id: int, file_ids: list[str]) -> None:
    from app.services.auto_fill_service import run_auto_fill_job

    with Session(get_engine()) as db:
        run_auto_fill_job(db, job_id, user_id, file_ids)


@celery_app.task(name="app.tasks.jobs.send_email_task")
def send_email_task(email_id: int) -> None:
    with Session(get_engine()) as db:
        record = db.get(EmailRecord, email_id)
        if not record:
            return
        if settings.email_mock_success:
            record.status = "mock_sent"
            record.error_message = None
        else:
            record.status = "failed"
            record.error_message = "mock provider configured to fail"
        record.sent_at = utcnow()
        db.add(record)
        db.commit()


@celery_app.task(name="app.tasks.jobs.generate_export_task")
def generate_export_task(task_id: str) -> None:
    with Session(get_engine()) as db:
        task = db.exec(select(ExportTask).where(ExportTask.task_id == task_id)).first()
        if not task:
            return

        task.status = "running"
        task.error_message = None
        db.add(task)
        db.commit()
        _write_export_cache(task)

        try:
            filters = json_loads(task.filters_json, {})
            rows = _query_export_rows(db, filters)

            settings.export_dir_path.mkdir(parents=True, exist_ok=True)
            file_name = _build_export_file_name(task, filters)
            file_path = settings.export_dir_path / file_name

            if task.scope == "teacher_statistics":
                _build_student_statistics_workbook(db, rows, file_path, filters)
            else:
                _build_application_workbook(rows, file_path)

            options = json_loads(task.options_json, {})
            task.file_name = file_name
            task.file_path = str(file_path)
            task.status = "completed"
            task.completed_at = utcnow()
            task.error_message = None
            db.add(task)
            if options.get("store_to_archive"):
                from app.services.archive_service import create_archive_record_from_task

                create_archive_record_from_task(db, task)
            else:
                db.commit()
            _write_export_cache(task)
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
            task.completed_at = utcnow()
            db.add(task)
            db.commit()
            _write_export_cache(task)
            raise


def _query_export_rows(db: Session, filters: dict) -> list[tuple[Application, User]]:
    stmt = select(Application, User).join(User, Application.applicant_id == User.id).where(Application.is_deleted.is_(False))
    stmt = apply_datetime_term_filter(stmt, Application.created_at, filters.get("term") or settings.default_term)
    if filters.get("class_id"):
        stmt = stmt.where(User.class_id == int(filters["class_id"]))
    if filters.get("grade"):
        class_ids = get_class_ids_by_grade(db, int(filters["grade"]), include_graduating=False)
        if class_ids:
            stmt = stmt.where(User.class_id.in_(class_ids))
        else:
            stmt = stmt.where(User.class_id == -1)
    graduating_class_ids = [row.class_id for row in db.exec(select(User.class_id).where(User.role == "student")).all() if is_graduating_class(db, row)]
    if graduating_class_ids:
        stmt = stmt.where(User.class_id.notin_(list(dict.fromkeys(graduating_class_ids))))
    if filters.get("status"):
        stmt = stmt.where(Application.status == filters["status"])
    return db.exec(stmt.order_by(Application.created_at.desc())).all()


def _build_export_file_name(task: ExportTask, filters: dict) -> str:
    suffix = (task.format or "xlsx").lower()
    if suffix != "xlsx":
        suffix = "xlsx"
    if task.scope == "teacher_statistics":
        term = str(filters.get("term") or "all")
        grade = str(filters.get("grade") or "all")
        class_id = str(filters.get("class_id") or "all")
        return f"teacher_statistics_{term}_{grade}_{class_id}_{task.task_id}.xlsx"
    return f"{task.task_id}.{suffix}"


def _build_application_workbook(rows: list[tuple[Application, User]], file_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "applications"
    ws.append(
        [
            "application_id",
            "student_name",
            "student_account",
            "class_id",
            "category",
            "sub_type",
            "title",
            "status",
            "score",
        ]
    )
    for application, user in rows:
        ws.append(
            [
                application.id,
                user.name,
                user.account,
                user.class_id,
                application.category,
                application.sub_type,
                application.title,
                application.status,
                application.item_score,
            ]
        )
    autosize_workbook_columns(wb)
    wb.save(file_path)


def _build_student_statistics_workbook(
    db: Session,
    rows: list[tuple[Application, User]],
    file_path: Path,
    filters: dict,
) -> None:
    summary = _aggregate_student_statistics(db, rows, filters)

    wb = Workbook()
    ws = wb.active
    ws.title = "student_statistics"
    ws.append(
        [
            "年级",
            "班级",
            "学生ID",
            "学号",
            "姓名",
            "申报总数",
            "驳回数",
            "待处理数",
            "官方总分",
            "原始总分",
            *[label for label, _ in EXPORT_SCORE_COLUMNS],
            "平均分",
        ]
    )
    for row in summary:
        ws.append(
            [
                row["grade"],
                row["class_id"],
                row["student_id"],
                row["student_account"],
                row["student_name"],
                row["total_count"],
                row["rejected_count"],
                row["pending_count"],
                row["total_score"],
                row["raw_total_score"],
                *[row[key] for _, key in EXPORT_SCORE_COLUMNS],
                row["average_score"],
            ]
        )
    autosize_workbook_columns(wb)
    wb.save(file_path)


def _aggregate_student_statistics(db: Session, rows: list[tuple[Application, User]], filters: dict) -> list[dict]:
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
        for student in _query_export_students(db, filters)
        if student.id is not None
    }
    fallback_factory = lambda: {
            "grade": None,
            "class_id": None,
            "student_id": None,
            "student_account": None,
            "student_name": None,
            "total_count": 0,
            "rejected_count": 0,
            "pending_count": 0,
            "total_score": 0.0,
        }
    for application, user in rows:
        item = summary.setdefault(user.id, fallback_factory())
        item["grade"] = get_class_grade(db, user.class_id)
        item["class_id"] = user.class_id
        item["student_id"] = user.id
        item["student_account"] = user.account
        item["student_name"] = user.name
        item["total_count"] += 1
        if application.status == "rejected":
            item["rejected_count"] += 1
        if application.status in PENDING_STATUSES:
            item["pending_count"] += 1

    score_summary_map = get_student_score_summary_map(db, list(summary.keys()))

    result = []
    for item in summary.values():
        total_count = int(item["total_count"] or 0)
        score_summary = serialize_score_summary(score_summary_map.get(item["student_id"]), student_id=item["student_id"])
        score_columns = build_score_export_columns(score_summary)
        result.append(
            {
                **item,
                "total_score": score_summary["actual_score"],
                "raw_total_score": score_summary["raw_total_score"],
                **score_columns,
                "average_score": round(score_summary["actual_score"] / total_count, 4) if total_count else 0.0,
                "actual_score": score_summary["actual_score"],
            }
        )
    result.sort(key=lambda item: (item["grade"] or 0, item["class_id"] or 0, item["student_account"] or ""))
    return result


def _query_export_students(db: Session, filters: dict) -> list[User]:
    stmt = select(User).where(User.role == "student", User.is_deleted.is_(False))
    if filters.get("class_id"):
        stmt = stmt.where(User.class_id == int(filters["class_id"]))
    if filters.get("grade"):
        class_ids = get_class_ids_by_grade(db, int(filters["grade"]), include_graduating=False)
        stmt = stmt.where(User.class_id.in_(class_ids) if class_ids else User.class_id == -1)
    graduating_class_ids = [
        row.class_id
        for row in db.exec(select(User.class_id).where(User.role == "student")).all()
        if is_graduating_class(db, row)
    ]
    if graduating_class_ids:
        stmt = stmt.where(User.class_id.notin_(list(dict.fromkeys(graduating_class_ids))))
    return db.exec(stmt.order_by(User.class_id.asc(), User.account.asc())).all()


def _write_export_cache(task: ExportTask) -> None:
    set_json(
        f"{settings.export_status_prefix}{task.task_id}",
        {
            "task_id": task.task_id,
            "status": task.status,
            "file_name": task.file_name,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "error_message": task.error_message,
        },
        ttl_seconds=settings.celery_result_expires_seconds,
    )
