"""Route regressions using only temporary SQLite data and signed test tokens."""
import asyncio
import sys
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from backend import database
from backend.agents import performance_agent
from backend.agents.tools.multi_model_compare import compare_models
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import QuestionnaireAnswer, User, UserRole, WorkflowSession
from backend.routes import agent_routes as agent


@compiles(MEDIUMTEXT, 'sqlite')
def compile_mediumtext(element, compiler, **kwargs):
    return 'TEXT'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module_name in ("backend.database", "backend.agents.base_agent"):
        monkeypatch.setattr(sys.modules[module_name], "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(agent, "_QUESTIONNAIRE_CACHE", {
        "owned": {"questions": [{"id": "q1"}], "project_name": "project"},
    })
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(agent, "_export_base_dir", lambda user: str(tmp_path / "exports" / str(user.id)))
    monkeypatch.setattr(agent, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(agent, "active_workflows", {})
    monkeypatch.setattr(agent, "active_workflow_configs", {})
    monkeypatch.setattr(agent, "_WORKFLOW_PROGRESS", {})
    with factory.begin() as db:
        for uid, role in ((1, UserRole.USER), (2, UserRole.USER), (3, UserRole.ADMIN)):
            db.add(User(id=uid, username=f"user{uid}", password_hash="unused", role=role, is_active=True))
        db.add(WorkflowSession(session_id="owned", user_id=1, project_name="project",
                               status="completed", final_report="existing report"))
    headers = {
        uid: {"Authorization": f"Bearer {auth.create_access_token({'user_id': uid})}"}
        for uid in (1, 2, 3)
    }
    yield SimpleNamespace(client=TestClient(app), db=factory, headers=headers, engine=engine)
    engine.dispose()


@pytest.mark.parametrize(("method", "url", "body"), [
    ("GET", "/api/agent/workflow/owned/status", None),
    ("POST", "/api/agent/workflow/approve", {"session_id": "owned"}),
    ("GET", "/api/agent/workflow/owned/report", None),
    ("POST", "/api/agent/workflow/save-report", {"session_id": "owned", "content": "changed"}),
    ("POST", "/api/agent/workflow/export", {"session_id": "owned"}),
    ("GET", "/api/agent/workflow/owned/questionnaire/answers", None),
    ("DELETE", "/api/agent/workflow/questionnaire/owned", None),
    ("POST", "/api/agent/workflow/questionnaire/batch-delete", {"session_ids": ["owned"]}),
    ("DELETE", "/api/client/reports/owned", None),
])
def test_private_workflow(isolated, method, url, body, monkeypatch):
    def no_backup_delete(*args, **kwargs):
        pytest.fail("unauthorized request attempted to delete a disk backup")

    monkeypatch.setattr(agent.os, "remove", no_backup_delete)
    anonymous = isolated.client.request(method, url, json=body)
    assert anonymous.status_code in (401, 403)
    stranger = isolated.client.request(method, url, json=body, headers=isolated.headers[2])
    assert stranger.status_code == 404
    with isolated.db() as db:
        assert db.query(WorkflowSession).one().final_report == "existing report"
    assert "owned" in agent._QUESTIONNAIRE_CACHE


@pytest.mark.parametrize("uid", [1, 3])
def test_owner_and_admin_can_read_and_save(isolated, uid):
    headers = isolated.headers[uid]
    report = "/api/agent/workflow/owned/report"
    assert isolated.client.get(report, headers=headers).json()["data"]["content"] == "existing report"
    result = isolated.client.post("/api/agent/workflow/save-report", headers=headers,
                                  json={"session_id": "owned", "content": "edited"})
    assert result.status_code == 200
    assert isolated.client.get(report, headers=headers).json()["data"]["content"] == "edited"


def test_public_questionnaire(isolated):
    base = "/api/agent/workflow"
    assert isolated.client.get(f"{base}/owned/questionnaire").json()["data"]["questionnaire"] == [{"id": "q1"}]
    answers = [{"question_id": "q1", "question_text": "test", "question_type": "likert", "selected_level_index": 1}]
    assert isolated.client.post(f"{base}/questionnaire/submit",
                                json={"session_id": "missing", "answers": answers}).status_code == 404
    assert isolated.client.post(f"{base}/questionnaire/submit",
                                json={"session_id": "owned", "answers": answers}).status_code == 200
    with isolated.db() as db:
        assert db.query(QuestionnaireAnswer).one().session_id == "owned"


def test_legacy_admin_routes_require_admin(isolated):
    url = "/api/auth/users"
    assert isolated.client.get(url).status_code in (401, 403)
    assert isolated.client.get(url, headers=isolated.headers[1]).status_code == 403
    assert isolated.client.get(url, headers=isolated.headers[3]).status_code == 200


def test_report_list_excludes_foreign_cache_and_disk(isolated):
    agent.PerformanceAgent.update_state_backup("owned", {"project_name": "project"})
    for url in ("/api/client/reports", "/api/agent/workflow/questionnaire/list"):
        response = isolated.client.get(url, headers=isolated.headers[2])
        assert response.status_code == 200
        assert response.json()["data"] == []


@pytest.mark.parametrize(("fmt", "magic"), [("word", b"PK"), ("pdf", b"%PDF")])
def test_saved_report_export_and_download(isolated, fmt, magic):
    base = "/api/agent/workflow"
    headers = isolated.headers[1]
    # Model an old disk report with a fresh, much shorter database edit.
    agent.PerformanceAgent.update_state_backup("owned", {"final_report": "old backup"})
    result = isolated.client.post(f"{base}/save-report", headers=headers,
                                  json={"session_id": "owned", "content": "# revised"})
    assert result.status_code == 200
    assert isolated.client.get(f"{base}/owned/report", headers=headers).json()["data"]["content"] == "# revised"
    export = isolated.client.post(f"{base}/export", headers=headers,
                                  json={"session_id": "owned", "format": fmt})
    assert export.status_code == 200, export.text
    file_name = export.json()["data"]["file_name"]
    url = "/api/agent/download/" + file_name
    assert isolated.client.get(url).status_code in (401, 403)
    assert isolated.client.get(url, headers=isolated.headers[2]).status_code == 404
    assert isolated.client.get(url, headers=headers).content.startswith(magic)


def test_questionnaire_delete_preserves_workflow(isolated):
    base = "/api/agent/workflow"
    with isolated.db.begin() as db:
        db.add(QuestionnaireAnswer(session_id="owned", question_id="q1", question_text="test"))
    agent.PerformanceAgent.update_state_backup("owned", {"final_report": "disk report"})
    assert isolated.client.delete(f"{base}/questionnaire/owned", headers=isolated.headers[1]).status_code == 200
    agent._QUESTIONNAIRE_CACHE.clear()
    assert isolated.client.get(f"{base}/owned/questionnaire").status_code == 404
    assert isolated.client.get(f"{base}/questionnaire/list", headers=isolated.headers[1]).json()["data"] == []
    with isolated.db() as db:
        assert db.query(QuestionnaireAnswer).count() == 0
        assert db.query(WorkflowSession).one().final_report == "existing report"
    assert agent.PerformanceAgent.load_state_backup("owned")["final_report"] == "disk report"


def test_manual_state_and_questionnaire_survive_snapshot_update(isolated):
    base = "/api/agent/workflow"
    headers = isolated.headers[1]
    agent.PerformanceAgent.update_state_backup("owned", {
        "step_results": {"3": {"content": "legacy"}},
        "questionnaire": agent._QUESTIONNAIRE_CACHE.pop("owned"),
    })
    response = isolated.client.post(f"{base}/export-style", headers=headers,
                                    json={"session_id": "owned", "font_size": 17})
    assert response.status_code == 200, response.text
    # The next request reads persisted state with neither a live agent nor checkpoint.
    data = isolated.client.get(f"{base}/owned/export-style", headers=headers).json()["data"]
    assert data["font_size"] == 17
    assert isolated.client.get(f"{base}/owned/questionnaire").status_code == 200
    assert agent._get_workflow_values("owned")["step_results"][3]["content"] == "legacy"


def test_upload_limits_and_file_ownership(isolated, monkeypatch):
    headers = isolated.headers[1]
    monkeypatch.setattr(agent, "MAX_FILE_SIZE", 8)
    url = "/api/agent/upload"
    assert isolated.client.post(url, headers=headers, files={"file": ("too-big.txt", b"x" * 9)}).status_code == 400
    assert not list(agent.UPLOAD_DIR.glob("*.txt"))
    upload = isolated.client.post(url, headers=headers, files={"file": ("file.txt", b"hello")})
    assert upload.status_code == 200
    data = upload.json()["data"]
    assert data["extracted_preview"] == "hello"
    assert isolated.client.post("/api/agent/workflow/start-stream", headers=isolated.headers[2],
                                json={"project_name": "x", "file_paths": [data["file_path"]]}).status_code == 400
    batch = isolated.client.post(url + "/batch", headers=headers,
                                 files=[("files", ("over.txt", b"9" * 9)), ("files", ("ok.txt", b"12"))])
    assert batch.json()["data"]["success_count"] == 1
    assert not list(agent.UPLOAD_DIR.glob("*_over.txt"))


def test_multi_model_comparison(monkeypatch):
    from backend.agents import base_agent

    class StubAgent:
        def __init__(self, model_name, **kwargs):
            self.model_name = model_name

        def chat(self, prompt, use_memory):
            assert not use_memory
            return {"success": True, "message": prompt} if self.model_name == "ok" else {"error": "unavailable"}

        def close(self):
            pass

    def callback(model, prompt, system):
        if model == "bad":
            raise RuntimeError("unavailable")
        return prompt

    monkeypatch.setattr(base_agent, "BaseAgent", StubAgent)
    for chat in (callback, None):
        result = compare_models(["ok", "bad"], "hello", chat_function=chat)
        assert result["individual_outputs"] == {"ok": "hello"}
        assert result["errors"] == {"bad": "unavailable"}


@pytest.mark.parametrize("fail", [False, True])
def test_sse_terminal_state_and_duplicate_start(isolated, monkeypatch, fail):
    statements = []

    @event.listens_for(isolated.engine, "before_cursor_execute")
    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def invoke(initial_state, config):
        progress = agent._WORKFLOW_PROGRESS[initial_state["session_id"]]
        progress["last_step"] = 1 if fail else 14
        progress["step_results"]["1"] = {"content": "step output"}
        if fail:
            raise RuntimeError("fake provider failure")
        return {"completed_steps": list(range(1, 15)), "final_report": "done"}

    monkeypatch.setattr(agent, "_get_or_create_workflow",
                        lambda sid, params: (SimpleNamespace(app=SimpleNamespace(invoke=invoke)), sid))
    headers = isolated.headers[1]
    url = "/api/agent/workflow/start-stream"
    body = {"project_name": "test", "session_id": "fresh"}
    response = isolated.client.post(url, headers=headers, json=body)
    assert response.status_code == 200
    assert '"event": "step_complete"' in response.text
    assert f'"event": "{ "error" if fail else "complete" }"' in response.text
    assert sum(sql.startswith("UPDATE workflow_sessions") for sql in statements) == 1
    assert not agent._WORKFLOW_PROGRESS
    assert isolated.client.post(url, headers=headers, json=body).status_code == 409
    assert isolated.client.post("/api/agent/workflow/approve", headers=headers,
                                json={"session_id": "fresh"}).status_code == 409
    assert isolated.client.get("/api/agent/workflow/fresh/status", headers=headers).json()["data"]["status"] == (
        "failed" if fail else "completed"
    )


def test_disconnect_does_not_interrupt_finalization(isolated, monkeypatch):
    finish = Event()

    def invoke(initial_state, config):
        progress = agent._WORKFLOW_PROGRESS[initial_state["session_id"]]
        progress["last_step"] = 14
        progress["step_results"]["1"] = {"content": "step output"}
        assert finish.wait(5)
        return {"completed_steps": list(range(1, 15)), "final_report": "after disconnect"}

    monkeypatch.setattr(agent, "_get_or_create_workflow",
                        lambda sid, params: (SimpleNamespace(app=SimpleNamespace(invoke=invoke)), sid))

    async def disconnect():
        with isolated.db() as db:
            response = await agent.start_workflow_stream(
                agent.WorkflowRequest(project_name="test", session_id="disconnect"), db.get(User, 1), db,
            )
            event = await anext(response.body_iterator)
            while event.startswith(":"):
                event = await anext(response.body_iterator)
            assert "step_start" in event
            await response.body_iterator.aclose()
            finish.set()

    asyncio.run(disconnect())
    assert not agent._WORKFLOW_PROGRESS
    with isolated.db() as db:
        record = db.query(WorkflowSession).filter_by(session_id="disconnect").one()
        assert record.status == "completed"
        assert record.final_report == "after disconnect"
        assert record.token_used > 0


def test_empty_signing_key_disables_authentication(isolated, monkeypatch):
    from jose import jwt

    forged = jwt.encode({"user_id": 1, "type": "access"}, "", algorithm="HS256")
    monkeypatch.setattr(auth, "SECRET_KEY", "")
    assert isolated.client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_batch_report_delete_keeps_foreign_snapshot(isolated):
    with isolated.db.begin() as db:
        db.add(WorkflowSession(session_id="own-two", user_id=2, project_name="project", status="completed"))
    for session_id in ("own-two", "owned"):
        agent.PerformanceAgent.update_state_backup(session_id, {"final_report": "backup"})
    response = isolated.client.post("/api/client/reports/batch-delete", headers=isolated.headers[2],
                                    json={"session_ids": ["owned", "own-two", "own-two"]})
    assert response.json()["data"]["deleted"] == 1
    assert agent.PerformanceAgent.load_state_backup("owned")["final_report"] == "backup"
    assert agent.PerformanceAgent.load_state_backup("own-two") == {}
    with isolated.db() as db:
        assert db.query(WorkflowSession).one().session_id == "owned"


def test_cleanup_expires_upload_without_expired_workflow(isolated, monkeypatch, tmp_path):
    import os
    import time
    from backend.services import cleanup_service

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    stale = upload_root / "old.txt"
    stale.write_text("expired", encoding="utf-8")
    past = time.time() - 2 * 86400
    os.utime(stale, (past, past))
    fresh = upload_root / "new.txt"
    fresh.write_text("fresh", encoding="utf-8")
    monkeypatch.setattr(cleanup_service, "__file__", str(tmp_path / "backend/services/cleanup_service.py"))

    with isolated.db() as db:
        stats = cleanup_service.CleanupService.cleanup_expired_files(db, retention_days=1)

    assert stats["deleted_sessions"] == 0
    assert stats["deleted_files"] == 1
    assert fresh.exists() and not stale.exists()


@pytest.mark.parametrize("approver", [1, 3])
def test_approve_actual_interrupt_only_once(isolated, approver):
    from typing import TypedDict
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt

    class State(TypedDict):
        completed_steps: list[int]
        step_control: dict
        final_report: str

    calls = []

    def approval_node(state):
        response = interrupt("approve")
        calls.append(response["approved"])
        return {
            "completed_steps": list(range(1, 15)),
            "step_control": {"current_step": 14},
            "final_report": "approved",
        }

    graph = StateGraph(State)
    graph.add_node("approval", approval_node)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)
    workflow = agent.PerformanceAgent.__new__(agent.PerformanceAgent)
    workflow.session_id = "owned"
    workflow.app = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "owned"}}
    workflow.app.invoke({"completed_steps": []}, config)
    agent.active_workflows["owned"] = workflow

    url = "/api/agent/workflow/approve"
    body = {"session_id": "owned", "approved": True}
    assert isolated.client.post(url, headers=isolated.headers[2], json=body).status_code == 404
    first = isolated.client.post(url, headers=isolated.headers[approver], json=body)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["final_report"] == "approved"
    assert isolated.client.post(url, headers=isolated.headers[approver], json=body).status_code == 409
    assert calls == [True]

