from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import settings
from app.core.utils import json_dumps, json_loads, utcnow
from app.models.file_analysis_result import FileAnalysisResult
from app.models.file_info import FileInfo
from app.models.user import User
from app.services.ocr import OCRServiceUnavailableError, extract_seal_and_signature, run_document_ocr

LEVEL_KEYWORDS = (
    "国际级",
    "国家级",
    "省级",
    "市级",
    "校级",
    "院级",
    "院系级",
    "班级",
    "特等奖",
    "一等奖",
    "二等奖",
    "三等奖",
    "金奖",
    "银奖",
    "铜奖",
    "优秀奖",
)
TITLE_HINTS = ("证书", "证明", "获奖", "奖", "竞赛", "荣誉", "表彰", "志愿", "创新", "一等奖", "二等奖", "三等奖")
SUMMARY_HINTS = (
    "证书",
    "证明",
    "荣获",
    "授予",
    "参赛单位",
    "参赛队员",
    "获奖者",
    "姓名",
    "同学",
    "一等奖",
    "二等奖",
    "三等奖",
    "特等奖",
    "优秀奖",
    "竞赛",
    "大赛",
    "项目",
    "活动",
    "委员会",
    "组委会",
    "签名",
    "主任",
    "负责人",
)
BORDER_NOISE_HINTS = ("研究会", "协会", "委员会", "组委会", "教育研究会", "教育研")
COMMON_CHINESE_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘斜厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧利师巩聂关荆司马欧阳上官夏侯诸葛闻人东方赫连皇甫尉迟公羊澹台公冶宗政濮阳淳于单于太叔申屠公孙仲孙轩辕令狐钟离宇文长孙慕容鲜于闾丘司徒司空"
FILE_ANALYSIS_VERSION = "paddleocr_pdf_render_v4"


def analyze_file(db: Session, file: FileInfo, *, uploader: User | None = None, force: bool = False) -> FileAnalysisResult:
    record = db.exec(select(FileAnalysisResult).where(FileAnalysisResult.file_id == file.id)).first()
    if not record:
        record = FileAnalysisResult(file_id=file.id, provider=settings.ai_audit_provider)
        db.add(record)
        db.commit()
        db.refresh(record)

    if record.status == "completed" and not force:
        payload = json_loads(record.analysis_json, {})
        if isinstance(payload, dict) and payload.get("analysis_version") == FILE_ANALYSIS_VERSION:
            return record

    record.provider = settings.ai_audit_provider
    record.status = "running"
    record.error_message = None
    record.updated_at = utcnow()
    db.add(record)
    db.commit()

    try:
        raw_result = run_document_ocr(Path(file.storage_path))
        raw_result = _normalize_raw_ocr_result(raw_result)
        summary = _build_summary(file=file, uploader=uploader, raw_result=raw_result)
        record.status = "completed"
        record.ocr_text = raw_result["ocr_text"] or None
        record.analysis_json = json_dumps(summary)
        record.error_message = None
        record.updated_at = utcnow()
        record.analyzed_at = utcnow()
    except OCRServiceUnavailableError as exc:
        record.status = "failed"
        record.ocr_text = None
        record.analysis_json = json_dumps({"reason": str(exc)})
        record.error_message = str(exc)
        record.updated_at = utcnow()
    except Exception as exc:
        record.status = "failed"
        record.ocr_text = None
        record.analysis_json = json_dumps({"reason": str(exc)})
        record.error_message = str(exc)
        record.updated_at = utcnow()

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _normalize_raw_ocr_result(raw_result: dict) -> dict:
    pages = raw_result.get("ocr_pages") or []
    normalized_pages = []
    for page in pages:
        normalized_lines = []
        for line in page.get("lines") or []:
            if not isinstance(line, dict):
                continue
            normalized_line = dict(line)
            normalized_line["text"] = _repair_common_ocr_text(normalized_line.get("text") or "")
            normalized_lines.append(normalized_line)
        normalized_page = dict(page)
        normalized_page["lines"] = normalized_lines
        normalized_page["text"] = "\n".join(item["text"] for item in normalized_lines if item.get("text")).strip()
        normalized_pages.append(normalized_page)
    normalized = dict(raw_result)
    normalized["ocr_pages"] = normalized_pages
    normalized["ocr_text"] = "\n".join(page["text"] for page in normalized_pages if page.get("text")).strip()
    return normalized


