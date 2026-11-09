"""批次 21 / A8：步骤 5 / 6 的输出瘦身与覆盖度（Batch 21）。

改前实测（29 份备份 + `token_usage` + 一份真实报告）：

  * **步骤 6 有一半是重复的 JSON**：29/29 的输出末尾都带一段 JSON 副本，
    字数 3,390–12,576（中位 6,218），而它要求写两遍的内容（Markdown 表格）
    本来就已经在前面了。没有任何代码解析这段 JSON（`grep json.loads` 为零），
    但它被原样拼进报告第三章 —— 会话 `8b8443a3` 的报告 37,727 字里有
    **9,755 字是裸 JSON 转储**，占 26%。
  * **步骤 5 是撞上限最狠的一步**：最大 10,352 token，2 次正好停在 8,192，
    7/28 次 ≥7,800；最长的输出 24,570 字。它按名字覆盖住了全部叶子指标，
    所以上一批把「`scoring_standards` 只剩 1 条」当成截断症状是**判断错了**——
    `_node_step5` 恒写 `{"raw": content}`，1 个键是设计如此。

所以这里锁四件事：
  1. 步骤 6 只出表格，不再要 JSON 副本（并写明理由，避免以后又被加回来）；
  2. 步骤 5 每个指标限行数、不重复指标解释与分值、不写「使用说明」这类尾巴；
  3. 步骤 5/6 也进 `STEP_OUTPUT_BUDGETS`（一个上限，撞了要可预测）；
  4. 覆盖度只统计不改写：有几个叶子在产出里真的出现了，缺了谁要报出来。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.prompts.default_prompts import (
    PromptTemplateFactory,
    TemperatureProfile,
)


def _step5_system() -> str:
    return str(PromptTemplateFactory.build_system_message(5, TemperatureProfile.NORMAL).content)


def _step6_system() -> str:
    return str(PromptTemplateFactory.build_system_message(6, TemperatureProfile.NORMAL).content)


def _step6_human() -> str:
    return PromptTemplateFactory.build_raw_human_prompt(6)


# ═══════════════════════════════════════════════════════
#  一、步骤 6：只出表格，不再写第二遍 JSON
# ═══════════════════════════════════════════════════════

def test_step6_template_drops_json_duplicate():
    """模板原来同时要 Markdown 表格与 JSON 副本——同一批内容写两遍，占掉一半额度。"""
    text = _step6_system()
    assert "同时附带结构化的JSON数据" not in text
    assert "不要输出 JSON" in text


def test_step6_template_explains_why_no_json():
    """只写「不要 JSON」会被后来的维护者当成洁癖改回去，必须写清代价。"""
    text = _step6_system()
    assert "报告第三章" in text
    assert "没有任何程序解析" in text


def test_step6_template_still_requires_full_table():
    """删 JSON 不许顺手削弱表格本身：六列与层级要求必须还在。"""
    text = _step6_system()
    assert "一级指标、二级指标、三级指标、指标解释、分值、评分标准" in text
    assert "Markdown 表格" in text


def test_step6_human_keeps_single_output_format():
    """Human 侧只要求「生成完整的表」，不该再出现「另外附一份 JSON」这种双写要求。"""
    raw = _step6_human()
    assert "请生成完整的评价指标体系表" in raw
    assert "附带" not in raw
    assert "同时" not in raw


# ═══════════════════════════════════════════════════════
#  二、步骤 5：每指标限行数
# ═══════════════════════════════════════════════════════

def test_step5_template_limits_lines_per_indicator():
    text = _step5_system()
    assert "每个指标不超过 6 行" in text
    assert "不要重复指标解释" in text


def test_step5_template_forbids_trailing_essay():
    """实测最长一份 24,570 字，尾部是「使用说明与限制条件」这种与打分无关的尾巴。"""
    text = _step5_system()
    assert "不要写「使用说明」「总体说明」" in text


def test_step5_template_keeps_level1_grouping():
    """限长时最容易顺手丢掉的结构：分组。没有它，29 条标准读起来是一长串。"""
    text = _step5_system()
    assert "按一级指标分组" in text
    assert "不写编制说明" in text


def test_step5_template_keeps_scoring_requirements():
    """限长的同时不许丢掉可判分性：档次、满分条件、扣分幅度都要留着。"""
    text = _step5_system()
    assert "档次" in text
    assert "满分条件" in text
    assert "扣分幅度" in text


# ═══════════════════════════════════════════════════════
#  三、预算：三步都要有上限
# ═══════════════════════════════════════════════════════

def test_budget_covers_steps_5_6_and_7():
    assert performance_agent.STEP_OUTPUT_BUDGETS == {5: 8000, 6: 6000, 7: 6000}


def test_budget_for_step5_below_observed_stop_point():
    """步骤 5 历史上有 2 次停在 8,192，给 8,000 是为了「可预测地失败」，不是省钱。"""
    assert performance_agent.step_output_budget(5) == 8000
    assert performance_agent.step_output_budget(6) == 6000


def test_budget_applies_extra_body_for_steps_5_6(monkeypatch):
    llm = _Recorder()
    agent = _agent(monkeypatch, llm)
    for step in (5, 6):
        bound = agent._llm_for_step(step)
        assert bound.bind_kwargs["extra_body"] == {"max_tokens": performance_agent.step_output_budget(step)}


# ═══════════════════════════════════════════════════════
#  四、覆盖度审计：只统计，不改写
# ═══════════════════════════════════════════════════════

def _tree(leaf_names):
    leaves = [{
        "id": f"L3_01_01_{index:02d}", "level": 3, "name": name, "parent_id": "L2_01_01",
        "score": 4, "explanation": "说明", "scoring_standard": "标准", "children": [],
    } for index, name in enumerate(leaf_names, 1)]
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


def test_step5_coverage_counts_covered_leaves():
    system = _tree(["立项依据充分性", "绩效目标合理性"])
    text = "### 立项依据充分性（4分）\n充分得4分。\n\n### 绩效目标合理性（4分）\n合理得4分。"
    audit = performance_agent.audit_step5_coverage(system, text)
    assert audit["leaves"] == 2
    assert audit["covered"] == 2
    assert audit["missing_ids"] == []


def test_step5_coverage_reports_missing_ids():
    system = _tree(["立项依据充分性", "绩效目标合理性"])
    text = "### 立项依据充分性（4分）\n充分得4分。"
    audit = performance_agent.audit_step5_coverage(system, text)
    assert audit["covered"] == 1
    assert audit["missing_ids"] == ["L3_01_01_02"]


def test_step5_coverage_is_empty_without_tree():
    audit = performance_agent.audit_step5_coverage({}, "随便写点什么")
    assert audit["leaves"] == 0
    assert audit["covered"] == 0


def test_step6_table_counts_rows_and_names():
    system = _tree(["立项依据充分性", "绩效目标合理性"])
    text = ("| 一级指标 | 二级指标 | 三级指标 | 指标解释 | 分值 | 评分标准 |\n"
            "|---|---|---|---|---|---|\n"
            "| 决策 | 项目立项 | 立项依据充分性 | 说明 | 4 | 标准 |\n"
            "| 决策 | 项目立项 | 绩效目标合理性 | 说明 | 4 | 标准 |\n")
    audit = performance_agent.audit_step6_table(system, text)
    assert audit["leaves"] == 2
    assert audit["covered"] == 2
    assert audit["rows"] == 2
    assert audit["missing_ids"] == []


def test_step6_table_detects_dropped_rows():
    """表格少了指标，报告第三、四章就对不上——这是必须报出来的真问题。"""
    system = _tree(["立项依据充分性", "绩效目标合理性"])
    text = ("| 一级指标 | 二级指标 | 三级指标 | 指标解释 | 分值 | 评分标准 |\n"
            "|---|---|---|---|---|---|\n"
            "| 决策 | 项目立项 | 立项依据充分性 | 说明 | 4 | 标准 |\n")
    audit = performance_agent.audit_step6_table(system, text)
    assert audit["rows"] == 1
    assert audit["missing_ids"] == ["L3_01_01_02"]


# ═══════════════════════════════════════════════════════
#  五、节点接线与备份白名单
# ═══════════════════════════════════════════════════════

def _agent(monkeypatch, llm=None, model_name="deepseek-chat", session_id=""):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = session_id
    agent.model_name = model_name
    agent.base_agent = SimpleNamespace(llm=llm)
    monkeypatch.setattr(agent, "_record_token", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_record_usage", lambda *a, **k: None)
    return agent


class _Recorder:
    def __init__(self):
        self.bind_kwargs = None

    def bind(self, **kwargs):
        clone = _Recorder()
        clone.bind_kwargs = kwargs
        return clone


def _run_node(monkeypatch, step, text, leaf_names):
    agent = _agent(monkeypatch)
    monkeypatch.setattr(agent, "_execute_step", lambda state, number: text)
    state = {
        "session_id": f"step{step}-fixture",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "indicator_system": _tree(leaf_names),
    }
    getattr(agent, f"_node_step{step}")(state)
    return state


def test_node_step5_writes_coverage(monkeypatch):
    state = _run_node(monkeypatch, 5, "### 立项依据充分性\n充分得4分。", ["立项依据充分性"])
    assert state["step5_coverage"]["leaves"] == 1
    assert state["step5_coverage"]["covered"] == 1
    assert state["scoring_standards"] == {"raw": "### 立项依据充分性\n充分得4分。"}


def test_node_step5_warns_when_leaf_missing(monkeypatch):
    state = _run_node(monkeypatch, 5, "### 立项依据充分性\n充分得4分。",
                      ["立项依据充分性", "绩效目标合理性"])
    assert state["step5_coverage"]["missing_ids"] == ["L3_01_01_02"]
    assert any("评分标准" in w and "1" in w for w in state["warnings"])


def test_node_step5_silent_when_complete(monkeypatch):
    state = _run_node(monkeypatch, 5, "### 立项依据充分性\n充分得4分。", ["立项依据充分性"])
    assert not any("评分标准" in w for w in state["warnings"])


def test_node_step6_writes_table_audit(monkeypatch):
    text = ("| 一级指标 | 二级指标 | 三级指标 | 指标解释 | 分值 | 评分标准 |\n"
            "|---|---|---|---|---|---|\n"
            "| 决策 | 项目立项 | 立项依据充分性 | 说明 | 4 | 标准 |\n")
    state = _run_node(monkeypatch, 6, text, ["立项依据充分性"])
    assert state["evaluation_table"] == text
    assert state["step6_table"]["rows"] == 1
    assert state["step6_table"]["covered"] == 1


def test_node_step6_warns_when_row_missing(monkeypatch):
    text = ("| 一级指标 | 二级指标 | 三级指标 | 指标解释 | 分值 | 评分标准 |\n"
            "|---|---|---|---|---|---|\n"
            "| 决策 | 项目立项 | 立项依据充分性 | 说明 | 4 | 标准 |\n")
    state = _run_node(monkeypatch, 6, text, ["立项依据充分性", "绩效目标合理性"])
    assert any("评价指标体系表" in w for w in state["warnings"])


def test_backup_persists_step56_fields():
    captured = {}
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "backup-fixture"
    agent.update_state_backup = lambda sid, backup: captured.update(backup)
    agent._persist_state_backup({
        "session_id": "backup-fixture",
        "step5_coverage": {"leaves": 2, "covered": 2},
        "step6_table": {"rows": 2, "covered": 2},
    })
    assert captured["step5_coverage"]["covered"] == 2
    assert captured["step6_table"]["rows"] == 2