def _quality_system(*names):
    return {"indicator_type": "项目支出指标体系", "level1_indicators": [
        {"id": "parent", "name": "产出与效益", "level": 1, "score": 100, "children": [
            {"id": "group", "name": "服务", "level": 2, "score": 100, "children": [
                {"id": f"leaf-{i}", "name": name, "level": 3, "score": 100 / len(names)}
                for i, name in enumerate(names)
            ]},
        ]},
    ]}


def test_questionnaire_versions_and_canonical_answers(isolated):
    base = "/api/agent/workflow"
    headers = isolated.headers[1]
    system = _quality_system("服务满意度", "服务满意程度", "实际支出", "质量与时效")
    agent.PerformanceAgent.update_state_backup("owned", {"indicator_system": system})
    old = isolated.client.post(f"{base}/questionnaire", headers=headers, json={"session_id": "owned"}).json()["data"]
    assert [q["related_indicator_id"] for q in old["questionnaire"]] == ["leaf-0"]
    old_question = old["questionnaire"][0]
    assert old_question["scale_levels"] == ["非常不满意", "不满意", "一般", "满意", "非常满意"]
    agent.PerformanceAgent.update_state_backup("owned", {"indicator_system": _quality_system("服务便利性")})
    new = isolated.client.post(f"{base}/questionnaire", headers=headers, json={"session_id": "owned"}).json()["data"]
    assert new["questionnaire_version"] != old["questionnaire_version"]
    assert new["questionnaire"][0]["related_indicator_name"] == "服务便利性"
    same = isolated.client.post(f"{base}/questionnaire", headers=headers, json={"session_id": "owned"}).json()["data"]
    assert same == new
    payload = {"session_id": "owned", "questionnaire_version": old["questionnaire_version"],
               "answers": [{"question_id": old_question["question_id"], "question_text": "tampered",
                            "selected_level_index": 5}]}
    assert isolated.client.post(f"{base}/questionnaire/submit", json=payload).status_code == 422
    payload["answers"][0]["selected_level_index"] = 0
    assert isolated.client.post(f"{base}/questionnaire/submit", json=payload).status_code == 200
    with isolated.db() as db:
        answer = db.query(QuestionnaireAnswer).one()
        assert answer.question_text == old_question["question_text"]
        assert answer.selected_level_index == 0


