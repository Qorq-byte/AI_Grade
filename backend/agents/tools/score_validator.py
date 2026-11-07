"""
四维度分值校验工具
严格按客户端需求实现分值分配的自动化校验
"""
import math
from typing import Dict, List, Optional, Any
from loguru import logger
from backend.agents.states.performance_state import (
    IndicatorInfo,
    IndicatorSystem,
    ScoreValidationResult,
    ScoreValidationDetail,
)


class ScoreValidator:
    """
    四维度分值校验器

    一维度：所有一级/二级/三级指标分值总和=100分
    二维度：一级=其二级之和=其三级之和
    三维度：二级=其三级之和
    四维度：产出+效益指标分值总和<60分时预警
    """

    # 产出和效益类指标的关键词匹配
    OUTPUT_KEYWORDS = ["产出", "output", "结果"]
    BENEFIT_KEYWORDS = ["效益", "benefit", "效果", "影响", "满意度"]

    def __init__(self):
        self._reset()

    def _reset(self):
        """重置校验状态"""
        self.validations: List[ScoreValidationDetail] = []
        self.warnings: List[str] = []

    def validate(self, indicator_system: IndicatorSystem) -> ScoreValidationResult:
        """
        对完整指标体系执行四维度校验

        Args:
            indicator_system: 完整的指标体系

        Returns:
            ScoreValidationResult: 校验结果
        """
        self._reset()
        level1 = indicator_system.get("level1_indicators", [])

        if not level1:
            return {
                "overall_passed": False,
                "validations": [{
                    "dimension": 0,
                    "passed": False,
                    "message": "指标体系为空，无法校验",
                    "details": None
                }],
                "warnings": ["指标体系为空"]
            }

        # 一维度：总分=100
        self._validate_dimension_1(level1)

        # 二维度：一级分=二级和=三级和
        self._validate_dimension_2(level1)

        # 三维度：二级分=三级和
        self._validate_dimension_3(level1)

        # 四维度：产出+效益<60预警
        self._validate_dimension_4(level1)

        overall_passed = all(v["passed"] for v in self.validations)

        result: ScoreValidationResult = {
            "overall_passed": overall_passed,
            "validations": self.validations,
            "warnings": self.warnings
        }

        logger.info(
            f"分值校验完成: overall_passed={overall_passed}, "
            f"validations={len(self.validations)}, warnings={len(self.warnings)}"
        )
        return result

    # ─── 一维度 ──────────────────────────────────────
    def _validate_dimension_1(self, level1: List[IndicatorInfo]):
        """一维度：各层级总分=100"""
        total_l1 = self._sum_scores(level1)
        total_l2 = self._sum_scores(self._collect_all_level(level1, 2))
        total_l3 = self._sum_scores(self._collect_all_level(level1, 3))

        passed = True
        details_parts = []

        if not self._approx_equal(total_l1, 100.0):
            passed = False
            details_parts.append(f"一级指标总分={total_l1}，应为100")
        else:
            details_parts.append(f"一级指标总分={total_l1} ✓")

        if not self._approx_equal(total_l2, 100.0):
            passed = False
            details_parts.append(f"二级指标总分={total_l2}，应为100")
        else:
            details_parts.append(f"二级指标总分={total_l2} ✓")

        if not self._approx_equal(total_l3, 100.0):
            passed = False
            details_parts.append(f"三级指标总分={total_l3}，应为100")
        else:
            details_parts.append(f"三级指标总分={total_l3} ✓")

        self._add_validation(1, passed, "所有指标层级总分=100分校验", "; ".join(details_parts))

    # ─── 二维度 ──────────────────────────────────────
    def _validate_dimension_2(self, level1: List[IndicatorInfo]):
        """二维度：每个一级=其二级之和=其三级之和"""
        passed = True
        details_parts = []

        for l1 in level1:
            name = l1.get("name", "未命名")
            l1_score = score_number(l1.get("score")) or 0
            children = l1.get("children", []) or []
            l2_sum = self._sum_scores(children)
            l3_sum = self._sum_scores(self._collect_all_level([l1], 3))

            if not self._approx_equal(l1_score, l2_sum):
                passed = False
                details_parts.append(
                    f"「{name}」一级={l1_score} ≠ 二级和={l2_sum}"
                )
            if l3_sum > 0 and not self._approx_equal(l1_score, l3_sum):
                passed = False
                details_parts.append(
                    f"「{name}」一级={l1_score} ≠ 三级和={l3_sum}"
                )

        if passed:
            details_parts.append("所有一级指标等于其二级/三级指标分值之和 ✓")

        self._add_validation(
            2, passed,
            "每个一级指标=其二级之和=其三级之和",
            "; ".join(details_parts) if details_parts else "全部通过"
        )

    # ─── 三维度 ──────────────────────────────────────
    def _validate_dimension_3(self, level1: List[IndicatorInfo]):
        """三维度：每个二级=其三级之和"""
        passed = True
        details_parts = []

        all_l2 = self._collect_all_level(level1, 2)
        for l2 in all_l2:
            name = l2.get("name", "未命名")
            l2_score = score_number(l2.get("score")) or 0
            children = l2.get("children", []) or []
            l3_sum = self._sum_scores(children)

            if l3_sum > 0 and not self._approx_equal(l2_score, l3_sum):
                passed = False
                details_parts.append(
                    f"「{name}」二级={l2_score} ≠ 三级和={l3_sum}"
                )

        if passed:
            details_parts.append("所有二级指标等于其三级指标分值之和 ✓")

        self._add_validation(
            3, passed,
            "每个二级指标=其三级之和",
            "; ".join(details_parts) if details_parts else "全部通过"
        )

    # ─── 四维度 ──────────────────────────────────────
    def _validate_dimension_4(self, level1: List[IndicatorInfo]):
        """四维度：产出+效益指标分值<60时预警"""
        all_l1 = level1
        all_l2 = self._collect_all_level(level1, 2)
        all_l3 = self._collect_all_level(level1, 3)

        output_benefit_l1 = sum(
            s for ind in all_l1
            if self._is_output_or_benefit(ind.get("name", ""))
            for s in [ind.get("score", 0) or 0]
        )
        output_benefit_l2 = sum(
            s for ind in all_l2
            if self._is_output_or_benefit(ind.get("name", ""))
            for s in [ind.get("score", 0) or 0]
        )
        output_benefit_l3 = sum(
            s for ind in all_l3
            if self._is_output_or_benefit(ind.get("name", ""))
            for s in [ind.get("score", 0) or 0]
        )

        warnings_generated = []
        if output_benefit_l1 >= 60:
            warnings_generated.append(
                f"一级指标中产出+效益分值={output_benefit_l1}≥60，建议<60分"
            )
        if output_benefit_l2 >= 60:
            warnings_generated.append(
                f"二级指标中产出+效益分值={output_benefit_l2}≥60，建议<60分"
            )
        if output_benefit_l3 >= 60:
            warnings_generated.append(
                f"三级指标中产出+效益分值={output_benefit_l3}≥60，建议<60分"
            )

        self.warnings.extend(warnings_generated)

        details = (
            f"一级产出+效益={output_benefit_l1}, "
            f"二级产出+效益={output_benefit_l2}, "
            f"三级产出+效益={output_benefit_l3}"
        )
        passed = len(warnings_generated) == 0

        self._add_validation(
            4, passed,
            "产出+效益指标分值<60分预警",
            details
        )

    # ─── 工具方法 ────────────────────────────────────
    def _sum_scores(self, indicators: List[IndicatorInfo]) -> float:
        """计算指标列表的分值总和"""
        return round(
            sum(score_number(ind.get("score")) or 0 for ind in indicators), 2
        )

    def _collect_all_level(
        self,
        indicators: List[IndicatorInfo],
        target_level: int
    ) -> List[IndicatorInfo]:
        """收集指定层级的所有指标（递归）"""
        result = []
        for ind in indicators:
            if ind.get("level") == target_level:
                result.append(ind)
            children = ind.get("children", []) or []
            result.extend(self._collect_all_level(children, target_level))
        return result

    def _is_output_or_benefit(self, name: str) -> bool:
        """判断指标名称是否属于产出或效益类"""
        name_lower = name.lower()
        return any(
            kw in name_lower for kw in self.OUTPUT_KEYWORDS + self.BENEFIT_KEYWORDS
        )

    def _approx_equal(self, a: float, b: float, tolerance: float = 0.01) -> bool:
        """浮点数近似相等判断"""
        return abs(a - b) <= tolerance

    def _add_validation(
        self,
        dimension: int,
        passed: bool,
        message: str,
        details: Optional[str] = None
    ):
        self.validations.append(
            ScoreValidationDetail(
                dimension=dimension,
                passed=passed,
                message=message,
                details=details
            )
        )


