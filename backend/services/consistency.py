"""步骤间一致性检查：纯程序校验，不调用模型、不花 token。

要解决的问题：报告里的数字来自 14 个步骤，彼此之间没有任何程序校验。
真实会出错的典型情形（不是编造，都能在现有代码里找到来源）：

  1. 指标分值不合 100（模型设计漏项）——但评分照常汇总，报告写「总分 92 分」；
  2. `score_summary` 与 `/score-summary` 接口是**两套独立实现**，口径一旦漂移，
     报告里是 91.50、界面上的换算分却是另一个数；
  3. 只有 2/20 个指标可核实时，`total_score` 仍是 5.00 这类小数，
     但等级是「待核实」——数字和等级自相矛盾，容易被当成「得分 5 分」；
  4. 模型在正文里自己写的「综合得分：88.5 分」与程序算出的分不一致。

本模块只做一件事：把这些说出来，附上出处步骤与两个数字，让人能判断。
判定口径刻意保守——**只在两处数字确实对不上时报**，不猜测、不推断缺失内容。

批次 26 起，结论多了两个字段，供「能不能导出」这类动作使用：

- `id`：稳定编号（`CODE#序号`），日志、工单、前端提示都能引用同一条；
- `blocking`：是否必须处理完才能导出。**只给结构性缺陷**（指标体系缺失、步骤没跑完、
  报告过短、有章节还是占位符），分值口径不一致这类只提示、不拦人 ——
  拦得太宽，用户会学会绕过它。
"""
import re
from typing import Any, Dict, List, Optional

from backend.agents.tools.questionnaire_gen import indicator_leaves
from backend.agents.tools.score_validator import (
    actual_indicator_score,
    score_number,
    validate_indicator_scores,
)

TOLERANCE = 0.01
# 正文里的数字允许一位小数的四舍五入差异
TEXT_TOLERANCE = 0.1
REQUIRED_STEPS = tuple(range(1, 15))
MIN_REPORT_CHARS = 200
# 分值校验的维度 4（产出+效益<60）是策略预警，不是硬错误，见 score_validator.allocation_valid
POLICY_DIMENSIONS = (4,)

#: `_assemble_final_report` 给空章节写的占位文案 —— 缺章的确定标记。
#: 故意不带右括号：步骤失败时写的是「（本部分内容尚未生成，请检查AI模型配置后重新运行工作流）」，
#: 那一句同样是缺章，不能漏。
EMPTY_CHAPTER_MARKER = "（本部分内容尚未生成"
#: 得分率低于这个值的指标，应当被问题分析与改进建议点名
LOW_SCORE_RATIO = 0.6
#: 指标名太短（「进度」「质量」）时子串匹配没有意义，跳过
MIN_INDICATOR_NAME_CHARS = 3

#: 必须处理完才能导出的问题。只放结构性缺陷，见模块文档最后一段
BLOCKING_CODES = frozenset({
    "INDICATOR_MISSING",
    "REPORT_WITH_INCOMPLETE_STEPS",
    "REPORT_TOO_SHORT",
    "REPORT_CHAPTER_MISSING",
})

_SCORE_IN_TEXT = re.compile(r"(?:综合得分|总得分|综合评分)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*分")
# 长词优先，避免「不合格」被截成「合格」
_GRADE_IN_TEXT = re.compile(r"(?:评价等级|综合等级|评定等级)[：:\s]*(不合格|待核实|优秀|良好|合格)")
_CHAPTER_LINE = re.compile(r"^##\s*(\S.*)$")
_PENDING_WORD = "待核实"


def _issue(code: str, severity: str, message: str, steps: Optional[List[int]] = None,
           expected: Any = None, actual: Any = None) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "message": message,
            "steps": list(steps or []), "expected": expected, "actual": actual}


