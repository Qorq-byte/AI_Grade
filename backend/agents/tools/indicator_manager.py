"""
指标体系管理工具
支持指标增删改查、模板导入、顺序调整、导入自有指标体系
"""
from typing import List, Dict, Any, Optional, Tuple
from copy import deepcopy
import json
import uuid
from loguru import logger

from backend.agents.states.performance_state import (
    IndicatorInfo,
    IndicatorSystem,
    IndicatorTypeEnum,
)


# ==================== 内置指标体系模板 ====================

# 项目支出绩效评价指标体系模板（一级+二级）
TEMPLATE_PROJECT_EXPENDITURE = {
    "indicator_type": "项目支出指标体系",
    "template_id": 1,
    "template_name": "项目支出绩效评价标准模板",
    "level1_indicators": [
        {
            "id": "t1_l1_001", "level": 1, "name": "决策", "order": 1,
            "score": 20, "source": "template", "children": [
                {"id": "t1_l2_001", "level": 2, "name": "项目立项", "order": 1, "score": 10, "parent_id": "t1_l1_001", "source": "template", "children": []},
                {"id": "t1_l2_002", "level": 2, "name": "绩效目标", "order": 2, "score": 5, "parent_id": "t1_l1_001", "source": "template", "children": []},
                {"id": "t1_l2_003", "level": 2, "name": "资金投入", "order": 3, "score": 5, "parent_id": "t1_l1_001", "source": "template", "children": []},
            ]
        },
        {
            "id": "t1_l1_002", "level": 1, "name": "过程", "order": 2,
            "score": 25, "source": "template", "children": [
                {"id": "t1_l2_004", "level": 2, "name": "资金管理", "order": 1, "score": 15, "parent_id": "t1_l1_002", "source": "template", "children": []},
                {"id": "t1_l2_005", "level": 2, "name": "组织实施", "order": 2, "score": 10, "parent_id": "t1_l1_002", "source": "template", "children": []},
            ]
        },
        {
            "id": "t1_l1_003", "level": 1, "name": "产出", "order": 3,
            "score": 30, "source": "template", "children": [
                {"id": "t1_l2_006", "level": 2, "name": "产出数量", "order": 1, "score": 10, "parent_id": "t1_l1_003", "source": "template", "children": []},
                {"id": "t1_l2_007", "level": 2, "name": "产出质量", "order": 2, "score": 10, "parent_id": "t1_l1_003", "source": "template", "children": []},
                {"id": "t1_l2_008", "level": 2, "name": "产出时效", "order": 3, "score": 5, "parent_id": "t1_l1_003", "source": "template", "children": []},
                {"id": "t1_l2_009", "level": 2, "name": "产出成本", "order": 4, "score": 5, "parent_id": "t1_l1_003", "source": "template", "children": []},
            ]
        },
        {
            "id": "t1_l1_004", "level": 1, "name": "效益", "order": 4,
            "score": 25, "source": "template", "children": [
                {"id": "t1_l2_010", "level": 2, "name": "项目效益", "order": 1, "score": 25, "parent_id": "t1_l1_004", "source": "template", "children": []},
            ]
        },
    ]
}

# 部门整体指标体系模板
TEMPLATE_DEPARTMENT_OVERALL = {
    "indicator_type": "部门整体指标体系",
    "template_id": 2,
    "template_name": "部门整体支出绩效评价标准模板",
    "level1_indicators": [
        {
            "id": "t2_l1_001", "level": 1, "name": "投入", "order": 1,
            "score": 15, "source": "template", "children": [
                {"id": "t2_l2_001", "level": 2, "name": "目标设定", "order": 1, "score": 7, "parent_id": "t2_l1_001", "source": "template", "children": []},
                {"id": "t2_l2_002", "level": 2, "name": "预算配置", "order": 2, "score": 8, "parent_id": "t2_l1_001", "source": "template", "children": []},
            ]
        },
        {
            "id": "t2_l1_002", "level": 1, "name": "过程", "order": 2,
            "score": 25, "source": "template", "children": [
                {"id": "t2_l2_003", "level": 2, "name": "预算执行", "order": 1, "score": 10, "parent_id": "t2_l1_002", "source": "template", "children": []},
                {"id": "t2_l2_004", "level": 2, "name": "预算管理", "order": 2, "score": 8, "parent_id": "t2_l1_002", "source": "template", "children": []},
                {"id": "t2_l2_005", "level": 2, "name": "资产管理", "order": 3, "score": 7, "parent_id": "t2_l1_002", "source": "template", "children": []},
            ]
        },
        {
            "id": "t2_l1_003", "level": 1, "name": "产出", "order": 3,
            "score": 30, "source": "template", "children": [
                {"id": "t2_l2_006", "level": 2, "name": "履职效能", "order": 1, "score": 30, "parent_id": "t2_l1_003", "source": "template", "children": []},
            ]
        },
        {
            "id": "t2_l1_004", "level": 1, "name": "效益", "order": 4,
            "score": 30, "source": "template", "children": [
                {"id": "t2_l2_007", "level": 2, "name": "社会效益", "order": 1, "score": 15, "parent_id": "t2_l1_004", "source": "template", "children": []},
                {"id": "t2_l2_008", "level": 2, "name": "满意度", "order": 2, "score": 15, "parent_id": "t2_l1_004", "source": "template", "children": []},
            ]
        },
    ]
}

# 模板注册表
BUILTIN_TEMPLATES: Dict[str, IndicatorSystem] = {
    IndicatorTypeEnum.PROJECT_EXPENDITURE.value: TEMPLATE_PROJECT_EXPENDITURE,
    IndicatorTypeEnum.DEPARTMENT_OVERALL.value: TEMPLATE_DEPARTMENT_OVERALL,
}


class IndicatorManager:
    """指标体系管理器"""

    def __init__(self, indicator_system: Optional[IndicatorSystem] = None):
        """
        初始化管理器

        Args:
            indicator_system: 现有的指标体系（可选）
        """
        self.system: IndicatorSystem = indicator_system or {
            "indicator_type": "项目支出指标体系",
            "template_id": None,
            "level1_indicators": [],
            "metadata": {}
        }