def _repair_common_ocr_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?<!\d)2(\d{2})(?=年\d{1,2}月)", r"20\1", value)
    return value


def get_file_analysis_record(db: Session, file_id: str) -> FileAnalysisResult | None:
    return db.exec(select(FileAnalysisResult).where(FileAnalysisResult.file_id == file_id)).first()


def get_file_analysis_payload(record: FileAnalysisResult | None) -> dict:
    if not record:
        return {}
    payload = json_loads(record.analysis_json, {})
    if not isinstance(payload, dict):
        return {}
    return payload


def _build_summary(*, file: FileInfo, uploader: User | None, raw_result: dict) -> dict:
    pages = raw_result.get("ocr_pages") or []
    full_text = raw_result.get("ocr_text") or ""
    document_title = _extract_document_title(pages, file.original_name)
    ocr_summary = _build_ocr_summary(pages=pages, full_text=full_text, document_title=document_title)
    seal_and_signature = extract_seal_and_signature(
        ocr_pages=pages,
        layout_pages=raw_result.get("layout_pages") or [],
        uploader_name=uploader.name if uploader else None,
        seal_score_threshold=settings.paddleocr_seal_score_threshold,
    )
    return {
        "analysis_version": FILE_ANALYSIS_VERSION,
        "document_title": document_title,
        "ocr_summary": ocr_summary,
        "ocr_text_length": len(full_text),
        "recognized_levels": _extract_levels(full_text, document_title, file.original_name),
        "uploader_name_match": _match_name(
            full_text,
            [
                uploader.name if uploader else None,
                uploader.account if uploader else None,
            ],
            pages=pages,
        ),
        "filename_vs_document_title": _compare_filename(file.original_name, document_title),
        "seal": seal_and_signature["seal"],
        "signature": seal_and_signature["signature"],
        "page_count": len(pages),
        "pages": pages,
    }


def _extract_document_title(pages: list[dict], original_name: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    if pages:
        first_page_lines = pages[0].get("lines", [])
        for index, line in enumerate(first_page_lines[:20]):
            text = (line.get("text") or "").strip()
            if len(text) < 4:
                continue
            if _looks_like_repeated_border_text(text):
                continue
            score = min(len(text), 30)
            if any(hint in text for hint in TITLE_HINTS):
                score += 40
            if any(char.isdigit() for char in text):
                score -= 18
            score -= index * 2
            candidates.append((score, index, text))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]
    return Path(original_name).stem


def _build_ocr_summary(*, pages: list[dict], full_text: str, document_title: str | None) -> str:
    raw_lines = _collect_ocr_lines(pages)
    if not raw_lines:
        raw_lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    cleaned_lines = [_clean_ocr_line(line) for line in raw_lines]
    cleaned_lines = [line for line in cleaned_lines if line]

    selected: list[str] = []
    if document_title and not _looks_like_repeated_border_text(document_title):
        selected.append(_clean_ocr_line(document_title))

    for line in cleaned_lines:
        normalized = _normalize_text(line)
        if not normalized or line in selected:
            continue
        if _looks_like_repeated_border_text(line):
            continue
        if _looks_like_noise_line(line):
            continue
        if _is_summary_candidate(line):
            selected.append(line)
        if len(selected) >= 12:
            break

    if len(selected) < 6:
        for line in cleaned_lines:
            if line in selected or _looks_like_noise_line(line):
                continue
            selected.append(line)
            if len(selected) >= 6:
                break

    summary = "；".join(_dedupe_keep_order(selected))
    if not summary:
        summary = _clean_ocr_line(full_text)[:650]
    return _limit_text(summary, 650)


def _collect_ocr_lines(pages: list[dict]) -> list[str]:
    lines: list[str] = []
    for page in pages:
        for line in page.get("lines", []):
            text = line.get("text") if isinstance(line, dict) else None
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
    return lines


def _clean_ocr_line(text: str) -> str:
    value = str(text or "").strip()
    for phrase in ("全国高等学校计算机教育研究会", "全国大学生计算机系统能力大赛组织委员会"):
        value = re.sub(rf"(?:{re.escape(phrase)}\s*){{2,}}", f"{phrase} ", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"([。；;，,])\1+", r"\1", value)
    return value.strip(" ：:，,。.;；")


