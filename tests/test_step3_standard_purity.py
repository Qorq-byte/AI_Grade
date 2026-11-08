"""批次 18 / A5：评分标准的字段里不许夹带资料核对结论（Batch 18）。

改前实测（27 份真实备份 + 2 份改前对照）：
  * 543 个叶子指标里 **400 条（74%）** 的 `scoring_standard` 夹着「待核实 / 无法判断 /
    资料不足」；批次 16 之后新跑的两份仍有 67%、81%；
  * 这 400 条**全部来自步骤 3 的输出**（步骤 5 只把 raw 文本存进
    `scoring_standards["raw"]`，进报告表的还是步骤 3 写的那份），
    且步骤 3 输出的条数与最终树的条数逐份完全相同；
  * 后果是报告里自相矛盾：标准栏写着「无法判断」，而同行的实际得分已经落地
    （批次 16 之后步骤 7 的落地率是 24/26）。

本批不改程序去「清洗」这些字段——那样会把信息抹掉、也掩盖模型行为——
只做两件事：**提示词把结论请出字段**，**程序侧把命中数记成可观测指标**。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.prompts.default_prompts import STEP3_SYSTEM_TEMPLATE


# ═══════════════════════════════════════════════════════
#  构造器
# ═══════════════════════════════════════════════════════

def _leaf(n, standard="满分条件明确，缺项按比例扣分。", verdict=False):
    if verdict:
        standard = f"⚠️ 现有资料中未提供第 {n} 项凭证，无法判断。"
    return {
        "id": f"L3_01_01_{n:02d}", "level": 3, "name": f"指标{n}", "parent_id": "L2_01_01",
        "score": 5.0, "explanation": "解释", "scoring_standard": standard, "children": [],
    }


def _tree(leaves):
    return {
        "indicator_type": "项目支出指标体系",
        "level1_indicators": [{
            "id": "L1_01", "level": 1, "name": "决策", "order": 1, "score": 20,
            "children": [{
                "id": "L2_01_01", "level": 2, "name": "项目立项", "parent_id": "L1_01",
                "score": 20, "children": list(leaves),
            }],
        }],
    }


def _fence(payload):
    return "指标体系如下。\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"


# ═══════════════════════════════════════════════════════
#  一、提示词：把结论请出字段、把缺口留在正文
# ═══════════════════════════════════════════════════════

def test_step3_forbids_verdict_inside_scoring_standard():
    """锚定条文本身，不用泛词：全模板里「不得」「资料核对结论」都有别处出现，
    只查这两个词的话，把这句禁令删掉照样能过。"""
    assert "**严禁**把本项目的资料核对结论写进" in STEP3_SYSTEM_TEMPLATE


def test_step3_lists_the_forbidden_wording_explicitly():
    """「资料不足」这种话模型最爱写，必须点名禁掉，不能只说「别写结论」。"""
    for word in ("待核实", "无法判断", "资料不足", "未提供"):
        assert word in STEP3_SYSTEM_TEMPLATE, f"提示词没有点名禁掉「{word}」"
    assert "严禁" in STEP3_SYSTEM_TEMPLATE


def test_step3_gives_a_before_after_example():
    """只给规则不给例子，模型会把禁令理解成「少写几个字」。改前实测的形态必须被当作反例点名。"""
    assert "反例" in STEP3_SYSTEM_TEMPLATE and "正例" in STEP3_SYSTEM_TEMPLATE


def test_step3_requires_gap_section_outside_json():
    assert "资料核对提示" in STEP3_SYSTEM_TEMPLATE
    assert "JSON 之外" in STEP3_SYSTEM_TEMPLATE or "json 之外" in STEP3_SYSTEM_TEMPLATE


def test_step3_forbids_dropping_indicators_to_look_clean():
    """信息不能靠删指标来「变干净」——必须明说。"""
    assert "不得成为删减指标的理由" in STEP3_SYSTEM_TEMPLATE


def test_step3_json_contract_is_untouched():
    """改的是字段写法，不是输出契约：三级结构与字段名不能动。"""
    for field in ('"id"', '"level"', '"name"', '"score"', '"explanation"',
                  '"scoring_standard"', '"children"'):
        assert field in STEP3_SYSTEM_TEMPLATE, field
    assert "{{" in STEP3_SYSTEM_TEMPLATE, "大括号必须仍是转义态，否则 .format 会炸"


# ═══════════════════════════════════════════════════════
#  二、程序侧审计：只计数，不动原文
# ═══════════════════════════════════════════════════════

def test_audit_counts_leaves_carrying_verdict():
    purity = performance_agent.audit_standard_purity(
        _tree([_leaf(1), _leaf(2, verdict=True), _leaf(3, verdict=True)]))
    assert purity["leaves"] == 3
    assert purity["with_verdict"] == 2
    assert purity["ids"] == ["L3_01_01_02", "L3_01_01_03"]


def test_audit_is_zero_for_clean_tree():
    purity = performance_agent.audit_standard_purity(_tree([_leaf(1), _leaf(2)]))
    assert (purity["leaves"], purity["with_verdict"], purity["ids"]) == (2, 0, [])


def test_audit_never_rewrites_the_standard():
    """审计不许静默清洗：原文一个字都不能动，否则等于把模型行为藏起来。"""
    dirty = _tree([_leaf(1, verdict=True)])
    before = dirty["level1_indicators"][0]["children"][0]["children"][0]["scoring_standard"]
    performance_agent.audit_standard_purity(dirty)
    after = dirty["level1_indicators"][0]["children"][0]["children"][0]["scoring_standard"]
    assert after == before


def test_audit_only_looks_at_leaves():
    """一二级节点没有 scoring_standard，别把它们凑进分母。"""
    tree = _tree([_leaf(1)])
    tree["level1_indicators"][0]["scoring_standard"] = "待核实"
    purity = performance_agent.audit_standard_purity(tree)
    assert purity["leaves"] == 1


def test_audit_tolerates_missing_and_null_fields():
    leaf = _leaf(1)
    leaf["scoring_standard"] = None
    other = _leaf(2)
    other.pop("scoring_standard")
    purity = performance_agent.audit_standard_purity(_tree([leaf, other]))
    assert (purity["leaves"], purity["with_verdict"]) == (2, 0)


def test_audit_handles_empty_system():
    assert performance_agent.audit_standard_purity({})["leaves"] == 0
    assert performance_agent.audit_standard_purity(None)["leaves"] == 0


# ═══════════════════════════════════════════════════════
#  三、节点级联动：审计不拦树，只记账
# ═══════════════════════════════════════════════════════

def _agent(monkeypatch, outputs):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "purity-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    queue = list(outputs)
    monkeypatch.setattr(agent, "_execute_step", lambda state, step: queue.pop(0))
    # 修复是一次内部补调；这些用例只关心首次输出，让修复拿不到内容即可。
    monkeypatch.setattr(agent, "_invoke_llm",
                        lambda messages, step_number, state, **kw: "")
    return agent


def _state(**extra):
    base = {"session_id": "purity-fixture", "completed_steps": [], "step_results": [],
            "warnings": []}
    base.update(extra)
    return base


def test_step3_node_records_purity(monkeypatch):
    tree = _tree([_leaf(1), _leaf(2, verdict=True), _leaf(3, verdict=True), _leaf(4)])
    agent = _agent(monkeypatch, [_fence(tree)])
    state = _state()

    agent._node_step3(state)

    assert state["standard_purity"]["leaves"] == 4
    assert state["standard_purity"]["with_verdict"] == 2
    assert state["indicator_system"]["level1_indicators"], "审计不能把树拦下来"


def test_step3_purity_does_not_raise_a_user_facing_warning(monkeypatch):
    """74% 的历史会话都会命中；这不是「本份材料有问题」，不该刷进 warnings 面板。"""
    agent = _agent(monkeypatch, [_fence(_tree([_leaf(1, verdict=True)]))])
    state = _state()

    agent._node_step3(state)

    assert state["warnings"] == []
    assert state["standard_purity"]["with_verdict"] == 1


def test_step3_clean_output_records_zero(monkeypatch):
    agent = _agent(monkeypatch, [_fence(_tree([_leaf(1), _leaf(2)]))])
    state = _state()

    agent._node_step3(state)

    assert state["standard_purity"] == {"leaves": 2, "with_verdict": 0, "ids": []}


def test_failed_step3_leaves_purity_absent(monkeypatch):
    """树都没建起来时不该留下一个骗人的「0 条命中」。"""
    agent = _agent(monkeypatch, ["资料不足，无法构建指标体系。"])
    state = _state()

    agent._node_step3(state)

    assert "standard_purity" not in state
