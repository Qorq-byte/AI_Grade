"""步骤间一致性检查：分值加总、得分与等级、报告取数三处是否自相矛盾。

这些断言不验证模型写得好不好，只验证「报告里的数字彼此说得通」这件事**能被程序判定**，
并且不误报——干净样本必须一条 issue 都不产出，否则用户会学会忽略这个提示。
"""
from copy import deepcopy

from backend.agents import performance_agent
from backend.services import consistency


# ═══════════════════════════════════════════════════════
#  构造一个各项都对得上的状态
# ═══════════════════════════════════════════════════════

PROJECT = "某某区农村供水保障工程"
ACTUALS = {"l3_a": 18, "l3_b": 17, "l3_c": 13, "l3_d": 18, "l3_e": 9, "l3_f": 13}  # 合计 88


def _system():
    """满分 100、每级都等于下级之和、且不触发「产出+效益≥60」策略预警的指标体系。"""
    def leaf(ident, name, score):
        return {"id": ident, "level": 3, "name": name, "score": score}

    def mid(ident, name, score, children):
        return {"id": ident, "level": 2, "name": name, "score": score, "children": children}

    def top(ident, name, score, children):
        return {"id": ident, "level": 1, "name": name, "score": score, "children": children}

    return {"level1_indicators": [
        top("l1_a", "决策", 40, [
            mid("l2_a", "立项规范", 40, [
                leaf("l3_a", "立项依据充分性", 20),
                leaf("l3_b", "立项程序规范性", 20),
            ]),
        ]),
        top("l1_b", "过程", 60, [
            mid("l2_b", "资金管理", 35, [
                leaf("l3_c", "资金到位率", 15),
                leaf("l3_d", "资金使用合规性", 20),
            ]),
            mid("l2_c", "组织实施", 25, [
                leaf("l3_e", "管理制度健全性", 10),
                leaf("l3_f", "制度执行有效性", 15),
            ]),
        ]),
    ]}


SCORE_LINE = "综合得分：88.00 分，评价等级：良好"


def _report(score_line=SCORE_LINE, extra=""):
    return (
        f"# {PROJECT}\n\n## 目 录\n一、绩效评价工作开展情况\n二、项目基本情况\n\n---\n\n"
        f"## 一、绩效评价工作开展情况\n\n本次评价依据财政项目支出绩效评价管理办法开展，"
        f"评价组通过资料审查、现场核查与访谈相结合的方式开展工作，评价范围为项目全部资金。\n\n"
        f"## 五、综合评价分析及结论\n\n{score_line}\n\n{extra}"
        f"项目总体执行情况较好，资金使用合规，管理制度基本健全，未发现重大违规问题。"
        f"建议在后续年度进一步细化绩效目标，加强过程监控与结果应用。\n"
    )


def clean_state():
    return {
        "session_id": "consistency-fixture",
        "project_name": PROJECT,
        "indicator_system": _system(),
        "indicator_score_map": {key: {"actual_score": value} for key, value in ACTUALS.items()},
        "score_summary": {"total_score": 88.0, "complete": True, "grade": "良好",
                          "scored_count": 6, "total_count": 6},
        "comprehensive_evaluation": f"{SCORE_LINE}\n\n项目总体执行情况较好，资金使用合规。",
        "final_report": _report(),
        "completed_steps": list(range(1, 15)),
        "document_coverage": {"total_chars": 12000, "used_chars": 10800,
                              "used_ratio": 0.9, "omitted_chars": 1200},
        "warnings": [],
    }


def codes(report):
    return [item["code"] for item in report["issues"]]


def check(state):
    return consistency.check_consistency(state)


# ═══════════════════════════════════════════════════════
#  不误报
# ═══════════════════════════════════════════════════════

def test_clean_state_reports_nothing():
    report = check(clean_state())
    assert report["issues"] == [], codes(report)
    assert report["error_count"] == 0 and report["warning_count"] == 0
    assert consistency.summary_line(report) == ""
    assert report["score"]["leaf_full_total"] == 100
    assert report["score"]["leaf_actual_total"] == 88.0


def test_partial_project_score_is_not_a_score_design_error():
    """项目只拿到 88 分，不是「分值设计不合 100」，不能报错。"""
    state = clean_state()
    state["indicator_score_map"] = {key: {"actual_score": 0} for key in ACTUALS}
    state["score_summary"] = {"total_score": 0.0, "complete": True, "grade": "不合格",
                              "scored_count": 6, "total_count": 6}
    state["comprehensive_evaluation"] = "综合得分：0.00 分，评价等级：不合格"
    state["final_report"] = _report(score_line="综合得分：0.00 分，评价等级：不合格")
    report = check(state)
    assert "INDICATOR_LEAF_TOTAL" not in codes(report)
    assert report["issues"] == [], codes(report)


