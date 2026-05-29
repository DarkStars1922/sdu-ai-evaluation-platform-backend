from types import SimpleNamespace

from sqlmodel import select

from app.core.utils import json_dumps
from app.models.file_analysis_result import FileAnalysisResult
from app.services.file_analysis_service import _repair_common_ocr_text
from app.services.auto_fill_service import _analysis_text, _build_applicant_contexts, _build_candidate_awards

API_PREFIX = "/api/v1"


def assert_ok(response):
    payload = response.json()
    assert response.status_code == 200, payload
    assert payload["code"] == 0, payload
    return payload["data"]


def assert_error(response, code):
    payload = response.json()
    assert response.status_code == 200, payload
    assert payload["code"] == code, payload
    return payload


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def register_and_login(client, account: str, name: str = "Auto Fill Student") -> str:
    assert_ok(
        client.post(
            f"{API_PREFIX}/auth/register",
            json={
                "account": account,
                "password": "pass1234",
                "name": name,
                "role": "student",
                "class_id": 301,
                "email": f"{account}@example.com",
                "is_reviewer": False,
            },
        )
    )
    login_data = assert_ok(client.post(f"{API_PREFIX}/auth/login", json={"account": account, "password": "pass1234"}))
    return login_data["access_token"]


def upload_proof(client, token: str, filename: str = "social-practice.png") -> str:
    data = assert_ok(
        client.post(
            f"{API_PREFIX}/files/upload",
            headers=auth_headers(token),
            files={"file": (filename, b"fake image bytes", "image/png")},
        )
    )
    return data["file_id"]


def patch_successful_ocr(monkeypatch):
    def fake_analyze_file(db, file, *, uploader=None, force=False):
        record = db.exec(select(FileAnalysisResult).where(FileAnalysisResult.file_id == file.id)).first()
        if not record:
            record = FileAnalysisResult(file_id=file.id)
        record.status = "completed"
        record.ocr_text = "社会实践证明 张三 2026年04月01日"
        record.analysis_json = json_dumps(
            {
                "document_title": "社会实践证明",
                "ocr_summary": "张三参加社会实践，日期为2026年04月01日。",
                "recognized_levels": [],
                "uploader_name_match": {"matched": True},
            }
        )
        record.error_message = None
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    monkeypatch.setattr("app.services.auto_fill_service.analyze_file", fake_analyze_file)


def test_auto_fill_job_confirm_creates_auto_tagged_application(client, monkeypatch):
    token = register_and_login(client, "autofill1001", name="张三")
    file_id = upload_proof(client, token)
    patch_successful_ocr(monkeypatch)

    def fake_call_llm(prompt_payload):
        assert prompt_payload["student"]["name"] == "张三"
        return {
            "category": "labor",
            "sub_type": "basic",
            "award_uid": 61,
            "title": "社会实践证明",
            "description": "参加社会实践并提交证明材料",
            "occurred_at": "2026-04-01",
            "score": 5,
            "score_tree_facts": {"依据": "社会实践"},
            "confidence": 0.92,
            "warnings": [],
            "needs_user_input": [],
            "_model": "test-model",
        }

    monkeypatch.setattr("app.services.auto_fill_service._call_llm", fake_call_llm)

    job = assert_ok(
        client.post(
            f"{API_PREFIX}/applications/auto-fill/jobs",
            headers=auth_headers(token),
            json={"attachments": [{"file_id": file_id}]},
        )
    )
    assert job["status"] == "completed"
    assert job["preview"]["award_uid"] == 61
    assert job["preview"]["category"] == "labor"

    created = assert_ok(
        client.post(
            f"{API_PREFIX}/applications/auto-fill/jobs/{job['job_id']}/confirm",
            headers=auth_headers(token),
            json={
                "award_uid": job["preview"]["award_uid"],
                "title": job["preview"]["title"],
                "description": job["preview"]["description"],
                "occurred_at": job["preview"]["occurred_at"],
                "attachments": [{"file_id": file_id}],
                "category": job["preview"]["category"],
                "sub_type": job["preview"]["sub_type"],
                "score": job["preview"]["score"],
            },
        )
    )
    assert created["tags"] == ["auto"]

    detail = assert_ok(client.get(f"{API_PREFIX}/applications/{created['application_id']}", headers=auth_headers(token)))
    assert detail["tags"] == ["auto"]

    updated = assert_ok(
        client.put(
            f"{API_PREFIX}/applications/{created['application_id']}",
            headers=auth_headers(token),
            json={
                "award_uid": detail["award_uid"],
                "title": "社会实践证明（已修改）",
                "description": detail["description"],
                "occurred_at": detail["occurred_at"],
                "attachments": [{"file_id": file_id}],
                "category": detail["category"],
                "sub_type": detail["sub_type"],
                "score": detail["score"],
                "version": detail["version"],
            },
        )
    )
    updated_detail = assert_ok(client.get(f"{API_PREFIX}/applications/{updated['application_id']}", headers=auth_headers(token)))
    assert updated_detail["tags"] == ["auto"]