def _leaf_full_total(state: Dict[str, Any]) -> Optional[float]:
    """三级指标的**满分**合计，判定「分值设计是否为 100」。"""
    system = state.get("indicator_system") or {}
    leaves = list(indicator_leaves(system.get("level1_indicators") or []))
    if not leaves:
        return None
    return round(sum(score_number(item.get("score", item.get("max_score"))) or 0
                     for item in leaves), 2)


def _leaf_actual_total(state: Dict[str, Any]) -> Optional[float]:
    """三级指标的**实际得分**合计，用于与落库的 score_summary 对账。"""
    system = state.get("indicator_system") or {}
    leaves = list(indicator_leaves(system.get("level1_indicators") or []))
    if not leaves:
        return None
    scores = state.get("indicator_score_map") or {}
    return round(sum(actual_indicator_score(item, scores) or 0 for item in leaves), 2)


def _level1_total(state: Dict[str, Any]) -> Optional[float]:
    level1 = (state.get("indicator_system") or {}).get("level1_indicators") or []
    if not level1:
        return None
    return round(sum(score_number(item.get("score")) or 0 for item in level1), 2)


def _check_indicator_allocation(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    system = state.get("indicator_system") or {}
    if not system.get("level1_indicators"):
        return [_issue("INDICATOR_MISSING", "error",
                       "步骤 3 没有产出指标体系，后续评分与报告缺少依据", [3])]

    issues: List[Dict[str, Any]] = []
    validation = validate_indicator_scores(system)
    for detail in validation.get("validations", []):
        if detail.get("passed"):
            continue
        dimension = detail.get("dimension")
        policy = dimension in POLICY_DIMENSIONS
        issues.append(_issue(
            f"INDICATOR_ALLOCATION_D{dimension}", "warning" if policy else "error",
            f"指标分值{'策略预警' if policy else '不合规'}"
            f"（第{dimension}维：{detail.get('message')}）："
            f"{detail.get('details') or '细则缺失'}",
            [3, 4, 6],
        ))

    full_total = _leaf_full_total(state)
    if full_total is not None and abs(full_total - 100) > TOLERANCE:
        issues.append(_issue(
            "INDICATOR_LEAF_TOTAL", "error",
            f"三级指标满分合计 {full_total}，不等于 100；按此汇总的得分与等级没有可比性",
            [3, 4, 6, 11],
            expected=100, actual=full_total,
        ))
    return issues


def _check_score_summary(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = state.get("score_summary") or {}
    if not summary:
        return [_issue("SCORE_SUMMARY_MISSING", "error",
                       "没有程序评分汇总（步骤 11 未执行或未落库），报告中的得分无法核对", [11])]

    issues: List[Dict[str, Any]] = []
    total = summary.get("total_score")
    complete = bool(summary.get("complete"))
    grade = summary.get("grade")
    scored = summary.get("scored_count")
    count = summary.get("total_count")

    leaves_total = _leaf_actual_total(state)
    if isinstance(total, (int, float)) and leaves_total is not None \
            and abs(total - leaves_total) > TOLERANCE:
        issues.append(_issue(
            "SCORE_SUMMARY_STALE", "error",
            f"落库的综合得分 {total} 与按当前指标/评分重算的 {leaves_total} 不一致，"
            "报告里引用的是旧数字",
            [4, 11], expected=leaves_total, actual=total,
        ))

    if isinstance(scored, int) and isinstance(count, int) and scored > count:
        issues.append(_issue(
            "SCORE_COUNT_INVALID", "error",
            f"已评分指标数 {scored} 超过指标总数 {count}", [7, 11],
            expected=count, actual=scored,
        ))

    if not complete and isinstance(total, (int, float)) and total > 0:
        issues.append(_issue(
            "PARTIAL_SCORE_PRESENTED", "warning",
            f"仅 {scored}/{count} 个指标可核实，综合得分仍算出 {total}；"
            "对外只能表述为「待核实」，不能写成得分",
            [11, 14], expected=_PENDING_WORD, actual=f"{total} 分",
        ))

    if complete and grade == _PENDING_WORD:
        issues.append(_issue(
            "GRADE_MISSING", "error",
            "评分已完整，但等级仍为「待核实」", [11], expected="优秀/良好/合格/不合格", actual=grade,
        ))
    if not complete and grade not in (_PENDING_WORD, None):
        issues.append(_issue(
            "GRADE_WITHOUT_BASIS", "error",
            f"评分不完整却给出了等级「{grade}」", [11], expected=_PENDING_WORD, actual=grade,
        ))
    return issues


def _extract_text_claims(state: Dict[str, Any]) -> List[tuple]:
    """从步骤 11 正文与最终报告里抽出模型自己写的得分/等级。

    最终报告把步骤 11 的正文整章并入，同一句话会出现两次；按 (类型, 值) 去重，
    只保留最早出现的出处，避免同一个矛盾被报两条。
    """
    claims: List[tuple] = []
    seen = set()
    for label, key, step in (("综合评价", "comprehensive_evaluation", 11),
                             ("最终报告", "final_report", 14)):
        text = state.get(key) or ""
        if not text:
            continue
        found = [("score", float(match.group(1))) for match in _SCORE_IN_TEXT.finditer(text)]
        found += [("grade", match.group(1)) for match in _GRADE_IN_TEXT.finditer(text)]
        for kind, value in found:
            token = (kind, value)
            if token in seen:
                continue
            seen.add(token)
            claims.append((kind, value, label, step))
    return claims


def _check_report_text(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = state.get("score_summary") or {}
    total = summary.get("total_score")
    grade = summary.get("grade")
    complete = bool(summary.get("complete"))
    issues: List[Dict[str, Any]] = []

    claims = _extract_text_claims(state)
    has_score = any(kind == "score" for kind, *_ in claims)
    text = f"{state.get('comprehensive_evaluation') or ''}\n{state.get('final_report') or ''}"

    if summary and complete and not has_score:
        issues.append(_issue(
            "REPORT_SCORE_MISSING", "warning",
            "报告正文里找不到「综合得分：X 分」，读者无法从报告本身得知结论分值",
            [11, 14],
        ))
    # 评分不完整时正确的写法是「待核实」而不是分数，缺的正是这句话
    if summary and not complete and _PENDING_WORD not in text:
        issues.append(_issue(
            "REPORT_PENDING_MISSING", "warning",
            "评分不完整，但报告正文没有出现「待核实」，读者会误以为已有结论分值",
            [11, 14], expected=_PENDING_WORD, actual="未出现",
        ))

    for kind, value, label, step in claims:
        if kind == "score" and isinstance(total, (int, float)) \
                and abs(float(value) - float(total)) > TEXT_TOLERANCE:
            issues.append(_issue(
                "REPORT_SCORE_MISMATCH", "warning",
                f"{label}写的是 {value} 分，程序汇总为 {total} 分", [step, 11],
                expected=total, actual=value,
            ))
        if kind == "grade" and grade and value != grade:
            issues.append(_issue(
                "REPORT_GRADE_MISMATCH", "warning",
                f"{label}写的是「{value}」，程序判定为「{grade}」", [step, 11],
                expected=grade, actual=value,
            ))
    return issues


def _check_steps_and_coverage(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    completed = {int(step) for step in (state.get("completed_steps") or [])}
    report = state.get("final_report") or ""

    if report:
        missing = [step for step in REQUIRED_STEPS if step not in completed]
        if missing:
            issues.append(_issue(
                "REPORT_WITH_INCOMPLETE_STEPS", "error",
                f"报告已生成，但步骤 {missing} 未完成；缺章节会被静默跳过", missing,
            ))
        if len(report.strip()) < MIN_REPORT_CHARS:
            issues.append(_issue(
                "REPORT_TOO_SHORT", "error",
                f"最终报告仅 {len(report.strip())} 字，不足以构成完整报告", [14],
                expected=f"≥{MIN_REPORT_CHARS} 字", actual=len(report.strip()),
            ))
        project_name = state.get("project_name") or ""
        if project_name and project_name not in report:
            issues.append(_issue(
                "PROJECT_NAME_MISMATCH", "warning",
                f"报告标题/正文里没有出现项目名称「{project_name}」，可能张冠李戴", [14],
            ))

    coverage = state.get("document_coverage") or {}
    ratio = coverage.get("used_ratio")
    if isinstance(ratio, (int, float)) and ratio < 0.5 and coverage.get("omitted_chars"):
        issues.append(_issue(
            "LOW_DOCUMENT_COVERAGE", "warning",
            f"报告依据的资料覆盖率仅 {ratio * 100:.1f}%"
            f"（未直接引用 {coverage.get('omitted_chars')} 字），结论的代表性有限",
            list(REQUIRED_STEPS),
            expected="≥50%", actual=f"{ratio * 100:.1f}%",
        ))
    return issues


CHECKS = ("indicator_allocation", "score_summary", "report_text", "chapters",
          "problem_suggestion_coverage", "steps_and_coverage")


def _missing_chapters(report: str) -> List[str]:
    """报告里「只有占位符」的章节标题。

    `_assemble_final_report` 对空章节不跳过、写一句 `EMPTY_CHAPTER_MARKER`，
    所以这句话就是缺章的确定证据 —— 不用猜章节是否「内容太少」。
    """
    titles: List[str] = []
    current = ""
    for line in report.splitlines():
        match = _CHAPTER_LINE.match(line.strip())
        if match:
            current = match.group(1).strip()
            continue
        if EMPTY_CHAPTER_MARKER in line and current and current not in titles:
            titles.append(current)
            current = ""
    return titles


def _check_chapters(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    report = state.get("final_report") or ""
    if not report:
        return []
    count = report.count(EMPTY_CHAPTER_MARKER)
    if not count:
        return []
    titles = _missing_chapters(report)
    named = "、".join(titles[:5]) if titles else "（未能定位章节标题）"
    return [_issue(
        "REPORT_CHAPTER_MISSING", "error",
        f"报告有 {count} 章还是占位符{named}；导出后这些章在 Word/PDF 里就只有这一句话",
        [14], expected="每章都有正文", actual=f"{count} 章缺失",
    )]


def low_score_indicator_names(state: Dict[str, Any],
                              ratio: float = LOW_SCORE_RATIO) -> List[str]:
    """得分率低于阈值的三级指标名（只取名字够长的，子串匹配才有意义）。

    公开版的唯一理由是**口径只有一个**：批次 26 的覆盖检查、批次 29 的步骤 9 补全
    都用它。两处各写一份，迟早会出现「门禁说漏了 3 个、补全只补了 2 个」。
    """
    system = state.get("indicator_system") or {}
    scores = state.get("indicator_score_map") or {}
    names: List[str] = []
    for item in indicator_leaves(system.get("level1_indicators") or []):
        name = (item.get("name") or "").strip()
        if len(name) < MIN_INDICATOR_NAME_CHARS:
            continue
        full = score_number(item.get("score", item.get("max_score")))
        actual = actual_indicator_score(item, scores)
        if not full or actual is None:
            continue
        if actual / full < ratio:
            names.append(name)
    return names


def _low_score_leaves(state: Dict[str, Any]) -> List[str]:
    """向后兼容的别名（覆盖检查在用）。"""
    return low_score_indicator_names(state)


def _check_problem_suggestion_coverage(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """低分指标有没有被「问题分析」和「改进建议」点名。

    只报**聚合结论**：36 份真实备份里 167 个低分指标有 131 个（78%）没被问题分析点名，
    逐条报会让一份报告多出十几条提示，用户立刻学会忽略它。聚合之后一份报告最多两条，
    指向「评分与叙述脱节」这个能被处理的事实。

    字段不存在（步骤没跑）时不报 —— 那是「还没写」，不是「写漏了」。
    """
    problems = state.get("problems_analysis")
    suggestions = state.get("suggestions")

    issues: List[Dict[str, Any]] = []
    names = _low_score_leaves(state)
    threshold = int(LOW_SCORE_RATIO * 100)
    for text, label, code, steps in (
        (problems, "问题分析", "PROBLEM_COVERAGE_GAP", [7, 9]),
        (suggestions, "改进建议", "SUGGESTION_COVERAGE_GAP", [7, 10]),
    ):
        if not isinstance(text, str) or not text.strip() or not names:
            continue
        missing = [name for name in names if name not in text]
        if not missing:
            continue
        more = "等" if len(missing) > 3 else ""
        issues.append(_issue(
            code, "warning",
            f"{len(missing)}/{len(names)} 个低分指标（得分率 <{threshold}%）"
            f"在「{label}」里没有被点名：{'、'.join(missing[:3])}{more}",
            steps, expected=f"逐个点名 {len(names)} 个低分指标", actual=f"未点名 {len(missing)} 个",
        ))

    # 「无主建议」：写了一大段建议，却没有任何问题分析。字段存在但为空才算 ——
    # 字段缺失说明步骤 9 没跑，由 REPORT_WITH_INCOMPLETE_STEPS 负责
    if isinstance(problems, str) and not problems.strip() \
            and isinstance(suggestions, str) and suggestions.strip():
        issues.append(_issue(
            "SUGGESTION_WITHOUT_PROBLEM", "warning",
            "写了改进建议，但问题分析章节是空的；读者看不出这些建议针对什么问题",
            [9, 10], expected="先列问题，再给建议", actual="问题分析为空",
        ))
    return issues


def check_consistency(state: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {issues, error_count, warning_count, blocking_count, exportable, score, checked}。"""
    state = state or {}
    issues: List[Dict[str, Any]] = []
    issues += _check_indicator_allocation(state)
    issues += _check_score_summary(state)
    issues += _check_report_text(state)
    issues += _check_chapters(state)
    issues += _check_problem_suggestion_coverage(state)
    issues += _check_steps_and_coverage(state)

    counters: Dict[str, int] = {}
    for item in issues:
        item["blocking"] = item["code"] in BLOCKING_CODES
        counters[item["code"]] = counters.get(item["code"], 0) + 1
        item["id"] = f"{item['code']}#{counters[item['code']]}"

    blocking = [item for item in issues if item["blocking"]]
    summary = state.get("score_summary") or {}
    return {
        "issues": issues,
        "error_count": sum(1 for item in issues if item["severity"] == "error"),
        "warning_count": sum(1 for item in issues if item["severity"] == "warning"),
        "blocking_count": len(blocking),
        # 导出闸门只看这一位；出现结构性缺陷时不允许静默导出
        "exportable": not blocking,
        "score": {
            "total_score": summary.get("total_score"),
            "complete": summary.get("complete"),
            "grade": summary.get("grade"),
            "scored_count": summary.get("scored_count"),
            "total_count": summary.get("total_count"),
            "leaf_full_total": _leaf_full_total(state),
            "leaf_actual_total": _leaf_actual_total(state),
            "level1_total": _level1_total(state),
        },
        "checked": list(CHECKS),
    }


def summary_line(report: Dict[str, Any]) -> str:
    """一句话结论，供 warnings / 前端提示使用。"""
    errors, warnings = report.get("error_count", 0), report.get("warning_count", 0)
    if not errors and not warnings:
        return ""
    codes = "、".join(f"{item['code']}" for item in report.get("issues", [])[:4])
    more = " 等" if len(report.get("issues", [])) > 4 else ""
    return f"步骤间一致性检查：{errors} 项错误、{warnings} 项提示（{codes}{more}）"
