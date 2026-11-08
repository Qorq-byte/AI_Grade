"""state_backups 读写竞态：并发读者与写者抢同一个 JSON 文件。

背景（B14.5 实测）：`update_state_backup` 持 `_STATE_BACKUP_LOCK` 后用
`Path.replace` 原子发布；而 `GET /workflow/questionnaire/list` 曾绕过这把锁，
直接 `open()` 读同一个文件。Windows 上 `open()` 不带 FILE_SHARE_DELETE，
只要目标文件还有读句柄，`os.replace` 就抛 `PermissionError [WinError 5]`，
在真实 e2e 里表现为 `POST /workflow/questionnaire` 偶发 500。

这里锁住四件事：
1. 读者短暂占用文件时，写入必须最终落地，而不是把 500 抛给用户；
2. 占用一直不放时，写入必须"响亮地失败"，并把旧内容完整留在盘上；
3. 重试循环必须真的重试（不是抛一次就算），且不会无限等；
4. 列表接口必须走持锁的读入口，不能再自己 `open()`。

注意：用例 1、3 在 POSIX 上恒绿——`os.replace` 在 POSIX 允许覆盖被打开的文件，
所以它们只在 Windows（本项目的部署目标）上具备证伪能力，这是有意保留的。
"""
import json
import os
import sys
import time
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.agents import performance_agent
from backend.agents.performance_agent import PerformanceAgent
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole
from backend.routes import agent_routes as agent


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    target = tmp_path / "backups"
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", target)
    return target


def _seed(backup_dir: Path, session_id: str, payload: dict) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{session_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _start_reader(path: Path, hold_seconds: float):
    """开一个读句柄并持有 hold_seconds 秒，返回 (线程, 已打开事件)。"""
    opened = Event()

    def hold():
        with open(path, "r", encoding="utf-8") as fh:
            fh.read()
            opened.set()
            time.sleep(hold_seconds)

    thread = Thread(target=hold, daemon=True)
    thread.start()
    return thread, opened


def test_write_lands_when_a_reader_holds_the_file_briefly(backup_dir):
    """真实复现：读句柄占用期间写，改前在 Windows 上必抛 WinError 5。"""
    path = _seed(backup_dir, "race", {"project_name": "旧值"})
    reader, opened = _start_reader(path, hold_seconds=0.2)
    try:
        assert opened.wait(2.0), "读线程没能打开备份文件"
        PerformanceAgent.update_state_backup("race", {"project_name": "新值"})
    finally:
        reader.join(2.0)

    assert PerformanceAgent.load_state_backup("race")["project_name"] == "新值"
    assert not list(backup_dir.glob("*.tmp")), "临时文件必须清理干净"


def test_retry_loop_keeps_trying_until_operation_succeeds(monkeypatch):
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_IO_BUDGET_SEC", 2.0)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "拒绝访问")
        return "done"

    assert performance_agent._retry_on_transient_permission(flaky) == "done"
    assert len(calls) == 3


def test_write_fails_loudly_when_publish_is_denied_forever(backup_dir, monkeypatch):
    """占用一直不放：必须抛错，且旧内容完好、不留临时文件。"""
    path = _seed(backup_dir, "stuck", {"project_name": "旧值"})
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_IO_BUDGET_SEC", 0.05)

    def denied(temp_path, target):
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(performance_agent, "_publish_backup", denied)
    with pytest.raises(PermissionError):
        PerformanceAgent.update_state_backup("stuck", {"project_name": "新值"})

    assert json.loads(path.read_text(encoding="utf-8"))["project_name"] == "旧值"
    assert not list(backup_dir.glob("*.tmp"))


def test_delete_state_backup_holds_the_shared_lock(backup_dir, monkeypatch):
    _seed(backup_dir, "gone", {"project_name": "项目"})
    seen = []
    original = performance_agent._discard_backup

    def spy(path, **kwargs):
        seen.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(performance_agent, "_discard_backup", spy)
    assert PerformanceAgent.delete_state_backup("gone") is True
    assert seen and seen[0].name == "gone.json"
    assert PerformanceAgent.delete_state_backup("gone") is False
    assert not (backup_dir / "gone.json").exists()


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(sys.modules["backend.agents.base_agent"], "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(agent, "_QUESTIONNAIRE_CACHE", {})
    monkeypatch.setattr(agent, "active_workflows", {})
    monkeypatch.setattr(agent, "active_workflow_configs", {})
    with factory.begin() as db:
        db.add(User(id=9, username="root", password_hash="unused",
                    role=UserRole.ADMIN, is_active=True))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 9})}"}
    yield TestClient(app), headers, tmp_path / "backups"
    engine.dispose()


def test_list_reads_backups_through_the_locked_reader(api, monkeypatch):
    client, headers, backup_dir = api
    PerformanceAgent.update_state_backup("disk-only", {"questionnaire": {
        "project_name": "磁盘上的项目", "questions": [{"id": "q1"}],
    }})
    seen = []
    original = PerformanceAgent.load_state_backup

    def spy(session_id):
        seen.append(session_id)
        return original(session_id)

    monkeypatch.setattr(PerformanceAgent, "load_state_backup", staticmethod(spy))
    resp = client.get("/api/agent/workflow/questionnaire/list", headers=headers)

    assert resp.status_code == 200, resp.text
    rows = {row["session_id"]: row for row in resp.json()["data"]}
    assert rows["disk-only"]["project_name"] == "磁盘上的项目"
    assert rows["disk-only"]["question_count"] == 1
    assert "disk-only" in seen, "列表接口绕过了持锁的读入口"


def test_list_skips_deleted_and_unreadable_backups(api):
    client, headers, backup_dir = api
    PerformanceAgent.update_state_backup("kept", {"questionnaire": {
        "project_name": "保留", "questions": [{"id": "q1"}, {"id": "q2"}],
    }})
    PerformanceAgent.update_state_backup("wiped", {"questionnaire": {"deleted": True}})
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "broken.json").write_text("{ 不是 JSON", encoding="utf-8")

    resp = client.get("/api/agent/workflow/questionnaire/list", headers=headers)

    assert resp.status_code == 200, resp.text
    rows = {row["session_id"]: row for row in resp.json()["data"]}
    assert set(rows) == {"kept"}
    assert rows["kept"]["question_count"] == 2
