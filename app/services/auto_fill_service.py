from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlmodel import Session, select

from app.core.award_catalog import find_award_rule, load_award_rule_map
from app.core.cache import get_json, set_json
from app.core.config import settings
from app.core.constants import ROLE_STUDENT
from app.core.score_rules import is_valid_score_category
from app.core.utils import utcnow
from app.models.award_dict import AwardDict
from app.models.file_analysis_result import FileAnalysisResult
from app.models.file_info import FileInfo
from app.models.user import User
from app.schemas.application import AutoFillConfirmRequest, AutoFillJobCreateRequest
from app.services.application_service import create_application
from app.services.errors import ServiceError
from app.services.file_analysis_service import analyze_file, get_file_analysis_payload
from app.services.serializers import serialize_file_analysis
from app.tasks.jobs import enqueue_auto_fill_job

logger = logging.getLogger(__name__)

AUTO_TAG = "auto"
STATUS_QUEUED = "queued"
STATUS_OCR_RUNNING = "ocr_running"
STATUS_LLM_RUNNING = "llm_running"
STATUS_COMPLETED = "completed"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

SYSTEM_PROMPT = (
    "你是高校综合测评自动填报助手。你会看到学生姓名、证明文件OCR摘要和候选得分规则。"
    "请只根据输入材料生成一条申报预览，不要编造没有证据的事实。"
    "你必须从候选得分规则中选择 award_uid；无法确定时选择最接近项并在 warnings/needs_user_input 中说明。"
    "如果材料同时包含参与证明和后续获奖名单，必须优先依据学生姓名附近的获奖名单上下文判断是否获奖及获奖等级。"
)

LEVEL_HINTS = (
    "国际级",
    "国家级",
    "省级",
    "市级",
    "校级",
    "校区级",
    "院级",
    "书院级",
    "一等奖",
    "二等奖",
    "三等奖",
    "特等奖",
    "优秀奖",
    "第一名",
    "第二名",
    "第三名",
)

SPORT_HINTS = ("体育", "运动", "铁人三项", "跑步", "健身", "体测", "球赛", "篮球", "足球", "排球", "羽毛球", "乒乓球")
ART_HINTS = ("文艺", "艺术", "合唱", "器乐", "才艺", "征文", "演讲", "辩论", "书画", "摄影")
LABOR_HINTS = ("劳动", "志愿", "实践", "宿舍", "生涯发展")
INNOVATION_HINTS = ("创新创业", "科创", "科研", "专利", "论文", "学科竞赛", "挑战杯", "互联网+", "数学建模")
AWARD_EVIDENCE_HINTS = ("获奖", "荣获", "一等奖", "二等奖", "三等奖", "优秀奖", "特等奖", "第一名", "第二名", "第三名")
COMPETITION_HINTS = ("竞赛", "比赛", "大赛", "赛", "名次")
UNIVERSITY_LEVEL_HINTS = ("山东大学", "校级", "校区", "青岛", "体育委员会")
DEPARTMENT_LEVEL_HINTS = ("学院", "书院", "院级")
PROVINCIAL_LEVEL_HINTS = ("省级", "山东省", "省赛")
NATIONAL_LEVEL_HINTS = ("国家级", "全国", "国赛")


