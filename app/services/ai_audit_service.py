from difflib import SequenceMatcher
from pathlib import Path

from sqlmodel import Session, select

from app.core.award_catalog import find_award_rule
from app.core.config import settings
from app.core.constants import (
    APPLICATION_STATUS_APPROVED,
    APPLICATION_STATUS_ARCHIVED,
    APPLICATION_STATUS_AI_ABNORMAL,
    APPLICATION_STATUS_PENDING_REVIEW,
    MANAGE_REVIEW_ROLES,
    ROLE_STUDENT,
)
from app.core.utils import json_dumps, utcnow
from app.models.ai_audit_report import AIAuditReport
from app.models.application import Application
from app.models.application_attachment import ApplicationAttachment
from app.models.award_dict import AwardDict
from app.models.file_analysis_result import FileAnalysisResult
from app.models.file_info import FileInfo
from app.models.user import User
from app.services.errors import ServiceError
from app.services.file_analysis_service import analyze_file, get_file_analysis_payload
from app.services.reviewer_scope_service import get_active_reviewer_class_ids
from app.services.serializers import serialize_ai_audit
from app.services.system_log_service import write_system_log

TITLE_HINTS = ("证书", "证明", "获奖", "奖", "竞赛", "荣誉", "表彰", "志愿", "创新", "一等奖", "二等奖", "三等奖")


def get_ai_report(db: Session, user: User, application_id: int) -> dict:
    application = db.get(Application, application_id)
    if not application or application.is_deleted:
        raise ServiceError("resource not found", 1002)

    if user.role in MANAGE_REVIEW_ROLES:
        pass
    elif user.role == ROLE_STUDENT:
        if application.applicant_id != user.id:
            reviewer_class_ids = get_active_reviewer_class_ids(db, user)
            if not reviewer_class_ids:
                raise ServiceError("permission denied", 1003)
            applicant = db.get(User, application.applicant_id)
            if not applicant or applicant.class_id not in reviewer_class_ids:
                raise ServiceError("permission denied", 1003)
    else:
        raise ServiceError("permission denied", 1003)

    report = db.exec(select(AIAuditReport).where(AIAuditReport.application_id == application_id)).first()
    if not report:
        raise ServiceError("ai report not found", 1002)
    return serialize_ai_audit(report)