def test_grade_word_is_not_truncated():
    """「不合格」不能被正则截成「合格」而谎报不一致。"""
    state = clean_state()
    state["score_summary"]["grade"] = "不合格"
    state["comprehensive_evaluation"] = "综合得分：88.00 分，评价等级：不合格"
    state["final_report"] = _report(score_line="综合得分：88.00 分，评价等级：不合格")
    assert "REPORT_GRADE_MISMATCH" not in codes(check(state))


def test_grade_word_mismatch_is_still_detected():
    state = clean_state()
    state["score_summary"]["grade"] = "不合格"
    state["comprehensive_evaluation"] = "综合得分：88.00 分，评价等级：合格"
    state["final_report"] = _report(score_line="综合得分：88.00 分，评价等级：合格")
    assert codes(check(state)) == ["REPORT_GRADE_MISMATCH"]


# ═══════════════════════════════════════════════════════
#  指标体系
# ═══════════════════════════════════════════════════════

def test_missing_indicator_system():
    state = clean_state()
    state["indicator_system"] = {}
    report = check(state)
    assert "INDICATOR_MISSING" in codes(report)
    assert report["issues"][0]["severity"] == "error"


def test_leaf_scores_not_summing_to_100():
    state = clean_state()
    state["indicator_system"]["level1_indicators"][0]["children"][0]["children"][0]["score"] = 15
    report = check(state)
    assert "INDICATOR_LEAF_TOTAL" in codes(report)
    issue = next(item for item in report["issues"] if item["code"] == "INDICATOR_LEAF_TOTAL")
    assert issue["expected"] == 100 and issue["actual"] == 95


def test_hierarchy_mismatch_is_an_error():
    state = clean_state()
    # 一级 40 分，但其三级之和变成 35
    state["indicator_system"]["level1_indicators"][0]["children"][0]["children"][0]["score"] = 15
    report = check(state)
    errors = [item["code"] for item in report["issues"] if item["severity"] == "error"]
    assert any(code.startswith("INDICATOR_ALLOCATION_D") for code in errors), errors


def test_policy_dimension_is_a_warning_not_an_error():
    """产出+效益≥60 是策略预警（score_validator 明确排除在硬校验之外），不能算错误。"""
    state = clean_state()
    level1 = state["indicator_system"]["level1_indicators"]
    level1[0]["name"] = "产出"
    level1[1]["name"] = "效益"
    report = check(state)
    policy = [item for item in report["issues"] if item["code"] == "INDICATOR_ALLOCATION_D4"]
    assert len(policy) == 1 and policy[0]["severity"] == "warning"
    assert report["error_count"] == 0


# ═══════════════════════════════════════════════════════
#  评分汇总
# ═══════════════════════════════════════════════════════

def test_missing_score_summary():
    state = clean_state()
    del state["score_summary"]
    assert codes(check(state)) == ["SCORE_SUMMARY_MISSING"]


def test_stale_summary_when_scores_changed_after_step11():
    state = clean_state()
    state["indicator_score_map"]["l3_a"]["actual_score"] = 20  # 合计变 90
    report = check(state)
    stale = next(item for item in report["issues"] if item["code"] == "SCORE_SUMMARY_STALE")
    assert stale["expected"] == 90.0 and stale["actual"] == 88.0
    assert stale["severity"] == "error"


def test_scored_count_above_total():
    state = clean_state()
    state["score_summary"]["scored_count"] = 7
    assert "SCORE_COUNT_INVALID" in codes(check(state))


def test_partial_score_presented_as_score():
    state = clean_state()
    del state["indicator_score_map"]["l3_f"]
    del state["indicator_score_map"]["l3_e"]
    state["score_summary"] = {"total_score": 66.0, "complete": False, "grade": "待核实",
                              "scored_count": 4, "total_count": 6}
    state["comprehensive_evaluation"] = "综合得分与评价等级：资料不足，待核实"
    state["final_report"] = _report(score_line="综合得分与评价等级：资料不足，待核实")
    report = check(state)
    assert codes(report) == ["PARTIAL_SCORE_PRESENTED"]
    assert report["issues"][0]["severity"] == "warning"


def test_complete_score_but_grade_still_pending():
    state = clean_state()
    state["score_summary"]["grade"] = "待核实"
    state["comprehensive_evaluation"] = "综合得分：88.00 分，评价等级：待核实"
    state["final_report"] = _report(score_line="综合得分：88.00 分，评价等级：待核实")
    assert "GRADE_MISSING" in codes(check(state))


def test_grade_given_without_complete_scoring():
    state = clean_state()
    del state["indicator_score_map"]["l3_f"]
    state["score_summary"] = {"total_score": 75.0, "complete": False, "grade": "合格",
                              "scored_count": 5, "total_count": 6}
    state["comprehensive_evaluation"] = "综合得分：75.00 分，评价等级：合格"
    state["final_report"] = _report(score_line="综合得分：75.00 分，评价等级：合格")
    assert "GRADE_WITHOUT_BASIS" in codes(check(state))