def create_auto_fill_job(db: Session, user: User, payload: AutoFillJobCreateRequest) -> dict:
    _require_student(user)
    file_ids = _normalize_file_ids([item.file_id for item in payload.attachments])
    if not file_ids:
        raise ServiceError("attachments are required", 1001)
    files = _get_owned_files(db, user, file_ids)

    job_id = uuid4().hex
    now = utcnow().isoformat()
    job = {
        "job_id": job_id,
        "user_id": user.id,
        "status": STATUS_QUEUED,
        "stage": "queued",
        "file_ids": file_ids,
        "files": [_serialize_job_file(file) for file in files],
        "preview": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    _save_job(job)
    enqueue_auto_fill_job(job_id, user.id, file_ids)
    return _public_job(_load_job(job_id) or job)


def get_auto_fill_job(db: Session, user: User, job_id: str) -> dict:
    _require_student(user)
    job = _get_job_for_user(user, job_id)
    return _public_job(job)


def cancel_auto_fill_job(db: Session, user: User, job_id: str) -> None:
    _require_student(user)
    job = _get_job_for_user(user, job_id)
    job.update(
        {
            "status": STATUS_CANCELED,
            "stage": "canceled",
            "error_message": None,
            "updated_at": utcnow().isoformat(),
        }
    )
    _save_job(job, ttl_seconds=120)


def confirm_auto_fill_job(db: Session, user: User, job_id: str, payload: AutoFillConfirmRequest) -> dict:
    _require_student(user)
    job = _get_job_for_user(user, job_id)
    if job.get("status") not in {STATUS_COMPLETED, STATUS_CONFIRMED}:
        raise ServiceError("auto fill preview is not ready", 1000)

    award = _get_active_award(db, payload.award_uid)
    if _score_requires_user_input(award) and payload.score is None:
        raise ServiceError("score is required for this award rule", 1001)

    data = create_application(db, user, payload, tags=[AUTO_TAG])
    job.update(
        {
            "status": STATUS_CONFIRMED,
            "stage": "confirmed",
            "application_id": data.get("application_id"),
            "updated_at": utcnow().isoformat(),
        }
    )
    _save_job(job)
    return data


def run_auto_fill_job(db: Session, job_id: str, user_id: int, file_ids: list[str]) -> None:
    job = _load_job(job_id)
    if not job or job.get("status") == STATUS_CANCELED:
        return
    user = db.get(User, user_id)
    if not user or user.role != ROLE_STUDENT:
        _fail_job(job_id, "student not found")
        return

    try:
        _update_job(job_id, status=STATUS_OCR_RUNNING, stage="ocr_running", error_message=None)
        files = _get_owned_files(db, user, _normalize_file_ids(file_ids))
        attachment_context = _build_attachment_context(db, user, files)
        if _is_canceled(job_id):
            return

        _update_job(
            job_id,
            status=STATUS_LLM_RUNNING,
            stage="llm_running",
            files=[item["file"] for item in attachment_context],
            ocr_summary=_combined_ocr_summary(attachment_context),
        )
        active_awards = _load_active_awards(db)
        candidates = _build_candidate_awards(attachment_context, active_awards)
        parsed = _call_llm(
            {
                "student": {"name": user.name},
                "files": [_llm_file_payload(item) for item in attachment_context],
                "candidate_awards": candidates,
            }
        )
        preview = _normalize_preview(parsed, candidates=candidates, active_awards=active_awards, attachment_context=attachment_context)
        if _is_canceled(job_id):
            return
        _update_job(
            job_id,
            status=STATUS_COMPLETED,
            stage="completed",
            preview=preview,
            source=preview.get("source"),
            model=preview.get("model"),
            error_message=None,
        )
    except Exception as exc:
        logger.warning("auto fill job failed: %s", exc)
        _fail_job(job_id, str(exc))


def _require_student(user: User) -> None:
    if user.role != ROLE_STUDENT:
        raise ServiceError("permission denied", 1003)


def _normalize_file_ids(file_ids: list[str | None]) -> list[str]:
    result = []
    for file_id in file_ids:
        value = str(file_id or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def _get_owned_files(db: Session, user: User, file_ids: list[str]) -> list[FileInfo]:
    files = []
    for file_id in file_ids:
        file = db.get(FileInfo, file_id)
        if not file or file.status == "deleted":
            raise ServiceError(f"attachment not found: {file_id}", 1002)
        if file.uploader_id != user.id:
            raise ServiceError("attachment owner mismatch", 1003)
        files.append(file)
    return files


def _get_active_award(db: Session, award_uid: int) -> AwardDict:
    award = db.exec(select(AwardDict).where(AwardDict.award_uid == award_uid, AwardDict.is_active.is_(True))).first()
    if not award:
        raise ServiceError("award_uid not found", 1001)
    return award


def _load_active_awards(db: Session) -> dict[int, AwardDict]:
    rows = db.exec(select(AwardDict).where(AwardDict.is_active.is_(True))).all()
    return {int(row.award_uid): row for row in rows}


def _build_attachment_context(db: Session, user: User, files: list[FileInfo]) -> list[dict]:
    context = []
    for file in files:
        analysis = db.exec(select(FileAnalysisResult).where(FileAnalysisResult.file_id == file.id)).first()
        if not analysis or analysis.status != "completed":
            analysis = analyze_file(db, file, uploader=user)
        payload = get_file_analysis_payload(analysis)
        applicant_contexts = _build_applicant_contexts(payload, user)
        context.append(
            {
                "file": _serialize_job_file(file, analysis=analysis),
                "file_record": file,
                "analysis": analysis,
                "payload": payload,
                "applicant_contexts": applicant_contexts,
                "text": _analysis_text(analysis, payload, applicant_contexts=applicant_contexts),
            }
        )
    return context


def _serialize_job_file(file: FileInfo, *, analysis: FileAnalysisResult | None = None) -> dict:
    payload = {
        "file_id": file.id,
        "filename": file.original_name,
        "content_type": file.content_type,
        "size": file.size,
    }
    if analysis:
        payload["analysis_status"] = analysis.status
        payload["analysis"] = serialize_file_analysis(analysis)
    return payload


def _analysis_text(analysis: FileAnalysisResult | None, payload: dict, *, applicant_contexts: list[dict] | None = None) -> str:
    context_text = _format_applicant_contexts(applicant_contexts or [], max_length=2600)
    parts = [
        context_text,
        payload.get("document_title"),
        payload.get("ocr_summary"),
        analysis.ocr_text if analysis else None,
    ]
    return _limit_text("\n".join(str(part).strip() for part in parts if str(part or "").strip()), 6000)


def _combined_ocr_summary(attachment_context: list[dict]) -> str:
    lines = []
    for item in attachment_context:
        file = item["file"]
        text = item.get("text") or ""
        lines.append(f"{file.get('filename')}: {_limit_text(text, 800)}")
    return "\n".join(lines)


def _llm_file_payload(item: dict) -> dict:
    payload = item.get("payload") or {}
    analysis = item.get("analysis")
    return {
        "file_id": item["file"]["file_id"],
        "filename": item["file"]["filename"],
        "analysis_status": analysis.status if analysis else "missing",
        "document_title": payload.get("document_title"),
        "ocr_summary": payload.get("ocr_summary"),
        "recognized_levels": payload.get("recognized_levels", []),
        "uploader_name_match": payload.get("uploader_name_match", {}),
        "applicant_contexts": item.get("applicant_contexts") or [],
        "text": _limit_text(item.get("text") or "", 3500),
    }


def _build_applicant_contexts(payload: dict, user: User) -> list[dict]:
    pages = payload.get("pages") or []
    if not isinstance(pages, list):
        return []
    candidates = [_normalize_text(value) for value in (user.name, user.account) if isinstance(value, str) and _normalize_text(value)]
    if not candidates:
        return []

    contexts = []
    selected_ranges: list[tuple[int, int, int]] = []
    for fallback_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        lines = [str(line.get("text") or "").strip() for line in page.get("lines", []) if isinstance(line, dict) and str(line.get("text") or "").strip()]
        if not lines:
            page_text = str(page.get("text") or "").strip()
            lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        page_index = _coerce_int(page.get("page_index"))
        if page_index is None:
            page_index = fallback_index
        for line_index, line in enumerate(lines):
            normalized_line = _normalize_text(line)
            if not any(candidate in normalized_line for candidate in candidates):
                continue
            start = max(0, line_index - 14)
            end = min(len(lines), line_index + 15)
            if any(page_index == selected_page and start <= selected_end and end >= selected_start for selected_page, selected_start, selected_end in selected_ranges):
                continue
            section_heading = _find_recent_award_heading(lines, line_index)
            context_lines = lines[start:end]
            contexts.append(
                {
                    "page_index": page_index,
                    "line_index": line_index,
                    "section_heading": section_heading,
                    "text": "\n".join(context_lines),
                }
            )
            selected_ranges.append((page_index, start, end))
            if len(contexts) >= 8:
                return contexts
    return contexts


def _find_recent_award_heading(lines: list[str], line_index: int) -> str | None:
    for index in range(line_index, max(-1, line_index - 80), -1):
        line = lines[index].strip()
        if re.search(r"(特等奖|一等奖|二等奖|三等奖|优秀奖)(?:[（(]\d+人[）)])?[:：]?$", line):
            return line
        if re.search(r"(获奖名单|参与名单|参加名单|名单)[:：]?$", line):
            return line
    return None


def _format_applicant_contexts(contexts: list[dict], *, max_length: int) -> str:
    if not contexts:
        return ""
    parts = []
    for context in contexts:
        heading = context.get("section_heading")
        prefix = f"学生姓名附近OCR片段 page={context.get('page_index')}"
        if heading:
            prefix += f" section={heading}"
        parts.append(f"{prefix}:\n{context.get('text') or ''}")
    return _limit_text("\n\n".join(parts), max_length)


def _build_candidate_awards(attachment_context: list[dict], active_awards: dict[int, AwardDict]) -> list[dict]:
    text = "\n".join(
        "\n".join(
            [
                item["file"].get("filename") or "",
                item.get("text") or "",
                " ".join((item.get("payload") or {}).get("recognized_levels") or []),
            ]
        )
        for item in attachment_context
    )
    rules = load_award_rule_map()
    scored = []
    for award_uid, award in active_awards.items():
        rule = rules.get(int(award_uid)) or {
            "award_uid": award_uid,
            "category": award.category,
            "sub_type": award.sub_type,
            "rule_name": award.award_name,
            "rule_path": award.award_name,
            "score": award.score,
            "max_score": award.max_score,
        }
        if not rule.get("category") or not rule.get("sub_type"):
            continue
        scored.append((_rule_match_score(rule, text), int(award_uid), rule, award))
    scored.sort(key=lambda item: (-item[0], item[1]))

    selected = []
    seen = set()
    for score, award_uid, rule, award in scored:
        if score <= 0 and len(selected) >= 24:
            break
        selected.append(_serialize_candidate(rule, award))
        seen.add(award_uid)
        if len(selected) >= 72:
            break
    if len(selected) < 24:
        for _score, award_uid, rule, award in scored:
            if award_uid in seen:
                continue
            selected.append(_serialize_candidate(rule, award))
            if len(selected) >= 24:
                break
    return selected


def _serialize_candidate(rule: dict, award: AwardDict) -> dict:
    return {
        "award_uid": int(award.award_uid),
        "category": rule.get("category") or award.category,
        "sub_type": rule.get("sub_type") or award.sub_type,
        "rule_name": rule.get("rule_name") or award.award_name,
        "rule_path": rule.get("rule_path") or rule.get("rule_name") or award.award_name,
        "score": float(award.score or rule.get("score") or 0.0),
        "max_score": float(award.max_score or rule.get("max_score") or award.score or 0.0),
    }


def _rule_match_score(rule: dict, text: str) -> float:
    haystack = _normalize_text(text)
    if not haystack:
        return 0.0
    score = 0.0
    rule_text = " / ".join([str(rule.get("rule_name") or ""), str(rule.get("rule_path") or "")])
    rule_haystack = _normalize_text(rule_text)
    for segment in _rule_segments(rule_text):
        normalized = _normalize_text(segment)
        if len(normalized) < 2:
            continue
        if normalized in haystack:
            score += min(len(normalized), 18)
    for hint in LEVEL_HINTS:
        if hint in text and hint in rule_text:
            score += 12
    score += _semantic_rule_score(rule, text=text, haystack=haystack, rule_haystack=rule_haystack)
    return score


def _semantic_rule_score(rule: dict, *, text: str, haystack: str, rule_haystack: str) -> float:
    score = 0.0
    category = str(rule.get("category") or "")
    sub_type = str(rule.get("sub_type") or "")
    has_award_evidence = _contains_any(text, AWARD_EVIDENCE_HINTS)
    has_competition_evidence = _contains_any(text, COMPETITION_HINTS)
    has_sport_evidence = _contains_any(text, SPORT_HINTS)
    has_art_evidence = _contains_any(text, ART_HINTS)
    has_labor_evidence = _contains_any(text, LABOR_HINTS)
    has_innovation_evidence = _contains_any(text, INNOVATION_HINTS)

    if has_sport_evidence:
        if category == "physical_mental":
            score += 30
        elif category == "innovation" and not has_innovation_evidence:
            score -= 28
        elif category == "art" and not has_art_evidence:
            score -= 18
        elif category == "labor" and not has_labor_evidence:
            score -= 12

    if has_art_evidence:
        if category == "art":
            score += 30
        elif category == "physical_mental" and not has_sport_evidence:
            score -= 18
        elif category == "innovation" and not has_innovation_evidence:
            score -= 16
        elif category == "labor" and not has_labor_evidence:
            score -= 10

    if has_labor_evidence:
        if category == "labor":
            score += 24
        elif category == "innovation" and not has_innovation_evidence:
            score -= 10

    if has_innovation_evidence and category == "innovation":
        score += 28

    if has_award_evidence:
        if sub_type == "achievement":
            score += 18
        elif sub_type == "basic":
            score -= 8

    if has_sport_evidence and has_competition_evidence and "体育竞赛" in rule_haystack:
        score += 36
    if has_sport_evidence and "体育类集体活动" in rule_haystack and has_award_evidence:
        score -= 18

    if "一等奖" in text and "第一名" in rule_haystack:
        score += 22
    if "二等奖" in text and "第二名" in rule_haystack:
        score += 18
    if "三等奖" in text and "第三名" in rule_haystack:
        score += 18
    if "优秀奖" in text and "其它名次" in rule_haystack:
        score += 10
    if any(hint in text for hint in ("参与未获奖", "未获奖")) and "参与未获奖" in rule_haystack:
        score += 18
    elif has_award_evidence and "参与未获奖" in rule_haystack:
        score -= 24

    applicant_section = _extract_applicant_section_heading(text)
    if applicant_section:
        if "一等奖" in applicant_section:
            score += 34 if ("第一名" in rule_haystack or "一等奖" in rule_haystack) else -8
        if "二等奖" in applicant_section:
            score += 34 if ("第二名" in rule_haystack or "二等奖" in rule_haystack) else -8
        if "三等奖" in applicant_section:
            score += 34 if ("第三名" in rule_haystack or "三等奖" in rule_haystack) else -8
        if "优秀奖" in applicant_section:
            score += 24 if "其它名次" in rule_haystack else -6

    if _contains_any(text, NATIONAL_LEVEL_HINTS) and "国家级" in rule_haystack:
        score += 18
    if _contains_any(text, PROVINCIAL_LEVEL_HINTS) and "省级" in rule_haystack:
        score += 18
    if _contains_any(text, UNIVERSITY_LEVEL_HINTS) and ("校级" in rule_haystack or "校区级" in rule_haystack):
        score += 18
    if _contains_any(text, DEPARTMENT_LEVEL_HINTS) and ("院级" in rule_haystack or "书院级" in rule_haystack):
        score += 8

    if "个人项目" in rule_haystack and not _contains_any(text, ("团队", "集体", "队伍", "小组", "成员")):
        score += 5
    if "集体项目" in rule_haystack and _contains_any(text, ("团队", "集体", "队伍", "小组", "成员")):
        score += 5

    return score


def _extract_applicant_section_heading(text: str) -> str:
    sections = re.findall(r"学生姓名附近OCR片段[^\n]*section=([^\n:：]+)", text)
    return "\n".join(sections[-3:])


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _rule_segments(value: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"[/｜|·,，\s]+", str(value or "")) if segment.strip()]


def _call_llm(prompt_payload: dict) -> dict:
    api_url = settings.auto_fill_llm_api_url or settings.teacher_analysis_llm_api_url or settings.report_story_llm_api_url or settings.evaluation_llm_api_url
    api_key = settings.auto_fill_llm_api_key or settings.teacher_analysis_llm_api_key or settings.report_story_llm_api_key or settings.evaluation_llm_api_key
    model = settings.auto_fill_llm_model or settings.teacher_analysis_llm_model or settings.report_story_llm_model or settings.evaluation_llm_model
    if not api_url or not api_key:
        raise RuntimeError("auto fill LLM is not configured")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(prompt_payload)},
        ],
        "temperature": settings.auto_fill_llm_temperature,
        "max_tokens": settings.auto_fill_llm_max_tokens,
    }
    with httpx.Client(timeout=settings.auto_fill_llm_timeout_seconds) as client:
        response = client.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    parsed = _extract_json_object(_extract_content(data))
    parsed["_model"] = model
    return parsed


