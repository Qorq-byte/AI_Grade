"""工作流详情与手动终止（批次 7 / P4-3）。

健康页能报出「N 个卡住超 24h」，但点不进去看是哪一个、卡在哪一步 —— 这批补的就是
从「发现异常」到「定位并处置」的最后一段。

测试重点：
1. 时间线要能区分「已完成 / 进行中 / 跑过但没跑完 / 未开始」。真实库里就有
   `completed_steps` 为空、但第 1 步已经烧掉几千 token 的会话，只按完成标记画会把
   问题显示成「一步都没做」。
2. 终止只能对未终态生效 —— 已经 completed 的会话被误标成 failed，用户会丢掉报告入口。
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import OperationLog, TokenUsage, User, UserRole, WorkflowSession
from backend.routes import _shared_state


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1
SID = "wf-detail-1"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'wf.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(_shared_state, "active_workflows", {})
    monkeypatch.setattr(_shared_state, "active_workflow_configs", {})
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {})

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(id=2, username="alice", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        # 典型卡死现场：completed_steps 为空，但第 1 步确实烧了 token
        db.add(WorkflowSession(
            session_id=SID, user_id=2, project_name="某区供水工程", status="running",
            # 真实库里的样子：崩在第 1 步，current_step 还没来及更新就停了
            current_step=0, completed_steps="[]", model_name="deepseek-chat",
            use_rag=True, token_used=3533, errors="[]",
            warnings=json.dumps([{"step": 1, "message": "输出被截断"}], ensure_ascii=False),
        ))
        db.add(WorkflowSession(
            session_id="wf-done", user_id=2, project_name="已完成的", status="completed",
            current_step=14, completed_steps=json.dumps(list(range(1, 15))),
            token_used=999,
        ))
        db.add(TokenUsage(user_id=2, session_id=SID, step_number=1,
                          model_name="deepseek-chat", total_tokens=3533))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


# ═══════════════════════════════════════════════════════
#  一、详情
# ═══════════════════════════════════════════════════════

def test_detail_returns_full_picture(api):
    client, headers, _ = api
    resp = client.get(f"/api/admin/workflows/{SID}", headers=headers)
    assert resp.status_code == 200, resp.text

    d = resp.json()["data"]
    assert d["project_name"] == "某区供水工程"
    assert d["username"] == "alice"
    assert d["status"] == "running"
    assert d["use_rag"] is True
    assert d["token_used"] == 3533
    assert d["created_at"].endswith("Z")
    assert len(d["timeline"]) == 14


def test_timeline_marks_partial_steps(api):
    """completed_steps 是空的，但第 1 步有 token —— 必须显示成「跑过未完成」。"""
    client, headers, _ = api
    d = client.get(f"/api/admin/workflows/{SID}", headers=headers).json()["data"]

    states = {t["step"]: t["state"] for t in d["timeline"]}
    assert states[1] == "partial", "有 token 却显示成未开始，就把问题掩盖了"
    assert states[2] == "pending"
    assert d["timeline"][0]["tokens"] == 3533
    # 没有当前步（崩在更新 current_step 之前），也不该凭空标一个
    assert "current" not in states.values()
    # 没有当前步（崩在更新 current_step 之前），也不该凭空标一个
    assert "current" not in states.values()


def test_completed_workflow_timeline_is_all_done(api):
    client, headers, _ = api
    d = client.get("/api/admin/workflows/wf-done", headers=headers).json()["data"]

    assert {t["state"] for t in d["timeline"]} == {"done"}
    # 终态会话没有「卡住时长」
    assert d["stalled_hours"] is None


def test_detail_survives_broken_json_columns(api):
    client, headers, factory = api
    with factory.begin() as db:
        w = db.query(WorkflowSession).filter(WorkflowSession.session_id == SID).first()
        w.errors = "{不是合法 JSON"
        w.warnings = None

    resp = client.get(f"/api/admin/workflows/{SID}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["errors"] == []
    assert resp.json()["data"]["warnings"] == []


def test_detail_404(api):
    client, headers, _ = api
    assert client.get("/api/admin/workflows/nope", headers=headers).status_code == 404


def test_detail_requires_admin(api):
    client, _, _ = api
    token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    resp = client.get(f"/api/admin/workflows/{SID}",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# ═══════════════════════════════════════════════════════
#  二、手动终止
# ═══════════════════════════════════════════════════════

def test_fail_marks_failed_and_records_who(api):
    client, headers, factory = api

    resp = client.post(f"/api/admin/workflows/{SID}/fail", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "失败" in resp.json()["message"]

    with factory() as db:
        w = db.query(WorkflowSession).filter(WorkflowSession.session_id == SID).first()
        assert w.status == "failed"
        assert w.completed_at is not None
        errors = json.loads(w.errors)
        assert any("管理员手动终止" in e.get("message", "") for e in errors)

        log = db.query(OperationLog).filter(
            OperationLog.action == "admin_fail_workflow").first()
        assert log is not None
        assert log.resource_id == SID


def test_fail_releases_in_memory_state(api, monkeypatch):
    """不释放的话，列表里还会显示它在「运行中」，白标记一场。"""
    client, headers, _ = api

    class FakeAgent:
        closed = False
        def close(self):
            FakeAgent.closed = True

    memory = {SID: FakeAgent()}
    progress = {SID: {"status": "running", "last_step": 3}}
    monkeypatch.setattr(_shared_state, "active_workflows", memory)
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", progress)

    resp = client.post(f"/api/admin/workflows/{SID}/fail", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["released_in_memory"] is True
    assert SID not in memory
    assert SID not in progress
    assert FakeAgent.closed is True


def test_fail_refuses_terminal_workflow(api):
    """已完成的会话被误标成失败，用户会直接丢掉报告入口。"""
    client, headers, _ = api
    resp = client.post("/api/admin/workflows/wf-done/fail", headers=headers)
    assert resp.status_code == 400
    assert "终态" in resp.json()["detail"]


def test_fail_404(api):
    client, headers, _ = api
    assert client.post("/api/admin/workflows/nope/fail", headers=headers).status_code == 404
