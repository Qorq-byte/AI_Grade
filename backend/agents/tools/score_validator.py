"""
四维度分值校验工具
严格按客户端需求实现分值分配的自动化校验
"""
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
            l1_score = l1.get("score", 0) or 0
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

