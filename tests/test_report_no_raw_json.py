"""报告正文不得出现裸 JSON（批次 22 = A9）。

起点证据（新会话 73604287，32 个叶子）：报告 33,423 字里 **3,738 字（11.2%）** 是
步骤 7 的评分 JSON 块，原样贴在第四章开头。第三章那份（步骤 6 的副本）批次 21 已经清掉，
这一份是 B20「先 JSON 后正文」的副作用：报告组装直接拼 `indicator_analysis`。

口径：
  * 拆分只做一次、在步骤 7 落盘时做（`split_step7_output`），组装只取正文；
  * `indicator_analysis` 保持原始全文——下游提示词与 refine 都读它，改语义会波及别处；
  * 老备份没有 `step7_prose` 时**回退到原文**，宁可留着 JSON 也不能把章节清空；
  * 识别不到合法 JSON 就不剥离（不做猜测式切割）。
"""
import json

from backend.agents import performance_agent
from backend.agents.tools import structured_output
from backend.routes import agent_routes

PAYLOAD = {"scores": {"L3_01_01_01": {
    "actual_score": 3.0, "evidence_id": "P1-14-1", "reason": "与指标表一致"}}}
JSON_BLOCK = "```json\n" + json.dumps(PAYLOAD, ensure_ascii=False) + "\n```"
PROSE = "本项目得分 3.0 分，主要问题在资料完整度。"
TABLE = "| 一级指标 | 分值 | 证据 |\n| 决策 | 10 | P1-14-1 |\n"


# ═══════════════════════════════════════════════════════
#  拆分函数：认得出、切得准、拿不准就不切
# ═══════════════════════════════════════════════════════

def test_split_json_first():
    scores_json, prose = structured_output.split_step7_output(JSON_BLOCK + "\n\n" + PROSE)

    assert json.loads(scores_json)["scores"]["L3_01_01_01"]["actual_score"] == 3.0
    assert prose == PROSE
    assert "```" not in prose


def test_split_json_last():
    """正文在前、JSON 在后（B20 之前的历史输出形态）。"""
    scores_json, prose = structured_output.split_step7_output(PROSE + "\n\n" + JSON_BLOCK)

    assert json.loads(scores_json)["scores"]
    assert prose == PROSE


def test_split_unfenced_json():
    raw = json.dumps(PAYLOAD, ensure_ascii=False) + "\n\n" + PROSE

    scores_json, prose = structured_output.split_step7_output(raw)

    assert json.loads(scores_json)["scores"]
    assert prose == PROSE


def test_split_keeps_text_when_json_invalid():
    """半截 JSON 不剥：切掉一半的代码块比留着更难读。"""
    raw = '```json\n{"scores": {"L3_01_01_01": {"actual_score": 3.0\n```\n\n' + PROSE

    scores_json, prose = structured_output.split_step7_output(raw)

    assert scores_json == ""
    assert prose == raw.strip()


def test_split_without_json():
    scores_json, prose = structured_output.split_step7_output(PROSE)

    assert scores_json == ""
    assert prose == PROSE


def test_split_empty():
    assert structured_output.split_step7_output("") == ("", "")


def test_split_ignores_non_json_fence():
    """正文里的普通代码块（比如一段表格样例）不能被当成评分 JSON 吃掉。"""
    raw = "```text\nL3_01_01_01 = 3.0\n```\n\n" + PROSE

    scores_json, prose = structured_output.split_step7_output(raw)

    assert scores_json == ""
    assert "L3_01_01_01 = 3.0" in prose


def test_split_collapses_blank_lines():
    """正文内部的空行也要压掉——只 strip 首尾是不够的。"""
    first, second = "第一段分析。", "第二段分析。"
    raw = first + "\n\n\n\n" + second + "\n\n" + JSON_BLOCK + "\n\n\n"

    _, prose = structured_output.split_step7_output(raw)

    assert prose == first + "\n\n" + second
    assert "\n\n\n" not in prose


# ═══════════════════════════════════════════════════════
#  步骤 7 落盘：prose 与 json 分开存，indicator_analysis 不动
# ═══════════════════════════════════════════════════════

def _tree(leaf_ids, full=10.0):
    leaves = [{
        "id": leaf_id, "level": 3, "name": f"指标{index}", "parent_id": "L2_01_01",
        "score": full, "explanation": "说明", "scoring_standard": "标准", "children": [],
    } for index, leaf_id in enumerate(leaf_ids, 1)]
    return {
        "indicator_type": "项目支出指标体系",
        "level1_indicators": [{
            "id": "L1_01", "level": 1, "name": "决策", "order": 1, "score": 100,
            "children": [{
                "id": "L2_01_01", "level": 2, "name": "项目立项", "parent_id": "L1_01",
                "score": 100, "children": leaves,
            }],
        }],
    }


def _agent(monkeypatch, text):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "a9-fixture"
    agent.model_name = "fixture"
    agent.base_agent = type("B", (), {"llm": None})()
    monkeypatch.setattr(agent, "_execute_step", lambda state, step: text)
    monkeypatch.setattr(agent, "_invoke_llm", lambda *a, **k: text)
    return agent


