"""批次 32：让降级真的可用 —— 配置入口、同模型重试、导出件标注。

批次 31 做完之后有个尴尬的事实：**能力默认是关的**。备选链只能靠环境变量或
`system_config` 手工塞，管理后台没有入口，默认部署里链里只有主模型 ——
写了等于没写。这里锁三件事：

1. 管理后台能配「备用模型（按顺序，最多 2 个）」，能读回，且不覆盖其它系统设置；
2. 新建会话的 `system_config.fallback_models` 从设置里取默认值；
3. 报告导出件在发生过降级时，**自己写一行说明**（不能只留在 warnings 里）。
"""
import json
from pathlib import Path

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
from backend.models.user_models import ModelConfig, User, UserRole
from backend.routes import _shared_state
from backend.routes import agent_routes as agent
from backend.routes import export_routes
from backend.routes import report_routes
from backend.routes import report_routes

PROJECT = "某某区农村供水保障工程"


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'b7.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(_shared_state, "active_workflows", {})
    monkeypatch.setattr(_shared_state, "active_workflow_configs", {})
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {})
    with factory.begin() as db:
        db.add(User(id=9, username="root", password_hash="unused",
                    role=UserRole.ADMIN, is_active=True))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 9})}"}
    yield TestClient(app), headers, factory, tmp_path / "backups"
    engine.dispose()


# ═══════════════════════════════════════════════════════
#  一、管理后台的配置入口
# ═══════════════════════════════════════════════════════

def test_fallback_models_roundtrip(api):
    client, headers, _, _ = api

    saved = client.post("/api/admin/settings/fallback-models",
                        json={"models": ["backup-a", "backup-b"]}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["fallback_models"] == ["backup-a", "backup-b"]

    got = client.get("/api/admin/settings", headers=headers)
    assert got.json()["data"]["fallback_models"] == ["backup-a", "backup-b"]


def test_fallback_models_are_stripped_and_deduped(api):
    client, headers, _, _ = api

    resp = client.post("/api/admin/settings/fallback-models",
                       json={"models": [" a ", "a", "", "b"]}, headers=headers)

    assert resp.json()["data"]["fallback_models"] == ["a", "b"]


def test_more_than_two_fallback_models_is_rejected(api):
    """限 2 个：一次调用最多试 主 + 2 备选，再多的降级结果已不可信。"""
    client, headers, _, _ = api

    resp = client.post("/api/admin/settings/fallback-models",
                       json={"models": ["a", "b", "c"]}, headers=headers)

    assert resp.status_code == 400
    assert "2" in resp.json()["detail"]


def test_setting_fallbacks_keeps_other_system_settings(api):
    """设置都塞在同一行 JSON 里，写备选不能把 AI 聊天开关冲掉。"""
    client, headers, factory, _ = api
    client.post("/api/admin/settings/ai-chat-toggle", json={"ai_chat_enabled": False},
                headers=headers)

    client.post("/api/admin/settings/fallback-models", json={"models": ["x"]}, headers=headers)

    data = client.get("/api/admin/settings", headers=headers).json()["data"]
    assert data["ai_chat_enabled"] is False
    assert data["fallback_models"] == ["x"]
    with factory.begin() as db:
        row = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
        assert json.loads(row.api_base_url)["ai_chat_enabled"] is False


def test_clearing_fallbacks_turns_fallback_off(api):
    client, headers, _, _ = api
    client.post("/api/admin/settings/fallback-models", json={"models": ["x"]}, headers=headers)

    resp = client.post("/api/admin/settings/fallback-models", json={"models": []}, headers=headers)

    assert resp.json()["data"]["fallback_models"] == []
    assert "清空" in resp.json()["message"]


# ═══════════════════════════════════════════════════════
#  二、新会话从设置里取默认值
# ═══════════════════════════════════════════════════════

def test_system_config_seeds_fallbacks_from_settings(monkeypatch):
    monkeypatch.setattr(pa, "fallback_models_from_settings", lambda: ["backup-a"])
    state = {"session_id": "seed", "model_name": "primary"}

    pa._ensure_system_config(state)

    assert state["system_config"]["fallback_models"] == ["backup-a"]


def test_fallback_models_from_settings_reads_the_json_row(api):
    client, headers, _, _ = api
    client.post("/api/admin/settings/fallback-models",
                json={"models": ["backup-a", "backup-b"]}, headers=headers)

    assert pa.fallback_models_from_settings() == ["backup-a", "backup-b"]


def test_fallback_models_from_settings_never_raises(monkeypatch):
    """读设置失败不该让工作流起不来。"""
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "SessionLocal", boom)
    assert pa.fallback_models_from_settings() == []


# ═══════════════════════════════════════════════════════
#  三、导出件里的降级标注
# ═══════════════════════════════════════════════════════

