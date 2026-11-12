"""缺失类结论的编号清理（批次 52 起，批次 54 扩词表并加台账）。

提示词方案失败（比例 6%→15%），改成组装报告时的确定性清理。
这里钉住：删编号、不删字、不误伤正向引用、可重复执行；
批次 54 再加两条：扩过的词表要真能命中，清理时要把「删了哪句的哪些编号」记下来。
"""
import pytest

from backend.agents.performance_agent import PerformanceAgent
from backend.services import citation_policy
from tests.rag_eval import citation_audit


def test_strips_ids_from_absence_claims():
    text = "立项批复及规划符合性文件缺失（[P1-3-1]对应佐证缺失）"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 1
    assert "[P1-3-1]" not in cleaned
    assert "立项批复及规划符合性文件缺失" in cleaned
    assert "对应佐证缺失" in cleaned


def test_strips_plain_bracket_after_absence_claim():
    cleaned, removed = citation_policy.strip_absence_ids("缺概算及预算批复依据[P1-14-1]")

    assert removed == 1
    assert cleaned == "缺概算及预算批复依据"


def test_keeps_positive_citations():
    text = "财政拨款4000万元，专项债1000万元（[P1-14-1]）。"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 0
    assert cleaned == text


def test_does_not_touch_ids_in_another_sentence():
    text = "立项批复缺失。总投资 5000 万元[P1-3-1]。"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 0
    assert "[P1-3-1]" in cleaned


def test_is_idempotent():
    once, _ = citation_policy.strip_absence_ids("缺合同文本[P1-3-1]")
    twice, removed = citation_policy.strip_absence_ids(once)

    assert removed == 0
    assert twice == once