# ═══════════════════════════════════════════════════════
#  报告正文取数
# ═══════════════════════════════════════════════════════

def test_complete_score_missing_from_report():
    state = clean_state()
    state["comprehensive_evaluation"] = "项目总体执行情况较好，资金使用合规。"
    state["final_report"] = _report(score_line="项目总体执行情况较好。")
    assert "REPORT_SCORE_MISSING" in codes(check(state))


def test_pending_word_missing_when_scoring_incomplete():
    state = clean_state()
    del state["indicator_score_map"]["l3_f"]
    state["score_summary"] = {"total_score": 75.0, "complete": False, "grade": "待核实",
                              "scored_count": 5, "total_count": 6}
    state["comprehensive_evaluation"] = "项目总体执行情况较好。"
    state["final_report"] = _report(score_line="项目总体执行情况较好。")
    assert "REPORT_PENDING_MISSING" in codes(check(state))


def test_report_score_mismatch():
    state = clean_state()
    state["comprehensive_evaluation"] = "综合得分：91.50 分，评价等级：良好"
    state["final_report"] = _report(score_line="综合得分：91.50 分，评价等级：良好")
    report = check(state)
    issue = next(item for item in report["issues"] if item["code"] == "REPORT_SCORE_MISMATCH")
    assert issue["expected"] == 88.0 and issue["actual"] == 91.5


def test_same_claim_in_report_is_reported_once():
    """最终报告整章并入步骤 11 的正文，同一个矛盾只能报一条。"""
    state = clean_state()
    state["comprehensive_evaluation"] = "综合得分：91.50 分，评价等级：良好"
    state["final_report"] = _report(score_line="综合得分：91.50 分，评价等级：良好")
    assert codes(check(state)).count("REPORT_SCORE_MISMATCH") == 1


# ═══════════════════════════════════════════════════════
#  步骤与覆盖度
# ═══════════════════════════════════════════════════════

def test_report_built_with_incomplete_steps():
    state = clean_state()
    state["completed_steps"] = [step for step in range(1, 15) if step != 9]
    report = check(state)
    issue = next(item for item in report["issues"] if item["code"] == "REPORT_WITH_INCOMPLETE_STEPS")
    assert issue["steps"] == [9]


def test_report_too_short():
    state = clean_state()
    state["final_report"] = "太短了。"
    assert "REPORT_TOO_SHORT" in codes(check(state))


def test_project_name_absent_from_report():
    state = clean_state()
    state["project_name"] = "另一个项目"
    assert "PROJECT_NAME_MISMATCH" in codes(check(state))


def test_low_document_coverage():
    state = clean_state()
    state["document_coverage"] = {"total_chars": 42363, "used_chars": 9999,
                                  "used_ratio": 0.236, "omitted_chars": 32364}
    report = check(state)
    issue = next(item for item in report["issues"] if item["code"] == "LOW_DOCUMENT_COVERAGE")
    assert issue["actual"] == "23.6%"
    assert report["error_count"] == 0


# ═══════════════════════════════════════════════════════
#  接入工作流
# ═══════════════════════════════════════════════════════

def test_step14_writes_result_into_state_and_warnings():
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    state = clean_state()
    state["indicator_score_map"]["l3_a"]["actual_score"] = 20  # 制造一处过期汇总

    report = agent._run_consistency_check(state)

    assert state["consistency"] is report
    assert "SCORE_SUMMARY_STALE" in codes(report)
    assert len(state["warnings"]) == 1
    assert state["warnings"][0].startswith("步骤间一致性检查：1 项错误")


def test_step14_does_not_duplicate_warning_on_rerun():
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    state = clean_state()
    state["indicator_score_map"]["l3_a"]["actual_score"] = 20
    agent._run_consistency_check(state)
    agent._run_consistency_check(state)
    assert len(state["warnings"]) == 1


def test_clean_state_adds_no_warning():
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    state = clean_state()
    agent._run_consistency_check(state)
    assert state["warnings"] == []
    assert state["consistency"]["issues"] == []


def test_check_does_not_mutate_state():
    state = clean_state()
    before = deepcopy(state)
    check(state)
    assert state == before


def test_node_step14_attaches_report_to_state():
    """真正走 step14 的代码路径：组装报告 → 检查 → 写 state 与 warnings。"""
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    recorded = []
    agent._record_step_result = lambda state, step, content: recorded.append(step)
    state = clean_state()
    state["final_report"] = ""
    state["completed_steps"] = []          # 报告已生成但步骤记录缺失，应当被报出来

    result = agent._node_step14(state)

    assert recorded == [14]
    assert result["final_report"].startswith(f"# {PROJECT}")
    assert result["consistency"]["checked"] == list(consistency.CHECKS)
    assert "REPORT_WITH_INCOMPLETE_STEPS" in codes(result["consistency"])
    assert result["warnings"][0].startswith("步骤间一致性检查：")