def _attempts(step, model, outcome="ok"):
    return {"step": step, "model": model, "attempt": 1,
            "latency_ms": 100, "outcome": outcome, "detail": ""}


def test_note_lists_every_step_that_used_a_fallback():
    values = {"llm_attempts": [
        _attempts(3, "primary-a"), _attempts(9, "primary-a", "error"),
        _attempts(9, "backup-b"), _attempts(10, "backup-b"),
    ]}

    note = export_routes._fallback_note(values)

    assert "步骤 9（backup-b）" in note
    assert "步骤 10（backup-b）" in note
    assert "步骤 3" not in note


def test_note_is_empty_when_only_the_primary_ran():
    values = {"llm_attempts": [_attempts(3, "primary-a"), _attempts(9, "primary-a")]}

    assert export_routes._fallback_note(values) == ""


def test_note_dedupes_repeated_fallbacks_on_the_same_step():
    values = {"llm_attempts": [
        _attempts(1, "primary-a"), _attempts(9, "backup-b"), _attempts(9, "backup-b"),
    ]}

    assert export_routes._fallback_note(values).count("步骤 9（backup-b）") == 1


def test_export_appends_the_note_to_the_document(api, monkeypatch):
    """导出件必须自己说明哪一步换了模型 —— warnings 是要去翻的，导出件是给外部看的。"""
    client, headers, _, backup_dir = api
    backup_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "session_id": "with-fallback", "project_name": PROJECT,
        "final_report": "# 报告\n\n## 一、绩效评价工作开展情况\n\n" + "正文" * 200,
        "completed_steps": list(range(1, 15)),
        "llm_attempts": [_attempts(1, "primary-a"), _attempts(9, "backup-b")],
        "warnings": [],
    }
    (backup_dir / "with-fallback.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")

    captured = {}

    def fake_export(**kwargs):
        captured["content"] = kwargs["content"]
        return str(backup_dir / "fake.docx"), False

    monkeypatch.setattr(export_routes, "_export_with_cache", fake_export)
    monkeypatch.setattr(export_routes, "_export_blockers", lambda session_id: [])
    monkeypatch.setattr(report_routes, "_report_review_steps", lambda session_id: [])

    resp = client.post("/api/agent/workflow/export",
                       json={"session_id": "with-fallback", "format": "word"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert "备选模型" in captured["content"]
    assert "步骤 9（backup-b）" in captured["content"]


def test_export_without_fallback_has_no_note(api, monkeypatch):
    client, headers, _, backup_dir = api
    backup_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "session_id": "no-fallback", "project_name": PROJECT,
        "final_report": "# 报告\n\n## 一、绩效评价工作开展情况\n\n" + "正文" * 200,
        "completed_steps": list(range(1, 15)),
        "llm_attempts": [_attempts(1, "primary-a")],
        "warnings": [],
    }
    (backup_dir / "no-fallback.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    captured = {}

    def fake_export(**kwargs):
        captured["content"] = kwargs["content"]
        return str(backup_dir / "fake.docx"), False

    monkeypatch.setattr(export_routes, "_export_with_cache", fake_export)
    monkeypatch.setattr(export_routes, "_export_blockers", lambda session_id: [])
    monkeypatch.setattr(report_routes, "_report_review_steps", lambda session_id: [])

    client.post("/api/agent/workflow/export",
                json={"session_id": "no-fallback", "format": "word"}, headers=headers)

    assert "备选模型" not in captured["content"]


def test_safe_note_swallows_unreadable_state(monkeypatch):
    """脚注读不到状态时返回空串，不抛 —— 导出逻辑不能因为它失败。"""
    from fastapi import HTTPException

    def missing(session_id):
        raise HTTPException(status_code=404, detail="工作流状态不存在")

    monkeypatch.setattr(export_routes, "_get_workflow_values", missing)
    assert export_routes._safe_fallback_note("no-state") == ""

    def boom(session_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(export_routes, "_get_workflow_values", boom)
    assert export_routes._safe_fallback_note("no-state") == ""


def test_safe_note_passes_through_a_readable_state():
    values = {"llm_attempts": [_attempts(1, "primary-a"), _attempts(9, "backup-b")]}

    assert "步骤 9（backup-b）" in export_routes._fallback_note(values)


def test_system_config_is_persisted(tmp_path, monkeypatch):
    """`system_config` 也要落盘：否则重启后续跑会用"当时的设置"重算，
    同一会话前后两步可能用不同的备选链（白名单第 5 次复发）。"""
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path)
    state = {"session_id": "persist-config",
             "system_config": {"fallback_models": ["backup-a"], "default_model": "primary"},
             "step_control": {}}
    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    agent.session_id = "persist-config"
    agent._persist_state_backup(state)

    payload = json.loads((tmp_path / "persist-config.json").read_text(encoding="utf-8"))
    assert payload["system_config"]["fallback_models"] == ["backup-a"]
