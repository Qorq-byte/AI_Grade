"""批次 42：聊天上下文评测本身也要被守。

跑分器最怕两件事：
1. **样本坏了**（空 expect、重复 id、chitchat 也写了 expect）——指标会变得没法解释；
2. **跑分器失去分辨力**（怎么改都 100%）——那就成了摆设。

所以除了「现在 20/20」，这里还断言「关掉章节检索后必须明显掉下来」。
"""
import pytest

from backend.services import chat_context
from tests.chat_eval import fixture, questions, run


def test_cases_are_well_formed():
    cases = questions.CASES

    assert len(cases) >= 20
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "样本 id 重复"
    for case in cases:
        assert case["question"].strip()
        assert case["why"].strip()
        if case["kind"] == "chitchat":
            assert "expect" not in case, f"{case['id']} 是闲聊，不该有 expect"
        else:
            assert case["kind"] == "report"
            assert case.get("expect"), f"{case['id']} 没有期望锚点"


def test_fixture_has_the_structures_the_questions_rely_on():
    state = fixture.state()

    assert state["indicator_system"]["level1_indicators"]
    assert state["step_evidence"]
    assert "（本部分内容尚未生成" in state["final_report"]
    assert state["score_summary"]["total_score"] != 86.0   # 正文 92 / 重算 86，故意不一致


def test_eval_scores_a_hundred_percent_today():
    """现在是满分。改坏了（切块/预算/检索）就该掉下来。"""
    assert run.evaluate()["metrics"]["recall"] == 1.0


def test_eval_can_actually_fail():
    """关掉章节检索，命中率必须明显下降 —— 否则这 20 条问题没在考章节检索。"""
    control = run.evaluate_without_snippets()["metrics"]

    assert control["recall"] <= 0.6, control


def test_baseline_check_passes():
    assert run._check_against_baseline(run.evaluate()) == 0


def test_context_never_exceeds_the_budget():
    for case in questions.CASES:
        context = chat_context.build_report_context(
            fixture.state(), fixture.PROJECT, query=case["question"])
        assert len(context["text"]) <= chat_context.MAX_CONTEXT_CHARS