def _state(**extra):
    base = {
        "session_id": "a9-fixture",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "indicator_system": _tree(["L3_01_01_01"]),
        "evaluation_table": TABLE,
        "effective_data_categorized": {"项目概况": "一、项目概况\n总投资5000万元。"},
        "step_evidence": {"7": [{"id": "P1-14-1", "kind": "project", "text": "投资5000万元",
                                 "source": "可研报告.pdf", "locator": "第14行"}]},
    }
    base.update(extra)
    return base


def test_step7_stores_split_fields(monkeypatch):
    agent = _agent(monkeypatch, JSON_BLOCK + "\n\n" + PROSE)
    state = _state()

    agent._node_step7(state)

    assert json.loads(state["step7_scores_json"])["scores"]
    assert PROSE in state["step7_prose"]
    assert "```" not in state["step7_prose"]


def test_step7_keeps_full_content_for_downstream(monkeypatch):
    """`indicator_analysis` 是步骤 9/11 提示词的输入，语义不能改。"""
    agent = _agent(monkeypatch, JSON_BLOCK + "\n\n" + PROSE)
    state = _state()

    agent._node_step7(state)

    assert "```json" in state["indicator_analysis"]
    assert state["step_results"][7]["content"] == state["indicator_analysis"]


def test_step7_split_does_not_touch_score_map(monkeypatch):
    agent = _agent(monkeypatch, JSON_BLOCK + "\n\n" + PROSE)
    state = _state()

    agent._node_step7(state)

    assert state["indicator_score_map"]["L3_01_01_01"]["actual_score"] == 3.0
    assert state["score_disposition"]["counts"]["accepted"] == 1


def test_step7_split_fields_in_backup_whitelist():
    """白名单是字段白名单：不加这两个键，重启后报告又会把 JSON 贴回去。"""
    captured = {}
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "a9-backup"
    agent.update_state_backup = lambda sid, backup: captured.update(backup)

    agent._persist_state_backup({
        "session_id": "a9-backup",
        "step7_scores_json": json.dumps(PAYLOAD, ensure_ascii=False),
        "step7_prose": PROSE,
    })

    assert captured["step7_prose"] == PROSE
    assert json.loads(captured["step7_scores_json"])["scores"]


# ═══════════════════════════════════════════════════════
#  组装：第四章只取正文；老备份回退原文
# ═══════════════════════════════════════════════════════

def test_final_report_chapter4_has_no_json(monkeypatch):
    agent = _agent(monkeypatch, JSON_BLOCK + "\n\n" + PROSE)
    state = _state()
    agent._node_step7(state)

    report = agent._assemble_final_report(state)

    assert "## 四、指标得分及分析" in report
    assert "```" not in report, "报告正文里不应再出现代码块"
    assert PROSE in report
    assert '"scores"' not in report


def test_final_report_falls_back_to_full_content():
    """老会话没有 step7_prose：宁可留着 JSON，也不能把第四章清空。"""
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    state = _state(indicator_analysis=JSON_BLOCK + "\n\n" + PROSE)

    report = agent._assemble_final_report(state)

    assert PROSE in report


def test_values_assembly_prefers_prose():
    values = {
        "project_name": "样例项目",
        "indicator_analysis": JSON_BLOCK + "\n\n" + PROSE,
        "step7_prose": PROSE,
    }

    report = agent_routes._assemble_report_from_steps(values)

    assert PROSE in report
    assert "```" not in report


def test_backup_assembly_prefers_prose():
    backup = {
        "project_name": "样例项目",
        "step7_prose": PROSE,
        "step_results": {"7": {"content": JSON_BLOCK + "\n\n" + PROSE}},
    }

    report = agent_routes._assemble_report_from_backup_steps(backup)

    assert PROSE in report
    assert "```" not in report


def test_backup_assembly_old_backup_compatible():
    backup = {
        "project_name": "样例项目",
        "step_results": {"7": {"content": JSON_BLOCK + "\n\n" + PROSE}},
    }

    report = agent_routes._assemble_report_from_backup_steps(backup)

    assert PROSE in report


# ═══════════════════════════════════════════════════════
#  人工修改步骤 7 之后，报告的正文要跟着变
# ═══════════════════════════════════════════════════════

def test_edit_step7_recomputes_prose(monkeypatch):
    captured = {}
    values = {
        "step_results": {7: {"content": "旧内容", "version": 1}},
        "review_steps": [],
    }
    monkeypatch.setattr(agent_routes, "_get_workflow_values", lambda sid: dict(values))
    monkeypatch.setattr(agent_routes, "_persist_state",
                        lambda sid, updates: captured.update(updates))

    new_content = PROSE + "\n\n" + JSON_BLOCK
    agent_routes._apply_step_edit("a9-edit", 7, new_content, "manual")

    assert captured["indicator_analysis"] == new_content
    assert captured["step7_prose"] == PROSE
    assert json.loads(captured["step7_scores_json"])["scores"]