def test_step_edit_marks_dependencies_and_preserves_manual_report(isolated):
    base = "/api/agent/workflow"
    headers = isolated.headers[1]
    agent.PerformanceAgent.update_state_backup("owned", {
        "step_results": {"2": {"content": "old fact"}, "9": {"content": "old analysis"}, "14": {"content": "old report"}},
        "effective_data": "old fact", "problems_analysis": "old analysis",
    })
    url = f"{base}/owned/steps/2"
    result = isolated.client.put(url, headers=headers, json={"content": "reviewed fact", "version": 1})
    assert result.status_code == 200
    assert result.json()["data"]["review_steps"] == [9, 14]
    assert isolated.client.put(url, headers=headers, json={"content": "racing edit", "version": 1}).status_code == 409
    backup = agent.PerformanceAgent.load_state_backup("owned")
    assert backup["effective_data"] == "reviewed fact"
    assert backup["step_results"]["2"]["revisions"][0]["content"] == "old fact"
    report = isolated.client.get(f"{base}/owned/report", headers=headers).json()["data"]
    assert report["content"] == "existing report"
    assert report["review_steps"] == [9, 14]
    assert isolated.client.post(f"{base}/export", headers=headers, json={"session_id": "owned"}).status_code == 409


def test_evidence_scope_dedup_and_locator(isolated):
    from backend.models.user_models import KnowledgeBase
    from backend.services.workflow_evidence import retrieve_step_evidence, render_evidence

    with isolated.db.begin() as db:
        db.add_all([
            KnowledgeBase(name="预算规则", content="预算依据应有合同", step_number=2, is_active=True),
            KnowledgeBase(name="其他步骤", content="预算不应从其他步骤读入", step_number=9, is_active=True),
            KnowledgeBase(name="停用规则", content="预算不应读取停用项", step_number=2, is_active=False),
        ])
    state = {"indicator_type": "项目支出指标体系", "all_raw_texts": {
        "contract.pdf": "【PDF 第2页】\n项目预算 20 万元\n项目预算 20 万元",
    }}
    with isolated.db() as db:
        hits = retrieve_step_evidence(db, state, 2, True)
        no_admin = retrieve_step_evidence(db, state, 2, False)
        no_hits = retrieve_step_evidence(db, {"all_raw_texts": {"empty.txt": "无关段落"}}, 2, False)
    assert [hit["kind"] for hit in hits] == ["project", "knowledge"]
    assert [hit["source"] for hit in hits] == ["contract.pdf", "预算规则"]
    assert "第2页" in hits[0]["locator"]
    assert no_admin == hits[:1]
    assert "资料不足" in render_evidence(no_hits)


