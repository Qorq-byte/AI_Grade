"""批次 38（B5 第一步）：聊天能引用报告 + 流式 + 长对话截断。

改造前 `/api/client/ai/chat` 的两个硬伤（取证见 `progress_round3.md` B38）：
- system 提示只有一句「你是云海睿评的AI助手」，**不知道用户在看哪份报告**；
- `db.query(AiMessage)...all()` 把整段对话全发给模型，长对话既贵又挤占窗口。

这组用例钉住：
1. 上下文里必须出现真实的一致性问题和证据编号（模型才能引用）；
2. 历史按轮次 + 字符预算截断，但**最后一条永远保留**；
3. 流式端点按 delta → done 出帧，结束后把回答落库；
4. 引用了别人的 workflow id → 404（不能借聊天读别人报告）。
"""
import json
from types import SimpleNamespace

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
from backend.models.user_models import AiConversation, AiMessage, User, UserRole, WorkflowSession
from backend.routes import client_routes
from backend.services import chat_context


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext(element, compiler, **kwargs):
    return "TEXT"


# ─── 纯函数：上下文与历史预算 ───

def _state():
    return {
        "project_name": "某某项目",
        "completed_steps": [1, 2],
        "final_report": "",
        "step_evidence": {"8": [
            {"id": "P1", "source": "可研报告.pdf", "locator": "第 12 行",
             "kind": "project", "text": "项目实际完成 80%"},
        ]},
    }


def test_context_is_empty_without_a_report():
    assert chat_context.build_report_context(None) == {"text": "", "references": []}


def test_context_carries_real_issues_and_evidence_ids():
    built = chat_context.build_report_context(_state(), "某某项目")

    assert "某某项目" in built["text"]
    assert "INDICATOR_MISSING#1" in built["text"]          # 阻断级问题，必须排在最前
    assert "步骤 3 没有产出指标体系" in built["text"]
    assert "[P1]" in built["text"]
    assert "报告里没有这项" in built["text"]                # 明确要求不要编造
    ids = [item["id"] for item in built["references"]]
    assert "INDICATOR_MISSING#1" in ids and "P1" in ids


def test_context_is_capped():
    """超长项目名 + 多问题也要压在预算内：上下文再重要也不能挤掉对话本身。"""
    state = _state()
    state["project_name"] = "超长项目名" * 800   # 4800 字，必然超过 3000 的预算

    built = chat_context.build_report_context(state, state["project_name"])

    assert len(built["text"]) <= chat_context.MAX_CONTEXT_CHARS
    assert built["text"].endswith("…")           # 截断要留痕，不是静默切掉


def test_trim_history_keeps_system_and_the_last_message():
    messages = [{"role": "system", "content": "上下文"}]
    messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}条"}
                 for i in range(100)]

    trimmed = chat_context.trim_history(messages, max_turns=6, max_chars=10 ** 6)

    assert trimmed[0]["role"] == "system"
    assert trimmed[-1]["content"] == "第99条"
    assert len(trimmed) == 7


def test_trim_history_drops_oldest_but_never_the_current_question():
    """预算再小，最后一条（用户刚问的）也必须留下 —— 丢了就没法回答。"""
    messages = [{"role": "system", "content": "上下文"}]
    messages += [{"role": "user", "content": "字" * 500} for _ in range(5)]

    trimmed = chat_context.trim_history(messages, max_turns=20, max_chars=100)

    assert len(trimmed) == 2
    assert trimmed[-1]["content"] == "字" * 500


# ─── 路由层：上下文注入 / 流式 / 归属校验 ───

class _Delta:
    def __init__(self, text):
        self.content = text


class _Chunk:
    def __init__(self, text):
        self.choices = [SimpleNamespace(delta=_Delta(text))]
        self.usage = None


class _FakeOwner:
    def __init__(self, reply="回答", pieces=("你", "好"), error=None):
        self.reply = reply
        self.pieces = pieces
        self.error = error
        self.calls = []


