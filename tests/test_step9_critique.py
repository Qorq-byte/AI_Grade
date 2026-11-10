"""批次 29 = B1（步骤 9 单独落地）：扣分指标漏项补全。

背景：批次 26 的覆盖检查量出**167 个低分指标里 131 个（78%）没在问题分析里被点名**。
这里不采用通用的「rubric 自评 → 修订」——那要靠模型自己说漏了谁。改成：

1. `consistency.low_score_indicator_names` 算出得分率 <60% 的三级指标（与门禁同一口径）；
2. 与正文做子串比对，"漏了谁"是**算出来的**；
3. 只在真漏了时补一次调用，让模型把这几条补进**已有**的正文；
4. 修订结果过三道闸（不许改丢已点名指标、长度不能塌/爆、至少补进一个），
   任何一道不过就保留原文并记账。

开关默认关闭：先 A/B 证明它值得多花一次调用，再改默认值。
"""
import json
from pathlib import Path

import pytest

from backend.agents import performance_agent as pa
from backend.services import consistency

PROJECT = "某某区农村供水保障工程"


def _system():
    """两个低分叶子（30% / 40%）+ 一个满分叶子。"""
    return {"level1_indicators": [
        {"id": "l1", "level": 1, "name": "决策", "score": 100, "children": [
            {"id": "l2", "level": 2, "name": "立项", "score": 100, "children": [
                {"id": "l3_a", "level": 3, "name": "立项依据充分性", "score": 20},
                {"id": "l3_b", "level": 3, "name": "立项程序规范性", "score": 40},
                {"id": "l3_c", "level": 3, "name": "绩效目标合理性", "score": 40},
            ]},
        ]},
    ]}


#: 三个叶子的实得分：a 6/20（30%）、b 10/40（25%）、c 40/40（100%）
SCORES = {"l3_a": {"actual_score": 6}, "l3_b": {"actual_score": 10},
          "l3_c": {"actual_score": 40}}
LOW = ["立项依据充分性", "立项程序规范性"]

BASE_TEXT = (
    "### 视角一：财政部门视角\n\n立项程序规范性不足，缺少集体决策记录。\n\n"
    "### 视角二：人大常委会视角\n\n预算监督意见落实不到位。\n\n"
    "### 视角三：审计部门视角\n\n资金支付凭证不完整。\n"
)


def _state(text=BASE_TEXT, critique=False):
    state = {
        "session_id": "step9-fixture",
        "project_name": PROJECT,
        "indicator_system": _system(),
        "indicator_score_map": SCORES,
        "indicator_analysis": "指标分析正文",
        "problems_analysis": "",
        "step_results": {},
        "warnings": [],
    }
    # 用真实默认值填 step_control（`_record_step_result` 需要 step_statuses），
    # 再打开开关 —— 手搓一个不完整的 dict 只会测到"fixture 写错了"
    pa._ensure_step_control(state)
    state["step_control"]["critique_step9"] = critique
    return state


def _run(monkeypatch, state, generated=BASE_TEXT, revised=None, raises=None):
    """跑 _node_step9：只替换模型调用，其余走真实实现。"""
    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    agent.session_id = "step9-fixture"
    agent.model_name = "test-model"
    calls = []

    monkeypatch.setattr(agent, "_execute_step", lambda s, step: generated)

    def fake_invoke(messages, step_number, s, timeout=300, stream=None):
        calls.append(messages)
        if raises:
            raise raises
        return revised

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)
    return agent._node_step9(state), calls


def test_disabled_by_default_only_calls_the_model_once(monkeypatch):
    state = _state(critique=False)

    result, calls = _run(monkeypatch, state, revised="不该被调用")

    assert result["problems_analysis"] == BASE_TEXT
    assert result["step9_critique"] == {"enabled": False, "step": 9}
    assert calls == []


def test_no_missing_indicator_skips_the_second_call(monkeypatch):
    """两个低分指标都点名了 —— 不该白花一次调用。"""
    text = BASE_TEXT + "\n立项依据充分性不足，缺少立项论证材料。\n"
    state = _state(critique=True)

    result, calls = _run(monkeypatch, state, generated=text)

    assert calls == []
    assert result["step9_critique"]["missing"] == []
    assert result["step9_critique"]["revised"] is False
    assert result["problems_analysis"] == text


