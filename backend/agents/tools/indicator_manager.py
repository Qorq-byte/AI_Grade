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

    # ─── 模板操作 ────────────────────────────────────

    def load_template(self, indicator_type: str) -> IndicatorSystem:
        """
        加载内置模板

        Args:
            indicator_type: 指标体系类型

        Returns:
            加载后的指标体系
        """
        template = BUILTIN_TEMPLATES.get(indicator_type)
        if template:
            self.system = deepcopy(template)
            logger.info(f"已加载模板: {indicator_type}")
        else:
            logger.warning(f"未找到模板: {indicator_type}，返回空体系")
            self.system = {
                "indicator_type": indicator_type,
                "template_id": None,
                "level1_indicators": [],
                "metadata": {}
            }
        return self.system

    def import_custom_system(self, custom_data: IndicatorSystem) -> IndicatorSystem:
        """
        导入自定义指标体系

        Args:
            custom_data: 用户自定义的指标体系

        Returns:
            导入后的指标体系
        """
        # 标准化导入的数据
        custom_data = self._normalize_system(custom_data)
        self.system = custom_data
        logger.info("已导入自定义指标体系")
        return self.system

    def get_templates(self) -> List[Dict[str, str]]:
        """获取所有可用模板列表"""
        return [
            {"value": key, "label": key}
            for key in BUILTIN_TEMPLATES.keys()
        ]

    # ─── 指标CRUD ────────────────────────────────────

    def add_indicator(
        self,
        parent_id: Optional[str],
        name: str,
        score: Optional[float] = None,
        level: Optional[int] = None
    ) -> IndicatorInfo:
        """
        添加新指标

        Args:
            parent_id: 父级指标ID（None则添加为一级指标）
            name: 指标名称
            score: 分值
            level: 层级（None则根据父级自动推断）

        Returns:
            新创建的指标
        """
        target_list, actual_level = self._find_parent(parent_id, level)

        new_id = str(uuid.uuid4())
        children = [] if actual_level <= 3 else []

        new_indicator: IndicatorInfo = {
            "id": new_id,
            "level": actual_level,
            "name": name,
            "parent_id": parent_id,
            "order": len(target_list) + 1,
            "score": score,
            "explanation": None,
            "scoring_standard": None,
            "source": "user_added",
            "children": children
        }

        target_list.append(new_indicator)
        logger.info(f"添加指标: L{actual_level} '{name}' (id={new_id})")
        return new_indicator

    def remove_indicator(self, indicator_id: str) -> bool:
        """删除指标（含其所有子指标）"""
        parent_list, _ = self._find_parent_by_child_id(indicator_id)
        if parent_list is not None:
            # 从父级的children列表中移除
            for i, ind in enumerate(parent_list):
                if ind.get("id") == indicator_id:
                    parent_list.pop(i)
                    logger.info(f"已删除指标: {indicator_id}")
                    return True
        logger.warning(f"未找到要删除的指标: {indicator_id}")
        return False

    def update_indicator(
        self,
        indicator_id: str,
        updates: Dict[str, Any]
    ) -> Optional[IndicatorInfo]:
        """
        更新指标信息

        Args:
            indicator_id: 指标ID
            updates: 要更新的字段

        Returns:
            更新后的指标，未找到返回None
        """
        indicator = self._find_by_id(indicator_id)
        if indicator:
            allowed_fields = ["name", "score", "explanation", "scoring_standard", "order"]
            for key, value in updates.items():
                if key in allowed_fields:
                    indicator[key] = value
            logger.info(f"已更新指标: {indicator_id}, fields={list(updates.keys())}")
            return indicator
        logger.warning(f"未找到要更新的指标: {indicator_id}")
        return None

    def move_indicator(
        self,
        indicator_id: str,
        new_order: int,
        same_level_only: bool = True
    ) -> bool:
        """
        调整指标在同一父级下的顺序

        Args:
            indicator_id: 指标ID
            new_order: 新序号（1-based）
            same_level_only: 是否只允许同级移动（默认True）

        Returns:
            是否成功
        """
        parent_list, _ = self._find_parent_by_child_id(indicator_id)
        if parent_list is None:
            return False

        # 找到目标指标
        current_idx = None
        for i, ind in enumerate(parent_list):
            if ind.get("id") == indicator_id:
                current_idx = i
                break

        if current_idx is None:
            return False

        target = parent_list.pop(current_idx)
        insert_pos = max(0, min(new_order - 1, len(parent_list)))
        parent_list.insert(insert_pos, target)

        # 重新编号
        for i, ind in enumerate(parent_list):
            ind["order"] = i + 1

        logger.info(f"已移动指标: {indicator_id} -> order={insert_pos + 1}")
        return True

    def move_indicator_with_children(
        self,
        indicator_id: str,
        new_order: int
    ) -> bool:
        """
        移动指标及其所有子指标（保持子指标跟随）
        用于一级指标拖拽排序

        Args:
            indicator_id: 指标ID
            new_order: 新序号

        Returns:
            是否成功
        """
        return self.move_indicator(indicator_id, new_order, same_level_only=True)

    # ─── 批量操作 ────────────────────────────────────

    def batch_set_scores(self, score_map: Dict[str, float]) -> int:
        """批量设置分值"""
        count = 0
        for indicator_id, score in score_map.items():
            if self.update_indicator(indicator_id, {"score": score}):
                count += 1
        logger.info(f"批量设置分值: {count}个指标")
        return count

    def get_indicator_by_id(self, indicator_id: str) -> Optional[IndicatorInfo]:
        """根据ID获取指标"""
        return self._find_by_id(indicator_id)

    def get_all_level2_indicators(self) -> List[IndicatorInfo]:
        """获取所有二级指标（用于逐个生成三级指标）"""
        return self._collect_level(self.system.get("level1_indicators", []), 2)

    def get_all_level3_indicators(self) -> List[IndicatorInfo]:
        """获取所有三级指标"""
        return self._collect_level(self.system.get("level1_indicators", []), 3)

    def get_level1_indicators(self) -> List[IndicatorInfo]:
        """获取所有一级指标"""
        return self.system.get("level1_indicators", [])

    def to_dict(self) -> IndicatorSystem:
        """导出为字典"""
        return self.system

    def to_json(self) -> str:
        """导出为JSON"""
        return json.dumps(self.system, ensure_ascii=False, indent=2)