# 便捷函数
def validate_indicator_scores(indicator_system: IndicatorSystem) -> ScoreValidationResult:
    """便捷校验函数"""
    validator = ScoreValidator()
    return validator.validate(indicator_system)

def score_number(value):
    """Use None for missing, malformed or non-finite scores. Zero is a score."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def actual_indicator_score(indicator, scores, indicator_id=None):
    full = score_number(indicator.get("score", indicator.get("max_score")))
    entry = scores.get(indicator_id or indicator.get("id"), {})
    actual = score_number(entry.get("actual_score", indicator.get("actual_score")))
    return actual if full is not None and actual is not None and actual <= full else None


def indicator_tree_error(system):
    """Validate required fields without inventing names, IDs or scores."""
    ids = set()

    def check(nodes):
        if not isinstance(nodes, list) or not nodes:
            return "指标列表为空或格式错误"
        for node in nodes:
            if not isinstance(node, dict):
                return "指标必须是对象"
            identifier, name = node.get("id"), node.get("name")
            if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
                return "指标 ID 缺失或重复"
            if not isinstance(name, str) or not name.strip():
                return "指标名称缺失"
            ids.add(identifier)
            if node.get("score") is not None and score_number(node["score"]) is None:
                return "指标分值超出 0–100 或格式错误"
            if node.get("children"):
                error = check(node["children"])
                if error:
                    return error
        return ""

    return check(system.get("level1_indicators")) if isinstance(system, dict) else "指标体系必须是对象"


def allocation_valid(validation):
    """Treat total and hierarchy as hard checks, with allocation policy warnings separate."""
    return all(result["passed"] for result in validation["validations"] if result["dimension"] != 4)