def test_missing_indicator_triggers_one_revision_and_is_adopted(monkeypatch):
    revised = BASE_TEXT + "\n立项依据充分性不足，缺少立项论证材料。\n"
    state = _state(critique=True)

    result, calls = _run(monkeypatch, state, revised=revised)

    assert len(calls) == 1
    prompt = calls[0][-1]["content"]
    assert "立项依据充分性" in prompt          # 漏的那个要出现在补全清单里
    assert BASE_TEXT in prompt                  # 同时把现有正文给模型，要求在原结构上补
    assert result["step9_critique"]["revised"] is True
    assert result["step9_critique"]["gained"] == ["立项依据充分性"]
    assert result["problems_analysis"] == revised.strip()
    assert result["problems_analysis_pre_critique"] == BASE_TEXT


def test_revision_is_rejected_if_it_drops_an_already_named_indicator(monkeypatch):
    """原文点名了「立项程序规范性」，修订版改没了 —— 宁可没补，不可改坏。"""
    revised = "### 视角一：财政部门视角\n\n立项依据充分性不足。\n"
    state = _state(critique=True)

    result, calls = _run(monkeypatch, state, revised=revised)

    assert len(calls) == 1
    assert result["problems_analysis"] == BASE_TEXT
    ledger = result["step9_critique"]
    assert ledger["revised"] is False
    assert "改丢" in ledger["rejected"]
    assert "problems_analysis_pre_critique" not in result


def test_revision_is_rejected_when_it_collapses_in_length(monkeypatch):
    # 保留了原本点名的指标，但整段只剩一句 —— 触发的应当是长度闸，不是「改丢」闸
    revised = "立项程序规范性不足。"
    state = _state(critique=True)

    result, _ = _run(monkeypatch, state, revised=revised)

    assert result["problems_analysis"] == BASE_TEXT
    assert "长度异常" in result["step9_critique"]["rejected"]


def test_revision_is_rejected_when_it_adds_nothing(monkeypatch):
    revised = BASE_TEXT + "\n补充一些不相关的话，但没有点名任何指标。\n"
    state = _state(critique=True)

    result, _ = _run(monkeypatch, state, revised=revised)

    assert result["problems_analysis"] == BASE_TEXT
    assert "一个都没补进去" in result["step9_critique"]["rejected"]


def test_llm_failure_keeps_the_original_text(monkeypatch):
    """补全是增强项：调用失败不能拖垮步骤 9。"""
    state = _state(critique=True)

    result, _ = _run(monkeypatch, state, raises=RuntimeError("超时"))

    assert result["problems_analysis"] == BASE_TEXT
    assert "调用失败" in result["step9_critique"]["rejected"]


def test_threshold_comes_from_the_shared_helper(monkeypatch):
    """补全与门禁必须用同一个口径。

    这里把公开函数换掉，看补全是否跟着变 —— 如果它自己算了一份，
    就会出现「门禁说漏了 3 个、补全只补了 2 个」。
    """
    monkeypatch.setattr(consistency, "low_score_indicator_names",
                        lambda state, ratio=0.6: ["外部注入的指标"])
    revised = BASE_TEXT + "\n外部注入的指标存在问题。\n"
    state = _state(critique=True)

    result, calls = _run(monkeypatch, state, revised=revised)

    assert len(calls) == 1
    assert "外部注入的指标" in calls[0][-1]["content"]
    assert result["step9_critique"]["low_score"] == 1


def test_ledger_and_pre_critique_text_are_persisted(tmp_path, monkeypatch):
    """重启后要能看到「补了几个、改了什么」，所以这两项必须在备份白名单里。"""
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path)
    state = _state(critique=True)
    state["session_id"] = "persist-step9"
    state["step9_critique"] = {"enabled": True, "revised": True, "gained": ["立项依据充分性"]}
    state["problems_analysis_pre_critique"] = BASE_TEXT

    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    agent.session_id = "persist-step9"
    agent._persist_state_backup(state)

    payload = json.loads((tmp_path / "persist-step9.json").read_text(encoding="utf-8"))
    assert payload["step9_critique"]["gained"] == ["立项依据充分性"]
    assert payload["problems_analysis_pre_critique"] == BASE_TEXT


def test_shared_helper_lists_only_low_score_leaves():
    names = consistency.low_score_indicator_names(_state())

    assert names == LOW