def _build_prompt(payload: dict) -> str:
    return (
        "请基于证明材料为学生生成一条综合测评申报预览。\n"
        "输出必须是严格JSON对象，不要Markdown，不要额外解释。\n"
        "字段要求：\n"
        "{\n"
        '  "category": "physical_mental|art|labor|innovation",\n'
        '  "sub_type": "basic|achievement",\n'
        '  "award_uid": 123,\n'
        '  "title": "申报名称",\n'
        '  "description": "申报说明",\n'
        '  "occurred_at": "YYYY-MM-DD或null",\n'
        '  "score": 1.5,\n'
        '  "score_tree_facts": {"依据": "用于选择得分树的信息"},\n'
        '  "confidence": 0.0,\n'
        '  "warnings": ["不确定点"],\n'
        '  "needs_user_input": ["score"]\n'
        "}\n"
        "award_uid 必须来自 candidate_awards。发生日期无法确定时填 null。分数不确定时填 null 并在 needs_user_input 写 score。\n"
        f"当前日期：{date.today().isoformat()}\n\n"
        f"输入数据：{json.dumps(payload, ensure_ascii=False)}"
    )


def _normalize_preview(
    parsed: dict,
    *,
    candidates: list[dict],
    active_awards: dict[int, AwardDict],
    attachment_context: list[dict],
) -> dict:
    candidate_by_uid = {int(item["award_uid"]): item for item in candidates}
    warnings = _string_list(parsed.get("warnings"))
    needs_user_input = _string_list(parsed.get("needs_user_input"))
    award_uid = _coerce_int(parsed.get("award_uid"))
    if award_uid not in candidate_by_uid:
        if candidates:
            award_uid = int(candidates[0]["award_uid"])
            warnings.append("AI 未能稳定匹配得分树，已使用最相近候选项，请人工确认。")
            _append_unique(needs_user_input, "award_uid")
        else:
            raise ValueError("no candidate award rules")
    award = active_awards.get(int(award_uid))
    if not award:
        raise ValueError("selected award_uid is inactive")

    rule = find_award_rule(award_uid) or candidate_by_uid.get(int(award_uid), {})
    category = str(parsed.get("category") or rule.get("category") or award.category or "").strip()
    sub_type = str(parsed.get("sub_type") or rule.get("sub_type") or award.sub_type or "").strip()
    rule_category = str(rule.get("category") or award.category or "").strip()
    rule_sub_type = str(rule.get("sub_type") or award.sub_type or "").strip()
    if category != rule_category or sub_type != rule_sub_type:
        category = rule_category
        sub_type = rule_sub_type
        warnings.append("AI 输出的大类/小类与得分树不一致，已按得分树修正。")
    if not is_valid_score_category(category, sub_type):
        raise ValueError("selected award rule has invalid category/sub_type")

    title = _clean_string(parsed.get("title"), 255) or _fallback_title(attachment_context)
    description = _clean_string(parsed.get("description"), 2000) or _fallback_description(attachment_context)
    occurred_at = _normalize_date(parsed.get("occurred_at"))
    if not occurred_at:
        _append_unique(needs_user_input, "occurred_at")
    score = _normalize_score(parsed.get("score"), award)
    if score is None and _score_requires_user_input(award):
        _append_unique(needs_user_input, "score")
    elif score is None:
        score = float(award.score or 0.0)

    llm_facts = parsed.get("score_tree_facts")
    if not isinstance(llm_facts, (dict, list)):
        llm_facts = {}
    return {
        "category": category,
        "sub_type": sub_type,
        "award_uid": int(award_uid),
        "title": title,
        "description": description,
        "occurred_at": occurred_at,
        "score": score,
        "attachments": [{"file_id": item["file"]["file_id"]} for item in attachment_context],
        "score_tree_facts": {
            "selected_rule": {
                "award_uid": int(award_uid),
                "rule_name": rule.get("rule_name") or award.award_name,
                "rule_path": rule.get("rule_path") or rule.get("rule_name") or award.award_name,
                "score": float(award.score or 0.0),
                "max_score": float(award.max_score or award.score or 0.0),
            },
            "llm_facts": llm_facts,
        },
        "confidence": _normalize_confidence(parsed.get("confidence")),
        "warnings": _dedupe_strings(warnings),
        "needs_user_input": _dedupe_strings(needs_user_input),
        "source": "llm",
        "model": parsed.get("_model"),
    }


