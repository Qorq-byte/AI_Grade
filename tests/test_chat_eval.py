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


def test_eval_does_not_drop_below_the_known_floor():
    """扩到 40 条后是 39/40 —— 唯一失分是 Q33「评价用了哪些方法」：

    夹具正文写的是「采用资料审查、现场核查与问卷相结合的**方式**」，
    问题问的是「**方法**」，BM25 没有同义词，命中不到。
    这是已知缺陷（见 B47 记录），不是回归；门槛设在 0.95，
    低于它才说明切块/预算/检索真的坏了。
    """
    assert run.evaluate()["metrics"]["recall"] >= 0.95


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


def test_live_detector_matches_full_evidence_ids():
    """真实证据编号是 `P1-3-1`；只匹配 `P1` 会把真实引用误判成编造（踩过）。"""
    from tests.chat_eval import live

    found = live.REF_RE.findall("见 [P1-3-1] 与（S11P0）、PARTIAL_SCORE_PRESENTED#1、[P2] 和（R37）")

    assert found == ["P1-3-1", "S11P0", "PARTIAL_SCORE_PRESENTED#1", "P2", "R37"]


def test_live_detector_ignores_plain_p_numbers_when_refs_have_none():
    """「建议0（P0）」不是证据编号；只有上下文里真有裸 P1 时才当引用。"""
    from tests.chat_eval import live

    assert live._cited_ids("（P0）与（P1-3-1）", {"P1-3-1"}) == {"P1-3-1"}
    assert live._cited_ids("（P2）", {"P1", "P2"}) == {"P2"}