def test_scores_zero_and_missing_are_distinct(isolated):
    agent.PerformanceAgent.update_state_backup("owned", {"indicator_system": _quality_system("服务满意度", "服务质量")})
    headers = isolated.headers[1]
    base = "/api/agent/workflow"
    scores = [{"indicator_id": "leaf-0", "indicator_name": "服务满意度", "full_score": 50, "actual_score": 0}]
    result = isolated.client.post(f"{base}/save-indicator-scores", headers=headers,
                                  json={"session_id": "owned", "scores": scores})
    assert result.status_code == 200, result.text
    summary = isolated.client.get(f"{base}/owned/score-summary", headers=headers).json()["data"]
    leaves = summary["level1_scores"][0]["items"][0]["items"]
    assert [item["actual_score"] for item in leaves] == [0, None]
    assert summary["score_100"] is None and summary["grade"] == "待核实"
    scores[0]["actual_score"] = 51
    assert isolated.client.post(f"{base}/save-indicator-scores", headers=headers,
                                json={"session_id": "owned", "scores": scores}).status_code == 422


def test_real_graph_start_pause_retry_and_resume(isolated, monkeypatch):
    from collections import Counter
    from pathlib import Path
    import json

    counts = Counter()
    monkeypatch.setattr(agent, "SSE_HEARTBEAT_SECONDS", 0.01)
    first_started, continue_first = Event(), Event()

    def fake_step(self, state, step):
        counts[step] += 1
        assert "20 万元" in state["raw_text"]
        assert state["indicator_type"] == "自定义"
        if step == 1:
            first_started.set()
            assert continue_first.wait(8)
        if step == 2 and counts[step] == 1:
            raise RuntimeError("temporary fixture failure")
        if step == 3:
            return json.dumps(_quality_system("服务质量"), ensure_ascii=False)
        return f"fixture step {step}"

    monkeypatch.setattr(performance_agent, "BaseAgent", lambda **kwargs: None)
    monkeypatch.setattr(performance_agent.PerformanceAgent, "_execute_step", fake_step)
    material = Path(agent.UPLOAD_DIR) / ("u1_" + "a" * 32 + "_fixture.txt")
    material.write_text("项目预算 20 万元，2025 年验收。", encoding="utf-8")

    async def drive():
        with isolated.db() as db:
            request = agent.WorkflowRequest(project_name="fixture", session_id="new-graph", file_paths=[str(material)],
                                            indicator_type="自定义", require_approval=True)
            response = await agent.start_workflow_stream(request, db.get(User, 1), db)
        try:
            first = await anext(response.body_iterator)
            assert first.startswith(":")
            first = await anext(response.body_iterator)
            assert '"event": "step_start"' in first and '"step_number": 1' in first
            assert first_started.is_set()
            assert (await anext(response.body_iterator)).startswith(": heartbeat")
            with isolated.db() as db:
                status = (await agent.get_workflow_status("new-graph", db)).data
            assert status["current_step"] == 1 and status["completed_steps"] == []
        finally:
            continue_first.set()
        events = [event async for event in response.body_iterator]
        assert any('"event": "error"' in event and '"step_number": 2' in event for event in events)
        await agent.retry_workflow(agent.RetryWorkflowRequest(session_id="new-graph"))
        for _ in range(120):
            if "new-graph" not in agent._WORKFLOW_PROGRESS:
                break
            await asyncio.sleep(0.025)

    asyncio.run(drive())
    headers = isolated.headers[1]
    base = "/api/agent/workflow"
    for gate in (3, 6, 11):
        status = isolated.client.get(f"{base}/new-graph/status?after_step=0", headers=headers).json()["data"]
        assert status["status"] == "awaiting_approval"
        assert status["approval_step"] == gate
        assert status["completed_steps"] == list(range(1, gate + 1))
        decision = {"session_id": "new-graph", "approved": True, "approval_step": gate}
        if gate > 3:
            stale = isolated.client.post(f"{base}/approve", headers=headers,
                                         json={**decision, "approval_step": 3})
            assert stale.status_code == 409
        if gate == 3:
            assert isolated.client.post(f"{base}/approve", headers=headers,
                                        json={**decision, "approved": False}).status_code == 200
            assert isolated.client.get(f"{base}/new-graph/status", headers=headers).json()["data"]["approval_step"] == 3
        assert isolated.client.post(f"{base}/approve", headers=headers, json=decision).status_code == 200
    final = isolated.client.get(f"{base}/new-graph/status?after_step=0", headers=headers).json()["data"]
    assert final["completed_steps"] == list(range(1, 15))
    assert final["status"] == "completed"
    assert counts[1] == 1 and counts[2] == 2 and counts[3] == 1
    assert "资料不足" in final["step_summaries"][10]["content"]

def test_score_extraction_uses_only_known_project_evidence(monkeypatch):
    import json

    workflow = agent.PerformanceAgent.__new__(agent.PerformanceAgent)
    workflow.model_name = "fixture"
    state = {
        "indicator_system": _quality_system("服务满意度", "服务质量"),
        "completed_steps": [],
        "step_evidence": {"7": [{"id": "P1-2-1", "kind": "project"}, {"id": "K1-1-1", "kind": "knowledge"}]},
    }
    model_text = json.dumps({"scores": {
        "leaf-0": {"actual_score": 0, "evidence_id": "P1-2-1", "reason": "验收未通过"},
        "leaf-1": {"actual_score": 50, "evidence_id": "K1-1-1", "reason": "通用规则"},
        "unknown": {"actual_score": 80, "evidence_id": "P1-2-1", "reason": "未知指标"},
    }}, ensure_ascii=False)
    monkeypatch.setattr(workflow, "_execute_step", lambda state, step: model_text)
    workflow._node_step7(state)
    assert list(state["indicator_score_map"]) == ["leaf-0"]
    assert state["indicator_score_map"]["leaf-0"]["actual_score"] == 0
    assert workflow._compute_score_summary(state)["grade"] == "待核实"