def _normalize_score(value: Any, award: AwardDict) -> float | None:
    try:
        if value is None or value == "":
            return None
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return None
    max_score = float(award.max_score or award.score or 0.0)
    if max_score and score > max_score:
        return None
    return score


def _score_requires_user_input(award: AwardDict) -> bool:
    return float(award.score or 0.0) == 0.0 and float(award.max_score or 0.0) > 0.0


def _fallback_title(attachment_context: list[dict]) -> str:
    for item in attachment_context:
        title = (item.get("payload") or {}).get("document_title")
        if title:
            return _clean_string(title, 255)
    first = attachment_context[0]["file"]["filename"] if attachment_context else "自动识别申报"
    return _clean_string(Path(first).stem, 255) or "自动识别申报"


def _fallback_description(attachment_context: list[dict]) -> str:
    summary = _combined_ocr_summary(attachment_context)
    return _limit_text(summary, 2000) or "由 AI 自动填报识别生成，请确认后提交。"


def _normalize_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _extract_content(data: dict) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        content = message.get("content") or first.get("text")
        if isinstance(content, str):
            return content
    content = data.get("content")
    if isinstance(content, str):
        return content
    nested_data = data.get("data")
    if isinstance(nested_data, dict) and isinstance(nested_data.get("content"), str):
        return nested_data["content"]
    return ""


