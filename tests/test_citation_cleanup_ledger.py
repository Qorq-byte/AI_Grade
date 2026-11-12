"""批次 54：引用清理台账的落盘与回读。

批次 53 把清理前移到落库，代价是**步骤内容不再等于模型原文**：
审计想查「模型原本引了什么」已经没有地方可看。台账把「第几步、哪一句、
删了哪些编号」留下来，不需要回滚清理逻辑也能答这个问题。

这里钉三件事：
1. 台账进备份白名单（`_persist_state_backup` 只写白名单里的键 —— 这个坑复发过五次）；
2. `/workflow/{id}/status` 能读到条数、总删除数、最近 3 条；
3. 老会话（本字段上线前跑的）不因为这个新键变成 500 或空对象崩掉。
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.agents import performance_agent as pa
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole
from backend.routes import _shared_state

ENTRIES = [
    {"step": 9, "at": "2026-11-13T02:00:01+00:00",
     "sentence": "立项批复及规划符合性文件缺失", "removed_ids": ["P1-3-1"]},
    {"step": 10, "at": "2026-11-13T02:00:02+00:00",
     "sentence": "验收资料未能获取", "removed_ids": ["P1-9-1", "P1-9-2"]},
    {"step": 11, "at": "2026-11-13T02:00:03+00:00",
     "sentence": "满意度调查暂无", "removed_ids": ["P1-12-1"]},
    {"step": 14, "at": "2026-11-13T02:00:04+00:00",
     "sentence": "工程量确认单一项为空白", "removed_ids": ["P1-9-9"]},
]


def _state():
    return {
        "session_id": "ledger-fixture",
        "project_name": "某某区农村供水保障工程",
        "step_results": {},
        "warnings": [],
        "citation_cleanup": list(ENTRIES),
    }


def test_cleanup_ledger_survives_a_backup_roundtrip(tmp_path, monkeypatch):
    """白名单漏了它，重启后审计就只能看到「正文被改过、不知道为什么」。"""
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path)
    state = _state()
    state["session_id"] = "ledger-roundtrip"      # 落盘按 state 里的会话号

    instance = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    instance.session_id = "ledger-roundtrip"
    instance._persist_state_backup(state)

    restored = pa.PerformanceAgent.load_state_backup("ledger-roundtrip")

    assert restored["citation_cleanup"] == ENTRIES


@pytest.fixture
def api(tmp_path, monkeypatch):
    @compiles(MEDIUMTEXT, "sqlite")
    def _mediumtext(element, compiler, **kwargs):
        return "TEXT"

    engine = create_engine(f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(_shared_state, "active_workflows", {})
    monkeypatch.setattr(_shared_state, "active_workflow_configs", {})
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {})
    with factory.begin() as db:
        db.add(User(id=11, username="root", password_hash="unused",
                    role=UserRole.ADMIN, is_active=True))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 11})}"}
    yield TestClient(app), headers, tmp_path / "backups"
    engine.dispose()


def _seed(backup_dir, session_id, state):
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    (backup_dir / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_status_exposes_count_and_the_last_three_entries(api):
    client, headers, backup_dir = api
    _seed(backup_dir, "ledger", _state())

    resp = client.get("/api/agent/workflow/ledger/status", headers=headers)

    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]["citation_cleanup_summary"]
    assert summary["count"] == 4
    assert summary["removed_ids"] == 5            # 1 + 2 + 1 + 1
    assert [item["step"] for item in summary["recent"]] == [10, 11, 14]
    assert summary["recent"][0]["removed_ids"] == ["P1-9-1", "P1-9-2"]
    assert "验收资料未能获取" in summary["recent"][0]["sentence"]


def test_status_summary_is_empty_for_sessions_without_the_ledger(api):
    """老会话没有这个字段：不能 500，也不能返回 None 让前端崩。"""
    client, headers, backup_dir = api
    state = _state()
    state.pop("citation_cleanup")
    _seed(backup_dir, "legacy", state)

    resp = client.get("/api/agent/workflow/legacy/status", headers=headers)

    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]["citation_cleanup_summary"]
    assert summary == {"count": 0, "removed_ids": 0, "recent": []}
