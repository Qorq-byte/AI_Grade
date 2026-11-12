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
