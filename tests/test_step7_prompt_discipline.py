"""批次 16 / A1：步骤 7 的下发提示词必须把「举证责任」说清（Batch 16）。

改前实测（真实模型、备份 7049d7c9 原样重放）：
  * 26 个叶子指标里 20 条写成 `actual_score: null` + 「待核实」，占 77%；
  * 理由 65/70 条是「未提供 X」，而被引用的编号（P1-9-1 / P1-11-1）确实存在——
    也就是说模型手里有能落地的事实，仍然整条放弃；
  * 同一份材料，模型还会把 `scores` 写成**数组**而不是对象，整轮判定直接归零。

所以这里锁两件事：
  1. 有相关事实就必须给分，未取得佐证按标准计分，只有两种情形准许写「待核实」；
  2. JSON 契约必须是「以指标 ID 为键的对象」，避免整轮因为形状不合规白跑。
同时锁住不能退化的底线：**严禁编造**条款必须还在，且「未提供佐证」计分要单独计数。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.prompts.default_prompts import (
    PromptTemplateFactory,
    TemperatureProfile,
)


# ═══════════════════════════════════════════════════════
#  一、步骤 7 系统提示词：处置口径
# ═══════════════════════════════════════════════════════

def _step7_system() -> str:
    return str(PromptTemplateFactory.build_system_message(7, TemperatureProfile.NORMAL).content)


def test_step7_must_score_when_related_fact_exists():
    """资料里有目标值/计划量就必须给数值分，不能因为「实际值未提供」整条放弃。"""
    text = _step7_system()
    assert "只要资料中存在与该指标相关的事实" in text
    assert "必须给出数值分值" in text
    assert "不得因为「实际值 / 完成量 / 验收结论未提供」" in text


def test_step7_limits_pending_to_two_cases():
    """「待核实」只留给「时点未到」和「资料完全不相关」两种情形。"""
    text = _step7_system()
    assert "允许写「待核实」的情形只有两种" in text
    assert "评价时点尚未到达" in text
    assert "资料与该指标完全不相关" in text


def test_step7_no_evidence_must_be_scored_and_marked():
    """未取得佐证要按标准计分，并用固定前缀把这类分值标出来。"""
    text = _step7_system()
    assert "未取得佐证时的处置" in text
    assert "「未提供佐证：」开头" in text
    assert performance_agent.NO_EVIDENCE_MARKER == "未提供佐证"


def test_step7_forbids_inheriting_upstream_pending():
    """上游评分标准里自带的「待核实」不得抄成本步骤结论。"""
    text = _step7_system()
    assert "不得直接抄成本步骤的评分结论" in text


def test_step7_blanket_abstention_rule_removed():
    """改前那条「无法判断就标无法评价」是整批放弃的直接授权，必须删掉。"""
    text = _step7_system()
    assert "标注为「无法评价」并说明原因" not in text


def test_step7_anti_fabrication_clause_still_present():
    """不许为了「落地」去编造：这条是底线，改提示词时最容易被顺手删掉。

    断言必须落在**步骤 7 自己**的条款上——全局 `quality_rules` 里也有「严禁编造」，
    只查这四个字的话，把本步条款删掉用例照样绿（变异检验抓过这个漏洞）。
    """
    text = _step7_system()
    assert "【严禁编造 — 与前文同等优先级】" in text
    assert "不得把计划值或目标值当作实际值" in text
    assert "不得估算、不得补全" in text


# ═══════════════════════════════════════════════════════
#  二、Human 提示词：JSON 契约
# ═══════════════════════════════════════════════════════

def test_step7_human_declares_object_scores_contract():
    """实测模型会写成数组导致整轮 0 落地，契约必须写明是对象。"""
    raw = PromptTemplateFactory.build_raw_human_prompt(7)
    assert "以指标 ID 为键的对象" in raw
    assert "不是数组" in raw
    assert "每一个三级指标" in raw


def test_step7_human_renders_as_valid_json_example():
    """模板里的大括号要转义，渲染后必须留下一个能读的 JSON 例子。"""
    rendered = PromptTemplateFactory.build_raw_human_prompt(7).format(
        project_name="测试项目",
        evaluation_table_markdown="表格",
        indicator_system_full_json="{}",
        effective_data_categorized="资料",
    )
    assert '{"scores": {"<三级指标ID>": {"actual_score": 3.5' in rendered
    assert "{{" not in rendered


# ═══════════════════════════════════════════════════════
#  三、通用收尾语：步骤 7 单独覆盖，别的步骤不动
# ═══════════════════════════════════════════════════════

def _capture_human_prompt(monkeypatch, step_number):
    """截获真正下发的 HumanMessage（走 _execute_step，不调模型）。"""
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "prompt-fixture"
    agent.model_name = "fixture"
    agent.temperature_profile = TemperatureProfile.NORMAL
    agent.temperature = 0.7
    agent.top_p = 0.9
    agent.use_rag = False
    agent.base_agent = SimpleNamespace(llm=None)
    agent.prompt_manager = __import__(
        "backend.agents.prompts.prompt_manager", fromlist=["get_prompt_manager"]
    ).get_prompt_manager()
    captured = {}

    def fake_invoke(messages, step, state, **kwargs):
        captured["messages"] = messages
        return "{}"

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)
    state = {
        "session_id": "prompt-fixture",
        "project_name": "测试项目",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "raw_text": "资料" * 200,
        "indicator_system": {"level1_indicators": []},
        "step_evidence": {},
    }
    agent._execute_step(state, step_number)
    return captured["messages"][1].content


def test_step7_suffix_overrides_generic_pending_wording(monkeypatch):
    """步骤 7 的收尾语不能再写「不能核实就写待核实」，否则和评分规则互相打架。"""
    human = _capture_human_prompt(monkeypatch, 7)
    assert "本步骤的「不能核实」按评分规则第 3、4 条处置" in human
    assert "不能核实的内容写「待核实」或「资料不足」" not in human
    assert "必须是以指标 ID 为键的对象" in human


def test_other_steps_keep_generic_pending_wording(monkeypatch):
    """只动步骤 7：其它步骤的通用收尾语保持原样。"""
    human = _capture_human_prompt(monkeypatch, 8)
    assert "不能核实的内容写「待核实」或「资料不足」" in human
    assert "本步骤的「不能核实」按评分规则第 3、4 条处置" not in human


# ═══════════════════════════════════════════════════════
#  四、台账：把「未取得佐证计分」和「实测得分」分开
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


def _run_step7(monkeypatch, leaf_ids, scores):
    payload = {"scores": scores}
    text = "评分分析正文。\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "step7-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    monkeypatch.setattr(agent, "_execute_step", lambda state, step: text)
    monkeypatch.setattr(agent, "_invoke_llm", lambda *a, **k: text)
    state = {
        "session_id": "step7-fixture",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "indicator_system": _tree(leaf_ids),
        "evaluation_table": "| 指标 | 分值 |\n| 决策 | 10 |\n",
        "effective_data_categorized": {"项目概况": "总投资5000万元。\n"},
        "step_evidence": {"7": [{"id": "P1-14-1", "kind": "project", "text": "总投资5000万元"}]},
    }
    agent._node_step7(state)
    return state


def test_no_evidence_scored_is_counted_separately(monkeypatch):
    """按「未取得佐证」计的分要单独计数，并在结论行里说明不是实测结果。"""
    state = _run_step7(monkeypatch, ["L3_01_01", "L3_01_02"], {
        "L3_01_01": {"actual_score": 0, "evidence_id": "P1-14-1",
                     "reason": "未提供佐证：未提供资金到位凭证，按标准不得分"},
        "L3_01_02": {"actual_score": 4, "evidence_id": "P1-14-1",
                     "reason": "总投资5000万元已列明，按标准得满分"},
    })
    dispo = state["score_disposition"]
    assert dispo["no_evidence_scored"] == 1
    assert dispo["counts"]["accepted"] == 2
    assert "1 项按「未取得佐证」计分" in dispo["summary_line"]


def test_no_evidence_counter_silent_when_all_measured(monkeypatch):
    """全部是实测得分时不许冒这条提示，否则每次运行都在喊狼来了。"""
    state = _run_step7(monkeypatch, ["L3_01_01"], {
        "L3_01_01": {"actual_score": 4, "evidence_id": "P1-14-1",
                     "reason": "总投资5000万元已列明，按标准得满分"},
    })
    dispo = state["score_disposition"]
    assert dispo["no_evidence_scored"] == 0
    assert "未取得佐证" not in dispo["summary_line"]


def test_manual_refresh_keeps_ledger_shape():
    """人工改分走 refresh_disposition 时不带 no_evidence 字段，汇总必须是 0 而不是炸。"""
    from backend.agents.performance_agent import refresh_disposition
    previous = {"items": {"a": {"status": "model_pending"}}, "counts": {"model_pending": 1}}
    out = refresh_disposition(previous, {"a": {"actual_score": 3}}, ["a"])
    assert out["no_evidence_scored"] == 0
