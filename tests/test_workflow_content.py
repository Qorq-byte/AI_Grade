"""分步正文查看（批次 8 / P4-4）。

管理端能看到「最终报告 35312 字」，但看不到正文。这批把它接上，同时防三件事：

1. **路径穿越**：`session_id` 会被拼进 `state_backups/{sid}.json`，不校验就是任意文件读取；
2. **越权取字段**：只能从白名单里的状态字段取，不能拿 `?field=password_hash` 之类顺手读别的；
3. **无痕查看**：报告是用户的业务数据，谁在什么时候看了谁的报告，必须留审计。
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
from backend.models.user_models import OperationLog, User, UserRole, WorkflowSession
from backend.routes import admin_routes


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1
SID = "wf-content-1"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'content.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    # 让 state_backups 指向临时目录
    monkeypatch.setattr(admin_routes, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(admin_routes, "_BACKUP_CACHE", {})

    backups = tmp_path / "state_backups"
    backups.mkdir()
    (backups / f"{SID}.json").write_text(json.dumps({
        "cost_list": "资料清单一：需要营业执照、批复文件",
        "indicator_analysis": "S7 指标分析正文",
        "score_table": "| 指标 | 得分 |\n| --- | --- |\n| A | 10 |",
        "experience_summary": "经验做法正文",
        "final_report": "# 最终报告\n\n这是完整报告。",
    }, ensure_ascii=False), encoding="utf-8")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(id=2, username="alice", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id=SID, user_id=2, project_name="某区供水工程",
                               status="completed", current_step=14, completed_steps="[]"))
        db.add(WorkflowSession(session_id="no-backup", user_id=2, project_name="只有检查点的",
                               status="failed", current_step=3,
                               checkpoint_data=json.dumps({"effective_data": "来自检查点的正文"},
                                                          ensure_ascii=False)))
        db.add(WorkflowSession(session_id="only-column", user_id=2, project_name="只有列",
                               status="completed", current_step=14,
                               final_report="数据库列里的最终报告"))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory, tmp_path
    engine.dispose()


def _content(client, headers, sid, step):
    return client.get(f"/api/admin/workflows/{sid}/content?step={step}", headers=headers)


# ═══════════════════════════════════════════════════════
#  一、取得到、且取对了
# ═══════════════════════════════════════════════════════

def test_step_content_comes_from_state_backup(api):
    client, headers, _, _ = api
    resp = _content(client, headers, SID, 1)
    assert resp.status_code == 200, resp.text

    d = resp.json()["data"]
    assert d["step"] == 1
    assert d["title"] == "资料清单"
    assert d["source"] == "state_backup"
    assert d["truncated"] is False
    assert "营业执照" in d["sections"][0]["content"]


def test_step_with_two_fields_returns_both(api):
    client, headers, _, _ = api
    d = _content(client, headers, SID, 7).json()["data"]

    labels = [s["label"] for s in d["sections"]]
    assert labels == ["指标得分分析", "评分表"]


def test_step_without_content_is_empty_but_200(api):
    """第 4 步没有内容 —— 空数组比 404 好，前端能直接显示「这一步没有正文」。"""
    client, headers, _, _ = api
    resp = _content(client, headers, SID, 4)
    assert resp.status_code == 200
    assert resp.json()["data"]["sections"] == []


def test_falls_back_to_checkpoint_when_no_backup_file(api):
    client, headers, _, _ = api
    d = _content(client, headers, "no-backup", 2).json()["data"]

    assert d["source"] == "checkpoint_data"
    assert d["sections"][0]["content"] == "来自检查点的正文"


def test_falls_back_to_final_report_column(api):
    """备份和检查点都没有时，最后兜底用 workflow_sessions.final_report。"""
    client, headers, _, _ = api
    d = _content(client, headers, "only-column", 14).json()["data"]

    assert d["source"] == "final_report_column"
    assert "数据库列里的最终报告" in d["sections"][0]["content"]


def test_detail_lists_which_steps_have_content(api):
    client, headers, _, _ = api
    d = client.get(f"/api/admin/workflows/{SID}", headers=headers).json()["data"]

    steps = {s["step"]: s["chars"] for s in d["sections"]}
    assert set(steps) == {1, 7, 8, 14}
    assert steps[14] == len("# 最终报告\n\n这是完整报告。")


# ═══════════════════════════════════════════════════════
#  二、防越权与防穿越
# ═══════════════════════════════════════════════════════

def test_session_id_cannot_escape_the_backup_dir(api, tmp_path):
    """`session_id` 拼接进文件路径，必须挡掉 ../ 和绝对路径。"""
    client, headers, _, _ = api
    (tmp_path / "secret.json").write_text('{"cost_list": "不该被读到"}', encoding="utf-8")

    for evil in ("../secret", "..%2Fsecret", "a/../../secret", ".."):
        resp = _content(client, headers, evil, 1)
        assert resp.status_code in (400, 404), f"{evil} 竟然通过了：{resp.status_code}"
        assert "不该被读到" not in resp.text


def test_step_out_of_range_is_rejected(api):
    client, headers, _, _ = api
    assert _content(client, headers, SID, 0).status_code == 422
    assert _content(client, headers, SID, 15).status_code == 422


def test_content_requires_admin(api):
    client, _, _, _ = api
    token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    resp = client.get(f"/api/admin/workflows/{SID}/content?step=1",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_unknown_session_is_404(api):
    client, headers, _, _ = api
    assert _content(client, headers, "does-not-exist", 1).status_code == 404


# ═══════════════════════════════════════════════════════
#  三、体量与审计
# ═══════════════════════════════════════════════════════

def test_overlong_content_is_truncated(api, monkeypatch):
    client, headers, _, tmp_path = api
    monkeypatch.setattr(admin_routes, "_CONTENT_CHAR_CAP", 50)
    (tmp_path / "state_backups" / f"{SID}.json").write_text(
        json.dumps({"cost_list": "字" * 500}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(admin_routes, "_BACKUP_CACHE", {})

    d = _content(client, headers, SID, 1).json()["data"]
    assert d["truncated"] is True
    assert len(d["sections"][0]["content"]) == 50


def test_viewing_content_is_audited(api):
    """报告是用户业务数据，谁看了谁的报告必须留痕。"""
    client, headers, factory, _ = api
    _content(client, headers, SID, 14)

    with factory() as db:
        log = db.query(OperationLog).filter(
            OperationLog.action == "admin_view_workflow_content").first()
        assert log is not None
        assert log.resource_id == SID
        assert "第 14 步" in (log.detail or "")