def test_step14_applies_the_cleanup(monkeypatch):
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent._assemble_final_report = lambda state: "招标文件缺失[P1-3-1]。总投资5000万元[P1-2-1]。"
    monkeypatch.setattr(PerformanceAgent, "_record_step_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(PerformanceAgent, "_run_consistency_check", lambda *args, **kwargs: None)
    state = {"project_name": "P", "step_results": {}, "step_control": {}}

    agent._node_step14(state)

    assert "[P1-3-1]" not in state["final_report"]
    assert "[P1-2-1]" in state["final_report"]


def test_strips_multiple_ids_hanging_on_one_absence_claim():
    text = "缺立项批复与预算依据（[P1-3-1]）（[P1-14-1]）"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 2
    assert "[P1-3-1]" not in cleaned and "[P1-14-1]" not in cleaned
    assert "缺立项批复与预算依据" in cleaned


def test_long_clause_is_still_cleaned():
    """旧口径是"60 字内"，长句会漏；分句口径不该漏。"""
    text = "缺" + "很长的说明" * 30 + "[P1-3-1]"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 1
    assert "[P1-3-1]" not in cleaned


def test_only_the_absence_sentence_is_touched():
    text = "缺立项批复[P1-3-1]。资金构成清楚[P1-14-1]。"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 1
    assert "[P1-3-1]" not in cleaned
    assert "[P1-14-1]" in cleaned


def test_citation_after_a_comma_in_the_same_sentence_is_also_stripped():
    """中文的「缺 X，……（[P1]）」很常见，按逗号切会漏判。"""
    text = "资金到位凭证未提供，相关指标计0分[P1-14-1]。"

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 1
    assert "[P1-14-1]" not in cleaned


def test_step_record_cleans_absence_ids(monkeypatch):
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.model_name = "m"
    state = {"step_results": {}, "step_control": {}}

    agent._record_step_result(state, 9, "立项批复缺失[P1-3-1]。总投资5000万元[P1-3-1]。")

    content = state["step_results"][9]["content"]
    assert "立项批复缺失" in content
    assert content.count("[P1-3-1]") == 1      # 只删缺失分句里的那一个


def test_step7_json_is_left_alone():
    """步骤 7 的正文里是评分 JSON，不在清理范围内（保守）。"""
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.model_name = "m"
    state = {"step_results": {}, "step_control": {}}

    agent._record_step_result(state, 7, '{"reason": "缺佐证[P1-3-1]"}')

    assert "[P1-3-1]" in state["step_results"][7]["content"]


# ═══════════════════════════════════════════════════════
#  批次 54：扩词表（暂无 / 未能获取 / 空白）
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("word, text", [
    ("暂无", "满意度调查数据暂无[P1-12-1]"),
    ("未能获取", "验收合格率资料未能获取[P1-9-1]"),
    ("空白", "工程量确认单一项为空白[P1-9-1]"),
])
def test_expanded_absence_words_are_detected(word, text):
    assert citation_policy.ABSENCE.search(word)

    cleaned, removed = citation_policy.strip_absence_ids(text)

    assert removed == 1
    assert "[" not in cleaned


def test_audit_tool_shares_the_same_vocabulary():
    """抽读脚本与清理实现必须同一个口径 —— 两套词表会把「已清理」又报成违规。"""
    assert citation_audit._ABSENCE is citation_policy.ABSENCE
    assert citation_audit._SENTENCE_SPLIT is citation_policy.SENTENCE_SPLIT
    assert citation_audit._EVIDENCE_ID is citation_policy.EVIDENCE_ID


# ═══════════════════════════════════════════════════════
#  批次 54：清理台账（哪一句、删了哪些编号）
# ═══════════════════════════════════════════════════════

def test_detailed_cleanup_lists_removed_ids_per_sentence():
    cleaned, removals = citation_policy.strip_absence_ids_detailed(
        "立项批复缺失（[P1-3-1]）。总投资5000万元[P1-2-1]。")

    assert [item["removed_ids"] for item in removals] == [["P1-3-1"]]
    assert "立项批复缺失" in removals[0]["sentence"]
    assert cleaned == "立项批复缺失（）。总投资5000万元[P1-2-1]。"


def test_detailed_cleanup_matches_the_plain_one():
    """两个入口只能有一套实现，计数与文本都得一样。"""
    text = "缺立项批复与预算依据（[P1-3-1]）（[P1-14-1]）。资金构成清楚[P1-2-1]。"

    plain_text, plain_count = citation_policy.strip_absence_ids(text)
    detailed_text, removals = citation_policy.strip_absence_ids_detailed(text)

    assert detailed_text == plain_text
    assert sum(len(item["removed_ids"]) for item in removals) == plain_count


def test_cleanup_still_leaves_zero_violations_when_details_are_collected():
    """台账不改变清理结果：同一段文本，清完按审计口径回放仍是 0 条。"""
    text = ("立项批复缺失[P1-3-1]。验收资料未能获取[P1-9-1]。"
            "满意度调查暂无[P1-12-1]。总投资5000万元[P1-2-1]。")

    cleaned, removals = citation_policy.strip_absence_ids_detailed(text)

    assert sum(len(item["removed_ids"]) for item in removals) == 3
    assert citation_audit.absence_violations({"final_report": cleaned}) == []
    # 正向引用必须留着 —— 否则「清理有效」是拿误删换来的
    assert "[P1-2-1]" in cleaned


def test_step_record_writes_the_cleanup_ledger():
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.model_name = "m"
    state = {"step_results": {}, "step_control": {}}

    agent._record_step_result(state, 9, "立项批复缺失[P1-3-1]。总投资5000万元[P1-2-1]。")

    entries = state["citation_cleanup"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["step"] == 9
    assert entry["removed_ids"] == ["P1-3-1"]
    assert "立项批复缺失" in entry["sentence"]
    assert entry["at"]          # 秒级 ISO 时间戳，审计要能对上「什么时候跑的」


def test_cleanup_ledger_accumulates_across_steps():
    """LangGraph 每个节点拿到的是状态副本（B31 的教训）—— 台账不能只剩最后一步。"""
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.model_name = "m"
    state = {"step_results": {}, "step_control": {}}

    agent._record_step_result(state, 9, "缺立项批复[P1-3-1]。")
    agent._record_step_result(state, 10, "缺验收资料[P1-9-1]。")

    assert [item["step"] for item in state["citation_cleanup"]] == [9, 10]


def test_clean_step_leaves_the_ledger_empty():
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.model_name = "m"
    state = {"step_results": {}, "step_control": {}}

    agent._record_step_result(state, 9, "总投资5000万元[P1-2-1]。")

    assert state.get("citation_cleanup") in (None, [])


def test_step14_records_its_own_cleanup(monkeypatch):
    """报告侧的清理发生在 _record_step_result 之前，必须自己记一笔。"""
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent._assemble_final_report = lambda state: "招标文件缺失[P1-3-1]。总投资5000万元[P1-2-1]。"
    monkeypatch.setattr(PerformanceAgent, "_record_step_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(PerformanceAgent, "_run_consistency_check", lambda *args, **kwargs: None)
    state = {"project_name": "P", "step_results": {}, "step_control": {}}

    agent._node_step14(state)

    assert [item["step"] for item in state["citation_cleanup"]] == [14]
    assert state["citation_cleanup"][0]["removed_ids"] == ["P1-3-1"]