class _Completions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.error:
            raise self.owner.error
        if kwargs.get("stream"):
            return iter(_Chunk(text) for text in self.owner.pieces)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.owner.reply))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


def _fake_client(owner):
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(owner)))


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'chat.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "s1.json").write_text(json.dumps(_state(), ensure_ascii=False), encoding="utf-8")
    with factory.begin() as db:
        db.add(User(id=5, username="u5", password_hash="unused", role=UserRole.USER, is_active=True))
        db.add(User(id=6, username="u6", password_hash="unused", role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id="s1", user_id=5, project_name="某某项目", status="completed"))
        db.add(WorkflowSession(session_id="s9", user_id=6, project_name="别人的项目", status="completed"))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 5})}"}
    yield TestClient(app), headers, factory, monkeypatch
    engine.dispose()


def test_chat_injects_the_report_context(api):
    client, headers, _, monkeypatch = api
    owner = _FakeOwner()
    monkeypatch.setattr(client_routes, "_get_openai_client", lambda model=None: _fake_client(owner))

    resp = client.post("/api/client/ai/chat", headers=headers,
                       json={"message": "我这份报告扣分点在哪", "workflow_session_id": "s1"})

    assert resp.status_code == 200, resp.text
    system = owner.calls[0]["messages"][0]["content"]
    assert "INDICATOR_MISSING#1" in system and "[P1]" in system
    assert resp.json()["data"]["references"]


def test_chat_defaults_to_the_latest_completed_report(api):
    client, headers, _, monkeypatch = api
    owner = _FakeOwner()
    monkeypatch.setattr(client_routes, "_get_openai_client", lambda model=None: _fake_client(owner))

    resp = client.post("/api/client/ai/chat", headers=headers, json={"message": "我的报告怎么样"})

    assert resp.status_code == 200, resp.text
    assert "某某项目" in owner.calls[0]["messages"][0]["content"]


def test_chat_rejects_someone_elses_workflow(api):
    client, headers, _, monkeypatch = api
    monkeypatch.setattr(client_routes, "_get_openai_client", lambda model=None: _fake_client(_FakeOwner()))

    resp = client.post("/api/client/ai/chat", headers=headers,
                       json={"message": "看看", "workflow_session_id": "s9"})

    assert resp.status_code == 404


def test_stream_emits_deltas_then_done_and_persists(api):
    client, headers, factory, monkeypatch = api
    owner = _FakeOwner(reply="你好", pieces=("你", "好"))
    monkeypatch.setattr(client_routes, "_get_openai_client", lambda model=None: _fake_client(owner))

    resp = client.post("/api/client/ai/chat/stream", headers=headers,
                       json={"message": "我这份报告扣分点在哪", "workflow_session_id": "s1"})

    assert resp.status_code == 200, resp.text
    assert "event: delta" in resp.text
    assert "event: done" in resp.text
    done = [line for line in resp.text.splitlines() if line.startswith("data: ")][-1]
    payload = json.loads(done[len("data: "):])
    assert payload["reply"] == "你好"
    assert payload["references"]

    with factory() as db:
        conversation = db.query(AiConversation).one()
        roles = [m.role for m in db.query(AiMessage)
                 .filter(AiMessage.conversation_id == conversation.id)
                 .order_by(AiMessage.id).all()]
    assert roles == ["user", "assistant"]


def test_stream_reports_an_error_frame_without_persisting_a_reply(api):
    client, headers, factory, monkeypatch = api
    owner = _FakeOwner(error=RuntimeError("rate limit exceeded"))
    monkeypatch.setattr(client_routes, "_get_openai_client", lambda model=None: _fake_client(owner))

    resp = client.post("/api/client/ai/chat/stream", headers=headers,
                       json={"message": "你好"})

    assert "event: error" in resp.text
    with factory() as db:
        roles = [m.role for m in db.query(AiMessage).all()]
    assert roles == ["user"]
