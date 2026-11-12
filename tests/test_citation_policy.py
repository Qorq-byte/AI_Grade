"""缺失类结论的编号清理（批次 52）。

提示词方案失败（比例 6%→15%），改成组装报告时的确定性清理。
这里钉住：删编号、不删字、不误伤正向引用、可重复执行。
"""
from backend.agents.performance_agent import PerformanceAgent
from backend.services import citation_policy


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
