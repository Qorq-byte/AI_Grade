"""批次 30：把「漏项补全」扩到步骤 10（改进建议），并补上开关的持久化。

为什么要单独一份用例而不是塞回 `test_step9_critique.py`：步骤 10 的**提示词**是另一套
（建议强调「针对什么问题 + 具体做什么」，明令禁止口号），闸门则是同一份实现。
两件事必须分别验证 —— 只测闸门共用，就测不出"步骤 10 拿错了提示词"。

这里还锁一条批次 29 埋下的真问题：`step_control` 不在备份白名单里，
开关重启后会**静默变回 False**（用户什么都没动、功能自己关了）。
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
from backend.routes import agent_routes as agent

PROJECT = "某某区农村供水保障工程"


def _system():
    return {"level1_indicators": [
        {"id": "l1", "level": 1, "name": "决策", "score": 100, "children": [
            {"id": "l2", "level": 2, "name": "立项", "score": 100, "children": [
                {"id": "l3_a", "level": 3, "name": "立项依据充分性", "score": 20},
                {"id": "l3_b", "level": 3, "name": "立项程序规范性", "score": 40},
            ]},
        ]},
    ]}


# l3_a 6/20=30%、l3_b 10/40=25% 都低于 60%；SUGGESTIONS 只点名了前者，
# 所以步骤 10 的补全目标恒为「立项程序规范性」
SCORES = {"l3_a": {"actual_score": 6}, "l3_b": {"actual_score": 10}}

SUGGESTIONS = (
    "## 一、完善立项论证\n\n立项依据充分性不足，建议补充可行性研究报告并履行集体决策程序。\n\n"
    "## 二、规范档案管理\n\n统一归档立项、招标、验收资料。\n"
)


def _state(step10=False, step9=False):
    state = {
        "session_id": "step10-fixture",
        "project_name": PROJECT,
        "indicator_system": _system(),
        "indicator_score_map": SCORES,
        "suggestions": "",
        "problems_analysis": "现有问题分析（含立项依据充分性）",
        "step_results": {},
        "warnings": [],
    }
    pa._ensure_step_control(state)
    state["step_control"]["critique_step9"] = step9
    state["step_control"]["critique_step10"] = step10
    return state


def _run(monkeypatch, state, generated=SUGGESTIONS, revised=None, raises=None):
    instance = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    instance.session_id = "step10-fixture"
    instance.model_name = "test-model"
    calls = []
    monkeypatch.setattr(instance, "_execute_step", lambda s, step: generated)
    monkeypatch.setattr(instance, "_extract_deduction_points", lambda s: [])

    def fake_invoke(messages, step_number, s, timeout=300, stream=None):
        calls.append((step_number, messages))
        if raises:
            raise raises
        return revised

    monkeypatch.setattr(instance, "_invoke_llm", fake_invoke)
    return instance._node_step10(state), calls


def test_step10_is_off_by_default_at_the_state_layer():
    state = _state()

    assert state["step_control"]["critique_step10"] is False


def test_step10_revision_uses_its_own_prompt_and_field(monkeypatch):
    """补进来的必须进 `suggestions`，且提示词是"建议"那套（禁止口号），不是步骤 9 那套。"""
    state = _state(step10=True)
    revised = (SUGGESTIONS + "\n## 三、规范立项程序\n\n针对立项程序规范性不足，"
               "建议出台立项审批细则，由办公室在下一预算年度前完成。\n")

    result, calls = _run(monkeypatch, state, revised=revised)

    assert len(calls) == 1
    step_number, messages = calls[0]
    assert step_number == 10
    prompt = messages[0]["content"]
    assert "改进建议" in prompt
    assert "口号" in prompt            # 明令禁止口号式写法
    assert "问题与原因" not in prompt   # 那是步骤 9 的任务描述，不该串台
    assert result["suggestions"] == revised.strip()
    assert result["suggestions_pre_critique"] == SUGGESTIONS
    assert result["step10_critique"]["revised"] is True
    assert result["step10_critique"]["gained"] == ["立项程序规范性"]


def test_step10_gates_are_shared_so_a_dropped_name_is_rejected(monkeypatch):
    state = _state(step10=True)
    revised = "## 一、完善立项论证\n\n补充立项依据。\n"   # 改丢了「立项依据充分性」

    result, _ = _run(monkeypatch, state, revised=revised)

    assert result["suggestions"] == SUGGESTIONS
    assert "改丢" in result["step10_critique"]["rejected"]


def test_step10_skips_the_call_when_nothing_is_missing(monkeypatch):
    state = _state(step10=True)
    text = SUGGESTIONS + "\n立项程序规范性方面，建议出台审批细则。\n"

    result, calls = _run(monkeypatch, state, generated=text)

    assert calls == []
    assert result["suggestions"] == text
    assert result["step10_critique"]["revised"] is False


def test_step10_ledger_and_pre_text_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path)
    state = _state(step10=True)
    state["session_id"] = "persist-step10"
    state["step10_critique"] = {"enabled": True, "revised": True, "gained": ["立项程序规范性"]}
    state["suggestions_pre_critique"] = SUGGESTIONS

    instance = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    instance.session_id = "persist-step10"
    instance._persist_state_backup(state)

    payload = json.loads((tmp_path / "persist-step10.json").read_text(encoding="utf-8"))
    assert payload["step10_critique"]["gained"] == ["立项程序规范性"]
    assert payload["suggestions_pre_critique"] == SUGGESTIONS


def test_switches_survive_a_backup_roundtrip(tmp_path, monkeypatch):
    """批次 29 的开关没进白名单 —— 重启后静默变回 False，等于功能自己关了。"""
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path)
    state = _state(step9=True, step10=True)
    state["session_id"] = "persist-switch"

    instance = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    instance.session_id = "persist-switch"
    instance._persist_state_backup(state)

    restored = pa.PerformanceAgent.load_state_backup("persist-switch")

    assert restored["step_control"]["critique_step9"] is True
    assert restored["step_control"]["critique_step10"] is True


# ═══════════════════════════════════════════════════════
#  接口：按步骤开关、按步骤看对比
# ═══════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch):
    @compiles(MEDIUMTEXT, "sqlite")
    def _mediumtext(element, compiler, **kwargs):
        return "TEXT"

    engine = create_engine(f"sqlite:///{(tmp_path / 'step10.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(agent, "active_workflows", {})
    monkeypatch.setattr(agent, "active_workflow_configs", {})
    monkeypatch.setattr(agent, "_WORKFLOW_PROGRESS", {})
    with factory.begin() as db:
        db.add(User(id=9, username="root", password_hash="unused",
                    role=UserRole.ADMIN, is_active=True))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 9})}"}
    yield TestClient(app), headers, tmp_path / "backups"
    engine.dispose()


def _seed(backup_dir, session_id, state):
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    (backup_dir / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_switch_endpoint_targets_only_the_requested_step(api):
    client, headers, backup_dir = api
    _seed(backup_dir, "multi", _state(step9=True, step10=True))

    resp = client.post("/api/agent/workflow/set-critique",
                       json={"session_id": "multi", "enabled": False, "step": 10},
                       headers=headers)

    assert resp.status_code == 200, resp.text
    saved = json.loads((backup_dir / "multi.json").read_text(encoding="utf-8"))
    assert saved["step_control"]["critique_step10"] is False
    assert saved["step_control"]["critique_step9"] is True


def test_switch_endpoint_rejects_unsupported_steps(api):
    client, headers, backup_dir = api
    _seed(backup_dir, "multi", _state())

    resp = client.post("/api/agent/workflow/set-critique",
                       json={"session_id": "multi", "enabled": True, "step": 11},
                       headers=headers)

    assert resp.status_code == 400
    assert "9" in resp.json()["detail"]


def test_critique_endpoint_serves_step10_and_keeps_the_old_alias(api):
    client, headers, backup_dir = api
    state = _state(step10=True)
    state["suggestions"] = "补全后的建议"
    state["suggestions_pre_critique"] = SUGGESTIONS
    state["step10_critique"] = {"enabled": True, "revised": True, "gained": ["立项程序规范性"]}
    state["problems_analysis"] = "问题分析现文"
    state["problems_analysis_pre_critique"] = "问题分析原文"
    state["step9_critique"] = {"enabled": True, "revised": True, "gained": []}
    _seed(backup_dir, "both", state)

    resp10 = client.get("/api/agent/workflow/both/critique/10", headers=headers)
    assert resp10.status_code == 200, resp10.text
    assert resp10.json()["data"]["after"] == "补全后的建议"
    assert resp10.json()["data"]["before"] == SUGGESTIONS

    resp9 = client.get("/api/agent/workflow/both/critique/9", headers=headers)
    assert resp9.json()["data"]["after"] == "问题分析现文"

    legacy = client.get("/api/agent/workflow/both/step9-critique", headers=headers)
    assert legacy.status_code == 200
    assert legacy.json()["data"]["after"] == "问题分析现文"


def test_critique_endpoint_rejects_unsupported_steps(api):
    client, headers, backup_dir = api
    _seed(backup_dir, "multi", _state())

    resp = client.get("/api/agent/workflow/multi/critique/12", headers=headers)

    assert resp.status_code == 400


def test_status_exposes_both_ledgers_and_the_switches(api):
    client, headers, backup_dir = api
    state = _state(step9=True)
    state["step9_critique"] = {"enabled": True, "revised": True, "low_score": 2}
    _seed(backup_dir, "statusable", state)

    resp = client.get("/api/agent/workflow/statusable/status", headers=headers)

    data = resp.json()["data"]
    assert set(data["critique"]) == {"9", "10"}
    assert data["critique"]["9"]["low_score"] == 2
    assert data["critique_switches"]["9"] is True
    assert data["critique_switches"]["10"] is False