# ═══════════════════════════════════════════════════════
#  八、开关与 diff 接口
# ═══════════════════════════════════════════════════════

def test_new_workflows_default_to_critique_on():
    """默认开：A/B 实测点名率 6% → 100%，代价是该步 token 约 2.3 倍。

    默认关的话，这项改动对真实报告等于没做 —— 与批次 26 的教训一致：
    「检测/增强不改变行为，就等于不存在」。
    """
    from backend.routes.agent_routes import WorkflowRequest

    assert WorkflowRequest.model_fields["critique_step9"].default is True


def test_state_layer_defaults_to_off():
    """但 `_ensure_step_control` 这一层默认关：它也会被恢复旧会话、跑单测时调用，
    默认开等于让这些路径莫名其妙多花一次模型钱。"""
    state = {"session_id": "x"}

    pa._ensure_step_control(state)

    assert state["step_control"]["critique_step9"] is False


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.mysql import MEDIUMTEXT
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker

    from backend import database
    from backend.main import app
    from backend.middleware import auth_middleware as auth
    from backend.models.user_models import User, UserRole
    from backend.routes import _shared_state
    from backend.routes import agent_routes as agent

    @compiles(MEDIUMTEXT, "sqlite")
    def _mediumtext(element, compiler, **kwargs):
        return "TEXT"

    engine = create_engine(f"sqlite:///{(tmp_path / 'step9.db').as_posix()}")
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
    yield TestClient(app), headers, tmp_path / "backups"
    engine.dispose()


def _seed(backup_dir, session_id, state):
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    (backup_dir / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_switch_endpoint_toggles_the_flag(api):
    client, headers, backup_dir = api
    _seed(backup_dir, "toggle", _state(critique=True))

    resp = client.post("/api/agent/workflow/set-critique",
                       json={"session_id": "toggle", "enabled": False}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["critique_step9"] is False
    saved = json.loads((backup_dir / "toggle.json").read_text(encoding="utf-8"))
    assert saved["step_control"]["critique_step9"] is False


def test_diff_endpoint_returns_both_versions(api):
    client, headers, backup_dir = api
    state = _state(critique=True)
    state["problems_analysis"] = "补全后的正文"
    state["problems_analysis_pre_critique"] = BASE_TEXT
    state["step9_critique"] = {"enabled": True, "revised": True, "gained": ["立项依据充分性"]}
    _seed(backup_dir, "diffable", state)

    resp = client.get("/api/agent/workflow/diffable/step9-critique", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["changed"] is True
    assert data["before"] == BASE_TEXT
    assert data["after"] == "补全后的正文"
    assert data["ledger"]["gained"] == ["立项依据充分性"]


def test_diff_endpoint_404_when_state_is_missing(api):
    client, headers, _ = api

    resp = client.get("/api/agent/workflow/nope/step9-critique", headers=headers)

    assert resp.status_code == 404


def _contains_text(node, needle):
    """在**解析后的数据**里找字符串。

    踩过的坑：一开始写成 `assert BASE_TEXT not in json.dumps(payload)`——
    JSON 会把换行转义成 `\n`，任何多行文本都"不在"序列化结果里，这条断言永远为真。
    要断言"没泄漏"，必须递归查值本身。
    """
    if isinstance(node, str):
        return needle in node
    if isinstance(node, dict):
        return any(_contains_text(value, needle) for value in node.values())
    if isinstance(node, list):
        return any(_contains_text(value, needle) for value in node)
    return False


def test_status_exposes_the_ledger_but_not_the_texts(api):
    """/status 是 5 秒一次的轮询接口：台账可以带，两版正文（各几千字）不能带。"""
    client, headers, backup_dir = api
    state = _state(critique=True)
    state["step9_critique"] = {"enabled": True, "revised": True, "low_score": 2,
                               "missing": ["立项依据充分性"], "gained": ["立项依据充分性"]}
    state["problems_analysis"] = "补全后的正文"
    state["problems_analysis_pre_critique"] = BASE_TEXT
    _seed(backup_dir, "statusable", state)

    resp = client.get("/api/agent/workflow/statusable/status", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["step9_critique"]["gained"] == ["立项依据充分性"]
    assert not _contains_text(data, BASE_TEXT), "轮询接口里不该出现补全前的全文"