def test_auto_fill_job_fails_when_llm_not_configured(client, monkeypatch):
    token = register_and_login(client, "autofill1002", name="李四")
    file_id = upload_proof(client, token)
    monkeypatch.setattr(
        "app.services.auto_fill_service._call_llm",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("auto fill LLM is not configured")),
    )

    job = assert_ok(
        client.post(
            f"{API_PREFIX}/applications/auto-fill/jobs",
            headers=auth_headers(token),
            json={"attachments": [{"file_id": file_id}]},
        )
    )
    assert job["status"] == "failed"
    assert "not configured" in job["error_message"]


def test_auto_fill_rejects_attachment_owned_by_another_student(client):
    owner_token = register_and_login(client, "autofill1003", name="王五")
    other_token = register_and_login(client, "autofill1004", name="赵六")
    file_id = upload_proof(client, owner_token)

    assert_error(
        client.post(
            f"{API_PREFIX}/applications/auto-fill/jobs",
            headers=auth_headers(other_token),
            json={"attachments": [{"file_id": file_id}]},
        ),
        1003,
    )


def test_auto_fill_job_records_llm_parse_failure(client, monkeypatch):
    token = register_and_login(client, "autofill1005", name="钱七")
    file_id = upload_proof(client, token)
    patch_successful_ocr(monkeypatch)
    monkeypatch.setattr("app.services.auto_fill_service._call_llm", lambda _payload: (_ for _ in ()).throw(ValueError("bad json")))

    job = assert_ok(
        client.post(
            f"{API_PREFIX}/applications/auto-fill/jobs",
            headers=auth_headers(token),
            json={"attachments": [{"file_id": file_id}]},
        )
    )
    assert job["status"] == "failed"
    assert "bad json" in job["error_message"]


def test_auto_fill_candidate_recall_prioritizes_sports_award():
    context = [
        {
            "file": {"filename": "“铁人三项运动14天打卡大赛”活动获奖证明.pdf"},
            "text": (
                "山东大学（青岛）体育委员会主办的“铁人三项运动14天打卡大赛”活动获奖证明。"
                "根据打卡完成情况，经综合评定，刘坤等5名同学荣获一等奖。"
            ),
            "payload": {"recognized_levels": ["一等奖"]},
        }
    ]
    active_awards = {
        uid: SimpleNamespace(
            award_uid=uid,
            category=category,
            sub_type=sub_type,
            award_name=f"award-{uid}",
            score=score,
            max_score=score,
        )
        for uid, category, sub_type, score in [
            (2, "physical_mental", "basic", 2.0),
            (8, "physical_mental", "achievement", 4.5),
            (22, "physical_mental", "achievement", 5.25),
            (208, "innovation", "basic", 1.5),
        ]
    }

    candidates = _build_candidate_awards(context, active_awards)

    assert candidates[0]["category"] == "physical_mental"
    assert candidates[0]["sub_type"] == "achievement"
    assert candidates[0]["award_uid"] in {8, 22}


def test_auto_fill_candidate_recall_uses_applicant_award_section_and_domain():
    context = [
        {
            "file": {"filename": "美育讲堂，摄影大赛参与证明及获奖名单.pdf"},
            "text": (
                "学生姓名附近OCR片段 page=5 section=二等奖（5人）：\n"
                "秋季摄影大赛，表现优异，获奖名单如下：\n"
                "一等奖（3人）：\n马云龙\n二等奖（5人）：\n徐昊天\n"
            ),
            "payload": {"recognized_levels": ["一等奖", "二等奖"]},
        }
    ]
    active_awards = {
        uid: SimpleNamespace(
            award_uid=uid,
            category=category,
            sub_type=sub_type,
            award_name=f"award-{uid}",
            score=score,
            max_score=score,
        )
        for uid, category, sub_type, score in [
            (12, "physical_mental", "achievement", 2.25),
            (13, "physical_mental", "achievement", 1.5),
            (41, "art", "achievement", 2.25),
            (42, "art", "achievement", 1.5),
            (61, "labor", "basic", 5.0),
        ]
    }

    candidates = _build_candidate_awards(context, active_awards)

    assert candidates[0]["award_uid"] == 42
    assert candidates[0]["category"] == "art"
    assert candidates[0]["sub_type"] == "achievement"


def test_file_analysis_repairs_common_modern_year_ocr_drop():
    assert _repair_common_ocr_text("226年4月21日") == "2026年4月21日"


def test_auto_fill_includes_later_award_list_context_for_applicant():
    user = type("UserStub", (), {"name": "徐昊天", "account": "202400130001"})()
    payload = {
        "document_title": "参与证明",
        "ocr_summary": "徐昊天参加美育讲堂活动。",
        "pages": [
            {
                "page_index": 0,
                "lines": [
                    {"text": "参与名单如下"},
                    {"text": "徐昊天"},
                    {"text": "202400130001"},
                ],
            },
            {
                "page_index": 3,
                "lines": [
                    {"text": "获奖名单"},
                    {"text": "一等奖："},
                    {"text": "王一"},
                    {"text": "二等奖："},
                    {"text": "徐昊天"},
                    {"text": "202400130001"},
                ],
            },
        ],
    }

    contexts = _build_applicant_contexts(payload, user)
    text = _analysis_text(None, payload, applicant_contexts=contexts)

    assert len(contexts) == 2
    assert any(context["page_index"] == 3 and context["section_heading"] == "二等奖：" for context in contexts)
    assert "学生姓名附近OCR片段 page=3 section=二等奖" in text