def get_ai_logs(db: Session, user: User, *, result: str | None, page: int, size: int) -> dict:
    if user.role not in {"teacher", "admin"}:
        raise ServiceError("permission denied", 1003)
    stmt = select(AIAuditReport)
    if result:
        stmt = stmt.where(AIAuditReport.result == result)
    rows = db.exec(stmt.order_by(AIAuditReport.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    total = len(db.exec(stmt).all())
    return {
        "page": page,
        "size": size,
        "total": total,
        "list": [serialize_ai_audit(row) for row in rows],
    }


def run_ai_audit(db: Session, application_id: int) -> None:
    application = db.get(Application, application_id)
    if not application or application.is_deleted:
        return
    applicant = db.get(User, application.applicant_id)
    original_status = application.status
    preserve_status = original_status in {APPLICATION_STATUS_APPROVED, APPLICATION_STATUS_ARCHIVED}
    report = db.exec(select(AIAuditReport).where(AIAuditReport.application_id == application_id)).first()
    if not report:
        report = AIAuditReport(application_id=application_id, provider=settings.ai_audit_provider, status="queued")
        db.add(report)
        db.commit()
        db.refresh(report)

    try:
        report.status = "running"
        report.updated_at = utcnow()
        db.add(report)
        db.commit()

        attachment_rows = db.exec(
            select(ApplicationAttachment, FileInfo, FileAnalysisResult)
            .join(FileInfo, ApplicationAttachment.file_id == FileInfo.id)
            .outerjoin(FileAnalysisResult, FileAnalysisResult.file_id == FileInfo.id)
            .where(ApplicationAttachment.application_id == application_id, FileInfo.status != "deleted")
        ).all()
        attachment_count = len(attachment_rows)
        award = db.exec(select(AwardDict).where(AwardDict.award_uid == application.award_uid)).first()
        risk_points = []
        warning_points = []
        result = "pass"
        next_status = APPLICATION_STATUS_PENDING_REVIEW
        if attachment_count == 0:
            risk_points.append("缺少证明附件")
            result = "abnormal"
            next_status = APPLICATION_STATUS_AI_ABNORMAL
        elif "异常" in application.title or "异常" in application.description:
            risk_points.append("标题或描述触发人工复核规则")
            result = "abnormal"
            next_status = APPLICATION_STATUS_AI_ABNORMAL

        attachment_analysis = []
        for _, file, analysis in attachment_rows:
            if not analysis or analysis.status != "completed":
                analysis = analyze_file(db, file, uploader=applicant)
            attachment_analysis.append(
                {
                    "file_id": file.id,
                    "filename": file.original_name,
                    "analysis": analysis,
                    "payload": get_file_analysis_payload(analysis),
                }
            )

        combined_ocr_text = _build_report_ocr_summary(attachment_analysis)

        identity_check = _build_identity_check(applicant, attachment_analysis)
        consistency_check = _build_consistency_check(application, award, attachment_analysis, applicant=applicant)
        risk_points.extend(consistency_check["critical_risks"])
        warning_points.extend(consistency_check["warning_risks"])
        if identity_check["status"] == "mismatch":
            warning_points.append("附件未识别到申请人姓名")
            result = "abnormal"
            next_status = APPLICATION_STATUS_AI_ABNORMAL
        if consistency_check["payload"]["title_check"]["status"] == "mismatch":
            result = "abnormal"
            next_status = APPLICATION_STATUS_AI_ABNORMAL
        if consistency_check["payload"]["level_check"]["status"] == "mismatch":
            result = "abnormal"
            next_status = APPLICATION_STATUS_AI_ABNORMAL

        report.provider = settings.ai_audit_provider
        report.status = "completed"
        report.result = result
        report.ocr_text = combined_ocr_text
        report.identity_check_json = json_dumps(identity_check)
        report.consistency_check_json = json_dumps(consistency_check["payload"])
        report.risk_points_json = json_dumps(risk_points + warning_points)
        report.score_breakdown_json = json_dumps(_build_score_breakdown(application, award))
        report.item_score = application.item_score
        report.total_score = application.total_score
        report.error_message = None
        report.updated_at = utcnow()
        report.audited_at = utcnow()

        final_application_status = original_status if preserve_status else next_status
        application.status = final_application_status
        application.updated_at = utcnow()
        db.add(report)
        db.add(application)
        db.commit()
        write_system_log(
            db,
            action="ai_audit.complete",
            target_type="application",
            target_id=str(application_id),
            detail={"result": result, "status": final_application_status},
        )
    except Exception as exc:
        report.status = "failed"
        report.result = "error"
        report.error_message = str(exc)
        report.updated_at = utcnow()
        db.add(report)
        if settings.ai_audit_fallback_to_manual and not preserve_status:
            application.status = APPLICATION_STATUS_PENDING_REVIEW
            application.updated_at = utcnow()
            db.add(application)
        db.commit()
        write_system_log(
            db,
            action="ai_audit.failed",
            target_type="application",
            target_id=str(application_id),
            detail={"error": str(exc)},
        )


def _build_identity_check(applicant: User | None, attachment_analysis: list[dict]) -> dict:
    expected_name = applicant.name if applicant else None
    expected_candidates = [value for value in (applicant.name if applicant else None, applicant.account if applicant else None) if value]
    file_checks = []
    matched_fields = []
    recognized_name_candidates = []
    any_completed = False
    any_match = False
    for item in attachment_analysis:
        analysis = item["analysis"]
        payload = item["payload"]
        name_check = payload.get("uploader_name_match", {})
        if analysis and analysis.status == "completed":
            any_completed = True
        matched = bool(name_check.get("matched"))
        if matched:
            any_match = True
            matched_fields.append(item["file_id"])
        for candidate in name_check.get("recognized_name_candidates", []):
            if candidate not in recognized_name_candidates:
                recognized_name_candidates.append(candidate)
        file_checks.append(
            {
                "file_id": item["file_id"],
                "filename": item["filename"],
                "matched": name_check.get("matched"),
                "matches": name_check.get("matches", []),
                "expected_candidates": name_check.get("expected_candidates", []),
                "recognized_name_candidates": name_check.get("recognized_name_candidates", []),
                "matched_page_indexes": name_check.get("matched_page_indexes", []),
                "status": analysis.status if analysis else "missing",
            }
        )
    status = "matched" if any_match else "mismatch" if any_completed else "unknown"
    return {
        "expected_name": expected_name,
        "expected_candidates": expected_candidates,
        "matched": any_match,
        "status": status,
        "matched_fields": matched_fields,
        "recognized_name_candidates": recognized_name_candidates,
        "files": file_checks,
    }


def _build_consistency_check(
    application: Application,
    award: AwardDict | None,
    attachment_analysis: list[dict],
    *,
    applicant: User | None,
) -> dict:
    participation_only = _uses_participation_light_check(application, award)
    document_titles = []
    ocr_texts = []
    recognized_levels = []
    seal_detected = False
    signature_detected = False
    file_checks = []
    any_completed = False
    applicant_page_indexes_by_file: dict[str, list[int]] = {}
    located_applicant_page = False

    for item in attachment_analysis:
        analysis = item["analysis"]
        payload = item["payload"]
        if analysis and analysis.status == "completed":
            any_completed = True
        all_pages = _all_payload_pages(payload)
        selected_pages = _select_applicant_pages(payload, applicant)
        title_pages = _select_title_pages(payload, application, award)
        selected_page_indexes = [page["page_index"] for page in selected_pages]
        title_page_indexes = [page["page_index"] for page in title_pages]
        if selected_page_indexes:
            located_applicant_page = True
        applicant_page_indexes_by_file[item["file_id"]] = selected_page_indexes

        content_pages = _dedupe_pages(selected_pages + title_pages) or all_pages
        content_page_index_set = {page["page_index"] for page in content_pages}
        file_page_index_set = {page["page_index"] for page in all_pages}
        if content_pages:
            for page in content_pages:
                page_text = page["text"]
                if page_text:
                    ocr_texts.append(page_text)
                document_title = _extract_document_title_from_text(page_text, item["filename"])
                if document_title:
                    document_titles.append(document_title)
                for level in _extract_levels(page_text, document_title):
                    if level not in recognized_levels:
                        recognized_levels.append(level)
            for page in all_pages:
                for level in _extract_levels(page["text"]):
                    if level not in recognized_levels:
                        recognized_levels.append(level)
                if page["text"]:
                    ocr_texts.append(page["text"])
            evidence_page_index_set = file_page_index_set or content_page_index_set
            seal_detected = seal_detected or _page_item_detected(payload.get("seal", {}), evidence_page_index_set)
            signature_detected = signature_detected or _page_item_detected(payload.get("signature", {}), evidence_page_index_set)
        else:
            document_title = payload.get("document_title")
            if document_title:
                document_titles.append(document_title)
            if analysis and analysis.status == "completed" and analysis.ocr_text:
                ocr_texts.append(analysis.ocr_text)
            for level in payload.get("recognized_levels", []):
                if level not in recognized_levels:
                    recognized_levels.append(level)
            seal_detected = seal_detected or bool(payload.get("seal", {}).get("detected"))
            signature_detected = signature_detected or bool(payload.get("signature", {}).get("detected"))
        filename_similarity = _text_similarity(Path(item["filename"]).stem, application.title)
        file_checks.append(
            {
                "file_id": item["file_id"],
                "filename": item["filename"],
                "matched": filename_similarity >= 0.62,
                "similarity": filename_similarity,
                "matched_page_indexes": selected_page_indexes,
                "matched_title_page_indexes": title_page_indexes,
            }
        )

    combined_ocr_text = "\n".join(ocr_texts)
    title_full_text_hit = _contains_normalized(combined_ocr_text, application.title)
    title_score = max((_text_similarity(title, application.title) for title in document_titles), default=0.0)
    if title_full_text_hit:
        title_score = max(title_score, 1.0)
    title_status = "matched" if title_score >= 0.72 else "mismatch" if document_titles or ocr_texts else "unknown"

    expected_levels = _extract_levels(application.title, application.description, award.award_name if award else "")
    if participation_only:
        level_status = "skipped"
    elif expected_levels and recognized_levels:
        level_status = "matched" if set(expected_levels) & set(recognized_levels) else "mismatch"
    elif expected_levels:
        level_status = "unknown"
    else:
        level_status = "matched"

    filename_status = "matched" if any(item["matched"] for item in file_checks) else "mismatch" if file_checks else "unknown"
    seal_status = "skipped" if participation_only else "matched" if seal_detected else "mismatch" if any_completed else "unknown"
    signature_status = (
        "skipped" if participation_only else "matched" if signature_detected else "mismatch" if any_completed else "unknown"
    )

    critical_risks = []
    warning_risks = []
    if _has_identity_candidates(applicant) and any_completed and not located_applicant_page:
        warning_risks.append("未定位到包含申请人的证书页")
    if title_status == "mismatch":
        critical_risks.append("附件 OCR 内容与申报标题不一致")
    if level_status == "mismatch":
        critical_risks.append("附件识别级别与申报内容不一致")
    if filename_status == "mismatch":
        warning_risks.append("上传文件名与申报标题相似度较低")
    if seal_status == "mismatch":
        warning_risks.append("未提取到印章区域")
    if signature_status == "mismatch":
        warning_risks.append("未提取到落款或签字区域")

    payload = {
        "audit_mode": "participation_only" if participation_only else "standard",
        "applicant_page_indexes_by_file": applicant_page_indexes_by_file,
        "matched": not critical_risks,
        "diff": critical_risks + warning_risks,
        "title_check": {
            "status": title_status,
            "expected": application.title,
            "recognized_titles": document_titles,
            "best_similarity": title_score,
            "full_text_hit": title_full_text_hit,
        },
        "level_check": {
            "status": level_status,
            "expected": expected_levels,
            "recognized": recognized_levels,
        },
        "filename_check": {
            "status": filename_status,
            "expected": application.title,
            "files": file_checks,
        },
        "seal_check": {
            "status": seal_status,
            "detected": seal_detected,
        },
        "signature_check": {
            "status": signature_status,
            "detected": signature_detected,
        },
    }
    return {
        "payload": payload,
        "critical_risks": critical_risks,
        "warning_risks": warning_risks,
    }


def _uses_participation_light_check(application: Application, award: AwardDict | None) -> bool:
    rule = find_award_rule(application.award_uid)
    texts = [
        application.title,
        application.description,
        award.award_name if award else "",
        rule.get("rule_name") if rule else "",
        rule.get("rule_path") if rule else "",
    ]
    haystack = "\n".join(filter(None, texts))
    return "参与未获奖" in haystack or ("参与" in haystack and "未获奖" in haystack)


def _select_applicant_pages(payload: dict, applicant: User | None) -> list[dict]:
    pages = payload.get("pages") or []
    if not isinstance(pages, list):
        return []
    candidates = _identity_candidates(applicant)
    selected = []
    for fallback_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_text = _page_text(page)
        page_index = _page_index(page, fallback_index)
        if not candidates:
            selected.append({"page_index": page_index, "text": page_text})
            continue
        normalized_page_text = _normalize_text(page_text)
        if not any(candidate in normalized_page_text for candidate in candidates):
            continue
        selected.append(
            {
                "page_index": page_index,
                "text": _applicant_context_text(page, candidates) or page_text,
            }
        )
    return selected


def _select_title_pages(payload: dict, application: Application, award: AwardDict | None) -> list[dict]:
    pages = payload.get("pages") or []
    if not isinstance(pages, list):
        return []
    expected_texts = [
        application.title,
        application.description,
        award.award_name if award else None,
    ]
    rule = find_award_rule(application.award_uid)
    if rule:
        expected_texts.extend([rule.get("rule_name"), rule.get("rule_path")])
    candidates = [
        normalized
        for value in expected_texts
        if isinstance(value, str) and len(normalized := _normalize_text(value)) >= 4
    ]
    if not candidates:
        return []

    selected = []
    for fallback_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_text = _page_text(page)
        normalized_page_text = _normalize_text(page_text)
        if any(candidate in normalized_page_text for candidate in candidates):
            selected.append({"page_index": _page_index(page, fallback_index), "text": page_text})
            continue
        lines = [line.get("text") or "" for line in page.get("lines", []) if isinstance(line, dict)]
        if any(_line_matches_expected_title(line, candidates) for line in lines):
            selected.append({"page_index": _page_index(page, fallback_index), "text": page_text})
    return selected


def _line_matches_expected_title(line: str, normalized_candidates: list[str]) -> bool:
    normalized_line = _normalize_text(line)
    if len(normalized_line) < 4:
        return False
    return any(
        normalized_line in candidate
        or candidate in normalized_line
        or _text_similarity(normalized_line, candidate) >= 0.72
        for candidate in normalized_candidates
    )


def _dedupe_pages(pages: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for page in pages:
        page_index = page.get("page_index")
        if page_index in seen:
            continue
        seen.add(page_index)
        result.append(page)
    return result


def _all_payload_pages(payload: dict) -> list[dict]:
    pages = payload.get("pages") or []
    if not isinstance(pages, list):
        return []
    selected = []
    for fallback_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        selected.append({"page_index": _page_index(page, fallback_index), "text": _page_text(page)})
    return selected


def _build_report_ocr_summary(attachment_analysis: list[dict]) -> str:
    summaries = []
    for item in attachment_analysis:
        analysis = item.get("analysis")
        payload = item.get("payload") or {}
        if not analysis or analysis.status != "completed":
            continue
        summary = payload.get("ocr_summary")
        if isinstance(summary, str) and summary.strip():
            summaries.append(summary.strip())
            continue
        if analysis.ocr_text:
            summaries.append(_compact_ocr_text(analysis.ocr_text))
    return "\n".join(summary for summary in summaries if summary).strip()


def _compact_ocr_text(text: str, *, max_length: int = 650) -> str:
    lines = []
    seen = set()
    for line in text.splitlines():
        value = line.strip()
        normalized = _normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        lines.append(value)
        if len("；".join(lines)) >= max_length:
            break
    summary = "；".join(lines)
    if len(summary) <= max_length:
        return summary
    return summary[: max_length - 1].rstrip("；;，,。 ") + "…"


def _identity_candidates(applicant: User | None) -> list[str]:
    if not applicant:
        return []
    raw_candidates = [applicant.name, applicant.account]
    return [normalized for value in raw_candidates if (normalized := _normalize_text(value or ""))]


def _has_identity_candidates(applicant: User | None) -> bool:
    return bool(_identity_candidates(applicant))


def _page_text(page: dict) -> str:
    text = page.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return "\n".join(line.get("text") or "" for line in page.get("lines", []) if isinstance(line, dict)).strip()


def _page_index(page: dict, fallback_index: int) -> int:
    try:
        return int(page.get("page_index", fallback_index))
    except (TypeError, ValueError):
        return fallback_index


def _applicant_context_text(page: dict, normalized_candidates: list[str]) -> str:
    lines = [line for line in page.get("lines", []) if isinstance(line, dict) and (line.get("text") or "").strip()]
    matched_rects = []
    for line in lines:
        normalized_line = _normalize_text(line.get("text") or "")
        if any(candidate in normalized_line for candidate in normalized_candidates):
            rect = _box_to_rect(line.get("box"))
            if rect:
                matched_rects.append(rect)

    if matched_rects:
        page_height = float(page.get("height") or 0.0)
        if page_height <= 0:
            page_height = max(((_box_to_rect(line.get("box")) or [0, 0, 0, 0])[3] for line in lines), default=0.0)
        vertical_band = max(260.0, page_height * 0.28) if page_height > 0 else 320.0
        selected_lines = []
        for line in lines:
            rect = _box_to_rect(line.get("box"))
            if not rect:
                continue
            line_center = (rect[1] + rect[3]) / 2
            if any(abs(line_center - ((matched[1] + matched[3]) / 2)) <= vertical_band for matched in matched_rects):
                selected_lines.append(line.get("text") or "")
        if selected_lines:
            return "\n".join(selected_lines).strip()

    page_text = _page_text(page)
    normalized_page = _normalize_text(page_text)
    for candidate in normalized_candidates:
        index = normalized_page.find(candidate)
        if index >= 0:
            return _text_window_around_candidate(page_text, candidate)
    return page_text


def _text_window_around_candidate(text: str, normalized_candidate: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if normalized_candidate in _normalize_text(line):
            start = max(0, index - 8)
            end = min(len(lines), index + 9)
            return "\n".join(lines[start:end]).strip()
    return text


def _extract_document_title_from_text(text: str, fallback_filename: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    for index, line in enumerate([item.strip() for item in text.splitlines() if item.strip()][:20]):
        if len(line) < 4:
            continue
        if _looks_like_document_title_noise(line):
            continue
        score = min(len(line), 30)
        if any(hint in line for hint in TITLE_HINTS):
            score += 40
        if any(char.isdigit() for char in line):
            score -= 18
        score -= index * 2
        candidates.append((score, index, line))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]
    return Path(fallback_filename).stem


def _looks_like_document_title_noise(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) > 42:
        return True
    return any(hint in stripped for hint in ("研究会", "协会", "委员会", "组委会", "教育研究会", "教育研")) and not any(
        hint in stripped for hint in ("荣获", "参赛", "获奖", "证书", "证明")
    )


def _page_item_detected(payload: dict, page_indexes: set[int]) -> bool:
    if not page_indexes:
        return False
    for item in payload.get("items", []):
        try:
            item_page_index = int(item.get("page_index"))
        except (TypeError, ValueError):
            continue
        if item_page_index in page_indexes:
            return True
    return False


def _box_to_rect(box) -> list[float] | None:
    if not box:
        return None
    if isinstance(box, list) and len(box) == 4 and all(isinstance(item, (int, float)) for item in box):
        return [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
    if isinstance(box, list):
        xs = []
        ys = []
        for point in box:
            if isinstance(point, list) and len(point) >= 2:
                xs.append(float(point[0]))
                ys.append(float(point[1]))
        if xs and ys:
            return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _build_score_breakdown(application: Application, award: AwardDict | None) -> list[dict]:
    rule = find_award_rule(application.award_uid)
    rule_name = None
    if rule:
        rule_name = rule.get("rule_name") or rule.get("rule_path")
    if not rule_name and award:
        rule_name = award.award_name
    if not rule_name:
        rule_name = f"奖项 {application.award_uid}"

    max_score = application.item_score
    if rule and rule.get("max_score") is not None:
        max_score = rule["max_score"]
    elif award:
        max_score = award.max_score

    return [
        {
            "rule_code": f"AWARD_UID_{application.award_uid}",
            "rule_name": rule_name,
            "category": rule.get("category") if rule else application.category,
            "sub_type": rule.get("sub_type") if rule else application.sub_type,
            "score": application.item_score,
            "max_score": max_score,
        }
    ]


def _extract_levels(*texts: str) -> list[str]:
    keywords = []
    haystack = "\n".join(filter(None, texts))
    for item in ("国际级", "国家级", "省级", "市级", "校级", "院级", "一等奖", "二等奖", "三等奖", "特等奖"):
        if item in haystack and item not in keywords:
            keywords.append(item)
    return keywords


def _text_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return 1.0
    return round(SequenceMatcher(None, normalized_left, normalized_right).ratio(), 4)


def _contains_normalized(left: str, right: str) -> bool:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    return bool(normalized_left and normalized_right and normalized_right in normalized_left)


def _normalize_text(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
