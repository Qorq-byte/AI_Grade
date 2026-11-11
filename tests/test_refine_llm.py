"""批次 37（B4 第二步）：refine 接上韧性链。

改造前 `/workflow/refine` 直接 `ChatOpenAI(...).invoke`：主模型 429/超时就是一次失败，
不重试、不降级、也不记 `llm_attempts` —— 导出件的「哪一步换了模型」标注自然也漏掉这次改写。

这组用例分两层：
- 纯函数层（`llm_resilience`）：重试顺序、降级条件、永久错误不降级、全失败的 kind；
- 路由层：预览返回尝试但与不落账、采用才写台账、全失败 503、永久错误 502。
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.agents import performance_agent
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole, WorkflowSession
from backend.routes import _shared_state, agent_routes
from backend.services import llm_resilience as R
from backend.services import refine


class RateLimited(Exception):
    status_code = 429


class BadRequest(Exception):
    status_code = 400


# ─── 纯函数：模型链与重试/降级 ───

def test_chain_dedupes_and_caps_at_three():
    assert R.model_chain("a", ["b", "a", "c", "d"]) == ["a", "b", "c"]


def test_chain_accepts_comma_separated_string():
    assert R.model_chain("a", "b, c ,a") == ["a", "b", "c"]


def test_chain_without_primary_uses_fallbacks_only():
    assert R.model_chain("", "b,c") == ["b", "c"]


def test_first_success_records_one_attempt():
    attempts = []

    out = R.invoke_with_fallback(chain=["a"], call=lambda model: "ok", step=8,
                                 attempts=attempts, retries=0)

    assert out == "ok"
    assert [item["outcome"] for item in attempts] == ["ok"]
    assert attempts[0]["model"] == "a"


def test_transient_error_retries_the_same_model_first():
    calls = []
    delays = []

    def call(model):
        calls.append(model)
        if calls.count(model) == 1:
            raise RateLimited("429")
        return f"{model}-ok"

    out = R.invoke_with_fallback(chain=["a", "b"], call=call, step=8, attempts=[],
                                 retries=1, sleep=delays.append)

    assert out == "a-ok"
    assert calls == ["a", "a"]          # 没有换成 b
    assert delays and delays[0] > 0     # 退避确实等了（测试里基数为 2s）


def test_exhausted_primary_falls_back_and_writes_a_warning():
    def call(model):
        if model == "a":
            raise RateLimited("429")
        return "from-b"

    attempts, warnings = [], []
    out = R.invoke_with_fallback(chain=["a", "b"], call=call, step=8, attempts=attempts,
                                 warnings=warnings, retries=1, sleep=lambda _: None)

    assert out == "from-b"
    assert [item["model"] for item in attempts] == ["a", "a", "b"]
    assert warnings and "备选模型 b" in warnings[0]


def test_permanent_error_does_not_fall_back():
    calls = []

    def call(model):
        calls.append(model)
        raise BadRequest("400")

    with pytest.raises(R.LLMInvocationError) as exc:
        R.invoke_with_fallback(chain=["a", "b"], call=call, step=8, attempts=[],
                               retries=1, sleep=lambda _: None)

    assert exc.value.kind == "permanent"
    assert calls == ["a"]


def test_all_models_failing_raises_transient_with_all_attempts():
    def call(model):
        raise RateLimited("429")

    with pytest.raises(R.LLMInvocationError) as exc:
        R.invoke_with_fallback(chain=["a", "b"], call=call, step=8, attempts=[],
                               retries=1, sleep=lambda _: None)

    assert exc.value.kind == "transient"
    assert len(exc.value.attempts) == 4   # 2 个模型 ×（1 次 + 1 次重试）


def test_empty_chain_is_a_permanent_error():
    with pytest.raises(R.LLMInvocationError) as exc:
        R.invoke_with_fallback(chain=[], call=lambda model: "x", step=8, attempts=[])

    assert exc.value.kind == "permanent"


def test_merge_attempts_keeps_the_latest_entries():
    merged = R.merge_attempts([{"i": index} for index in range(10)], [{"i": 99}], limit=5)

    assert len(merged) == 5
    assert merged[-1]["i"] == 99


# ─── 路由层：预览不落账、采用写台账、失败映射 ───

@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext(element, compiler, **kwargs):
    return "TEXT"


class _Model:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        outcome = self.behaviour(self.calls)
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"content": outcome})()


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'refine_llm.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "0")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1", "project_name": "某某项目",
        "completed_steps": [1, 2, 3, 4, 5, 6, 7, 8],
        "step_results": {"8": {"content": "旧正文", "version": 2, "edited_by": "workflow"}},
        "system_config": {"fallback_models": ["backup"]},
        "warnings": [],
        "review_steps": [],
    }, ensure_ascii=False), encoding="utf-8")
    with factory.begin() as db:
        db.add(User(id=5, username="u5", password_hash="unused", role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id="s1", user_id=5, project_name="某某项目", status="completed"))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 5})}"}
    monkeypatch.setattr(_shared_state, "active_workflow_configs", {"s1": {"model_name": "primary"}})
    yield TestClient(app), headers, monkeypatch
    engine.dispose()


def _state():
    return performance_agent.PerformanceAgent.load_state_backup("s1")


def _install_models(monkeypatch, primary, backup):
    built = {"primary": _Model(primary), "backup": _Model(backup)}
    monkeypatch.setattr(refine, "build_model", lambda name, temperature=0.3: built[name])
    return built


def test_preview_returns_attempts_without_writing_the_ledger(api):
    client, headers, monkeypatch = api
    built = _install_models(monkeypatch,
                            primary=lambda n: RateLimited("429"),
                            backup=lambda n: "备选改写")

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8,
                             "message": "改", "preview": True})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["draft"] == "备选改写"
    assert data["degraded"] is True
    assert len(data["attempts"]) == 3          # primary 两次 + backup 一次
    # 预览不落账：备份里没有 llm_attempts，正文也没变
    state = _state()
    assert "llm_attempts" not in state
    assert state["step_results"]["8"]["content"] == "旧正文"


def test_apply_with_fallback_persists_attempts_and_warning(api):
    client, headers, monkeypatch = api
    _install_models(monkeypatch, primary=lambda n: RateLimited("429"),
                    backup=lambda n: "备选改写")

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8, "message": "改"})

    assert resp.status_code == 200, resp.text
    state = _state()
    assert state["step_results"]["8"]["content"] == "备选改写"
    assert [item["outcome"] for item in state["llm_attempts"]] == ["error", "error", "ok"]
    assert any("备选模型 backup" in item for item in state["warnings"])


def test_all_models_failing_returns_503_and_leaves_the_step_alone(api):
    client, headers, monkeypatch = api
    _install_models(monkeypatch, primary=lambda n: RateLimited("429"),
                    backup=lambda n: RateLimited("429"))

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8, "message": "改"})

    assert resp.status_code == 503
    assert "模型" in resp.json()["detail"]
    assert _state()["step_results"]["8"]["content"] == "旧正文"


def test_permanent_error_returns_502_and_does_not_try_the_backup(api):
    client, headers, monkeypatch = api
    built = _install_models(monkeypatch, primary=lambda n: BadRequest("400"),
                            backup=lambda n: "不该被调用")

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8, "message": "改"})

    assert resp.status_code == 502
    assert built["backup"].calls == 0