def _is_summary_candidate(line: str) -> bool:
    if any(hint in line for hint in ("组织委", "委责会", "委员会主任", "技术委员会主任")) and not any(
        hint in line for hint in ("荣获", "参赛")
    ):
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", line):
        return len(line) >= 3 or line[0] in COMMON_CHINESE_SURNAMES
    if any(hint in line for hint in SUMMARY_HINTS):
        return True
    if re.search(r"(?:20\d{2}|二[〇零○O0一二三四五六七八九十]{2,})年", line):
        return True
    return False


def _looks_like_noise_line(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) <= 1:
        return True
    if re.fullmatch(r"[\W_0-9A-Za-z]{1,4}", stripped):
        return True
    return False


def _looks_like_repeated_border_text(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) > 42:
        return True
    return any(hint in stripped for hint in BORDER_NOISE_HINTS) and not any(
        hint in stripped for hint in ("荣获", "参赛", "获奖", "签名", "主任", "单位", "队员")
    )


def _dedupe_keep_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        normalized = _normalize_text(line)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(line)
    return result


def _limit_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip("；;，,。 ") + "…"


def _extract_levels(*texts: str) -> list[str]:
    matched = []
    haystack = "\n".join(filter(None, texts))
    for keyword in LEVEL_KEYWORDS:
        if keyword in haystack and keyword not in matched:
            matched.append(keyword)
    return matched


def _match_name(text: str, expected_names: list[str | None], *, pages: list[dict] | None = None) -> dict:
    candidates = [name.strip() for name in expected_names if isinstance(name, str) and name.strip()]
    recognized_candidates = _extract_name_candidates(text)
    if not candidates:
        return {
            "expected_name": None,
            "expected_candidates": [],
            "matched": None,
            "matches": [],
            "matched_page_indexes": [],
            "recognized_name_candidates": recognized_candidates,
        }
    normalized_text = _normalize_text(text)
    matches = [name for name in candidates if _normalize_text(name) and _normalize_text(name) in normalized_text]
    matched = bool(matches)
    matched_page_indexes = _find_matching_page_indexes(pages or [], candidates)
    for name in matches:
        if name not in recognized_candidates:
            recognized_candidates.insert(0, name)
    return {
        "expected_name": candidates[0],
        "expected_candidates": candidates,
        "matched": matched,
        "matches": matches,
        "matched_page_indexes": matched_page_indexes,
        "recognized_name_candidates": recognized_candidates,
    }


def _extract_name_candidates(text: str) -> list[str]:
    if not text:
        return []
    patterns = [
        "(?:姓名|获奖者|获奖学生|学生姓名|参赛者|参赛学生|作者|申报人|申请人)[:：\\s]*([\\u4e00-\\u9fff]{2,4})",
        "([\\u4e00-\\u9fff]{2,4})同学",
        "授予[:：\\s]*([\\u4e00-\\u9fff]{2,4})",
        "(?:Name|Student|Applicant)[:：\\s]*([A-Za-z][A-Za-z ._-]{1,40})",
    ]
    ignored = {"学生", "姓名", "获奖", "证书", "大学", "学院", "学校", "项目", "成员"}
    results: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1).strip(" ：:，,。.;；")
            if not value or value in ignored or value in results:
                continue
            results.append(value)
            if len(results) >= 8:
                return results
    return results


def _find_matching_page_indexes(pages: list[dict], candidates: list[str]) -> list[int]:
    result = []
    normalized_candidates = [_normalize_text(candidate) for candidate in candidates if _normalize_text(candidate)]
    if not normalized_candidates:
        return result
    for fallback_index, page in enumerate(pages):
        page_text = page.get("text") or "\n".join(line.get("text") or "" for line in page.get("lines", []))
        normalized_page_text = _normalize_text(page_text)
        if any(candidate in normalized_page_text for candidate in normalized_candidates):
            page_index = page.get("page_index", fallback_index)
            try:
                result.append(int(page_index))
            except (TypeError, ValueError):
                result.append(fallback_index)
    return result


def _compare_filename(filename: str, document_title: str | None) -> dict:
    source_name = Path(filename).stem
    similarity = _text_similarity(source_name, document_title or "")
    return {
        "filename": filename,
        "document_title": document_title,
        "matched": similarity >= 0.68,
        "similarity": similarity,
    }


def _text_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return 1.0
    return round(SequenceMatcher(None, normalized_left, normalized_right).ratio(), 4)


def _normalize_text(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
