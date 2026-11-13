"""系统健康页（批次 6 / P4-2）。

这一页最容易变成「一片绿但什么都没测」。所以测试锁两件事：
1. 每个检查项的数据确实来自真实测量（不是写死的字符串）；
2. 阈值判断要能被触发 —— 磁盘 90% 必须是 warn、96% 必须是 down，
   否则红灯永远不会亮，等于没有告警。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.config import settings
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole, WorkflowSession
from backend.routes import admin_routes


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'health.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(id=2, username="alice", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id="s-running", project_name="p", status="running"))
        db.add(WorkflowSession(session_id="s-done", project_name="p", status="completed"))
        db.add(WorkflowSession(session_id="s-fail", project_name="p", status="failed"))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


def _checks(client, headers):
    resp = client.get("/api/admin/system-health", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data, {c["key"]: c for c in data["checks"]}


def test_health_reports_all_expected_dimensions(api):
    client, headers, _ = api
    data, checks = _checks(client, headers)

    assert set(checks) == {"database", "disk", "workflows", "llm", "scheduler", "process"}
    assert data["overall"] in {"ok", "warn", "down"}
    assert data["checked_at"].endswith("Z")


def test_workflow_counts_come_from_the_database(api):
    client, headers, _ = api
    _, checks = _checks(client, headers)

    detail = checks["workflows"]["detail"]
    assert detail["by_status"]["running"] == 1
    assert detail["by_status"]["completed"] == 1
    assert detail["by_status"]["failed"] == 1
    assert "运行中 1" in checks["workflows"]["summary"]


def test_disk_threshold_turns_yellow_then_red(api, monkeypatch):
    """90% 必须是 warn、96% 必须是 down —— 否则这页永远一片绿。"""
    client, headers, _ = api

    fake = {
        "uploads": {"files": 3, "mb": 0.1, "truncated": False},
        "exports": {"files": 1, "mb": 2.0, "truncated": False},
        "volume": {"total_gb": 100.0, "free_gb": 10.0, "used_pct": 90.0},
    }
    monkeypatch.setattr(admin_routes, "_disk_snapshot", lambda root: fake)
    data, checks = _checks(client, headers)
    assert checks["disk"]["level"] == "warn"
    assert data["overall"] != "ok"

    fake["volume"]["used_pct"] = 96.0
    data, checks = _checks(client, headers)
    assert checks["disk"]["level"] == "down"
    assert data["overall"] == "down"


def test_llm_level_follows_keys_and_models(api, monkeypatch):
    client, headers, _ = api

    # 全无 Key → down
    for key in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "DEEPSEEK_MODELS", "")
    monkeypatch.setattr(settings, "QWEN_MODELS", "")
    monkeypatch.setattr(settings, "OPENAI_MODELS", "")
    monkeypatch.setattr(settings, "ANTHROPIC_MODELS", "")
    _, checks = _checks(client, headers)
    assert checks["llm"]["level"] == "down"

    # 有 Key 但没有任何模型 → warn（半配）
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")
    _, checks = _checks(client, headers)
    assert checks["llm"]["level"] == "warn"

    # Key + 模型都有 → ok
    monkeypatch.setattr(settings, "DEEPSEEK_MODELS", "deepseek-chat")
    _, checks = _checks(client, headers)
    assert checks["llm"]["level"] == "ok"
    assert "deepseek-chat" in checks["llm"]["detail"]["env_models"]


def test_env_models_are_counted_not_just_db_rows(api, monkeypatch):
    """模型写在 .env 里是常态，只数数据库会得出「可用 0 个模型」的假警报。"""
    client, headers, _ = api
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "DEEPSEEK_MODELS", "deepseek-chat, deepseek-coder")
    monkeypatch.setattr(settings, "QWEN_MODELS", "qwen-plus")
    monkeypatch.setattr(settings, "OPENAI_MODELS", "")
    monkeypatch.setattr(settings, "ANTHROPIC_MODELS", "")

    _, checks = _checks(client, headers)
    detail = checks["llm"]["detail"]
    assert detail["db_models"] == []
    assert set(detail["env_models"]) == {"deepseek-chat", "deepseek-coder", "qwen-plus"}
    assert "可用 3 个模型" in checks["llm"]["summary"]


def test_health_requires_admin(api):
    client, _, _ = api
    user_token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    resp = client.get("/api/admin/system-health",
                      headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 403


def test_health_is_cheap_to_poll(api):
    """这页每 30 秒自刷，单次不能变成全表扫描。"""
    client, headers, factory = api
    engine = factory.kw["bind"]
    from tests.test_admin_query_budget import count_queries

    with count_queries(engine) as calls:
        client.get("/api/admin/system-health", headers=headers)

    # 鉴权 1 + SELECT 1 + 工作流分组 + 卡住计数 + 模型配置 1 + system_config 1
    assert len(calls) <= 10, f"打了 {len(calls)} 次库\n" + "\n".join(calls[:8])