def _extract_json_object(content: str) -> dict:
    if not content:
        raise ValueError("empty llm response")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("llm response must be object")
    return payload


def _get_job_for_user(user: User, job_id: str) -> dict:
    job = _load_job(job_id)
    if not job:
        raise ServiceError("auto fill job not found", 1002)
    if int(job.get("user_id") or 0) != int(user.id or 0):
        raise ServiceError("permission denied", 1003)
    return job


def _job_key(job_id: str) -> str:
    return f"{settings.auto_fill_job_prefix}{job_id}"


def _load_job(job_id: str) -> dict | None:
    payload = get_json(_job_key(job_id))
    return payload if isinstance(payload, dict) else None


def _save_job(job: dict, *, ttl_seconds: int | None = None) -> None:
    set_json(_job_key(job["job_id"]), job, ttl_seconds or settings.auto_fill_job_ttl_seconds)


def _update_job(job_id: str, **changes) -> dict | None:
    job = _load_job(job_id)
    if not job:
        return None
    if job.get("status") == STATUS_CANCELED:
        return job
    job.update(changes)
    job["updated_at"] = utcnow().isoformat()
    _save_job(job)
    return job


def _fail_job(job_id: str, error_message: str) -> None:
    _update_job(job_id, status=STATUS_FAILED, stage="failed", error_message=error_message)


def _is_canceled(job_id: str) -> bool:
    job = _load_job(job_id)
    return bool(job and job.get("status") == STATUS_CANCELED)


def _public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key != "user_id"}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_string(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    return text[:max_length]


def _limit_text(text: str, max_length: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").lower())


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:12]


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _dedupe_strings(values: list[str]) -> list[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
