"""
调查问卷生成器
基于里克特量表，根据指标体系自动生成调查问卷
"""
from typing import List, Dict, Any, Optional
from loguru import logger
import uuid
import json

from backend.utils.llm_helper import get_model_provider


# 里克特量表默认等级
DEFAULT_LIKERT_LEVELS = ["非常不满意", "不满意", "一般", "满意", "非常满意"]

# 问卷基本句式模板
QUESTION_TEMPLATES = {
    "benefit": "您认为本项目对{indicator_name}的影响是否显著？",
    "satisfaction": "您对{indicator_name}的满意程度如何？",
    "effectiveness": "您认为{indicator_name}的实施效果如何？",
    "quality": "您认为{indicator_name}的质量如何？",
    "efficiency": "您认为{indicator_name}的效率如何？",
    "default": "您认为本项目的{indicator_name}表现如何？",
}

# 效益类指标关键词映射到问题模板
INDICATOR_TEMPLATE_MAP = {
    "社会效益": "benefit",
    "经济效益": "benefit",
    "生态效益": "benefit",
    "环境效益": "benefit",
    "满意度": "satisfaction",
    "可持续影响": "effectiveness",
    "产出质量": "quality",
    "产出时效": "efficiency",
    "履职效能": "effectiveness",
    "项目效益": "benefit",
}


class QuestionnaireGenerator:
    """调查问卷生成器"""

    def __init__(
        self,
        scale_levels: Optional[List[str]] = None,
        question_templates: Optional[Dict[str, str]] = None
    ):
        """
        初始化问卷生成器

        Args:
            scale_levels: 自定义里克特量表等级
            question_templates: 自定义问句模板
        """
        self.scale_levels = scale_levels or DEFAULT_LIKERT_LEVELS
        self.question_templates = question_templates or QUESTION_TEMPLATES

    def generate_questions(
        self,
        indicator_system: Dict[str, Any],
        project_core_content: str = "",
        custom_indicators: Optional[List[str]] = None,
        max_questions: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        根据指标体系生成问卷题目
        优先使用LLM生成智能问题，失败时回退到结构化模式
        """
        # 首先尝试用LLM生成
        questions = []
        if hasattr(self, 'base_agent') and self.base_agent:
            try:
                questions = self._generate_with_llm(
                    indicator_system, project_core_content, custom_indicators, max_questions
                )
                if questions:
                    logger.info(f"LLM生成问卷成功: {len(questions)}题")
                    return questions
            except Exception as e:
                logger.warning(f"LLM问卷生成失败: {e}，回退到基础模式")

        # 回退到基础模式（从指标体系直接生成）
        logger.info("使用基础模式从指标体系生成问卷")
        return self._generate_structured_questions(
            indicator_system, project_core_content, custom_indicators, max_questions
        )

    def _generate_with_llm(
        self,
        indicator_system: Dict[str, Any],
        project_core_content: str = "",
        custom_indicators: Optional[List[str]] = None,
        max_questions: int = 30,
    ) -> List[Dict[str, Any]]:
        """使用LLM智能生成问卷题目"""
        from langchain_core.messages import SystemMessage, HumanMessage

        indicators_text = json.dumps(indicator_system, ensure_ascii=False, indent=2)
        system_prompt = (
            "你是绩效评价问卷设计专家。请根据提供的指标体系，为每个三级指标生成一道里克特五级量表题目。\n"
            "要求：\n"
            "1. 每个问题对应一个三级指标，题目应简洁明确\n"
            "2. 选项为：非常不满意/不满意/一般/满意/非常满意\n"
            "3. 返回JSON数组：[{'id':'指标ID','question':'题目','levels':['非常不满意','不满意','一般','满意','非常满意']},...]\n"
            "4. 只输出JSON，不要其他文字"
        )
        human_prompt = (
            f"## 项目背景\n{project_core_content[:2000]}\n\n"
            f"## 指标体系\n{indicators_text[:4000]}\n\n"
            f"请为符合问卷条件的三级指标生成题目，最多{max_questions}题。只输出JSON数组。"
        )

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        response = self.base_agent.llm.invoke(messages, config={"timeout": 120})

        text = response.content if hasattr(response, 'content') else str(response)
        text = (text or "").strip()
        # 提取JSON
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end + 1]

        raw_questions = json.loads(text)
        questions = []
        for i, rq in enumerate(raw_questions):
            q = {
                "question_id": rq.get("id", f"q_{i}"),
                "question_text": rq.get("question", rq.get("question_text", f"题目{i+1}")),
                "question_type": "likert",
                "scale_levels": rq.get("levels", DEFAULT_LIKERT_LEVELS),
                "related_indicator_id": rq.get("id", ""),
                "related_indicator_name": rq.get("question", "")[:50],
                "template_type": "benefit",
            }
            questions.append(q)
        return questions

    def _generate_structured_questions(
        self,
        indicator_system: Dict[str, Any],
        project_core_content: str = "",
        custom_indicators: Optional[List[str]] = None,
        max_questions: int = 30,
    ) -> List[Dict[str, Any]]:
        """从指标体系结构化生成问题（不需要LLM）"""
        questions = []

        # 收集需要生成问卷的指标
        target_indicators = self._identify_survey_indicators(
            indicator_system, custom_indicators
        )

        logger.info(f"识别到 {len(target_indicators)} 个可生成问卷的指标")

        # 为每个指标生成题目
        for idx, indicator in enumerate(target_indicators[:max_questions]):
            question = self._generate_single_question(indicator, idx + 1)
            questions.append(question)

        logger.info(f"已生成 {len(questions)} 个问卷题目")
        return questions

    def _identify_survey_indicators(
        self,
        indicator_system: Dict[str, Any],
        custom_indicators: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        识别需要生成问卷的指标

        规则：
        1. 如果指定了custom_indicators，则使用指定的指标
        2. 否则自动识别效益类、满意度类等需要调查问卷的指标
        """
        if custom_indicators:
            # 根据ID查找指标
            result = []
            for ind_id in custom_indicators:
                ind = self._find_indicator_by_id(
                    indicator_system.get("level1_indicators", []), ind_id
                )
                if ind:
                    result.append(ind)
            return result

        # 自动识别：三级指标中名称包含效益/满意度/效果等关键词的
        all_l3 = self._collect_level3_indicators(
            indicator_system.get("level1_indicators", [])
        )

        benefit_l3 = [
            ind for ind in all_l3
            if self._is_survey_indicator(ind.get("name", ""))
        ]

        # 如果没有三级指标，检查二级指标
        if not all_l3:
            all_l2 = self._collect_level2_indicators(
                indicator_system.get("level1_indicators", [])
            )
            benefit_l3 = [
                ind for ind in all_l2
                if self._is_survey_indicator(ind.get("name", ""))
            ]
            # 如果二级也没有匹配的，回退到所有二级指标
            if not benefit_l3 and all_l2:
                benefit_l3 = all_l2
                logger.info("未找到问卷类指标，回退到全部二级指标")

        # 如果过滤后为空但有三/二级指标，回退到全部指标
        if not benefit_l3 and all_l3:
            benefit_l3 = all_l3
            logger.info("未找到问卷类指标，回退到全部三级指标")

        # 按顺序返回
        return sorted(benefit_l3, key=lambda x: (
            x.get("parent_id", ""),
            x.get("order", 0)
        ))

    def _generate_single_question(
        self,
        indicator: Dict[str, Any],
        index: int
    ) -> Dict[str, Any]:
        """为单个指标生成问卷题目"""
        indicator_name = indicator.get("name", "")
        indicator_id = indicator.get("id", str(uuid.uuid4()))

        # 选择问句模板
        template_key = self._select_template(indicator_name)
        template = self.question_templates.get(
            template_key, self.question_templates["default"]
        )

        question_text = template.format(indicator_name=indicator_name)

        return {
            "question_id": str(uuid.uuid4()),
            "index": index,
            "question_text": question_text,
            "related_indicator_id": indicator_id,
            "related_indicator_name": indicator_name,
            "scale_levels": list(self.scale_levels),
            "question_type": "likert",
            "created_by": "system",
        }

    def _select_template(self, indicator_name: str) -> str:
        """根据指标名称选择合适的问句模板"""
        for keyword, template_key in INDICATOR_TEMPLATE_MAP.items():
            if keyword in indicator_name:
                return template_key
        return "default"

    def _is_survey_indicator(self, name: str) -> bool:
        """判断指标是否需要问卷"""
        keywords = [
            "效益", "满意度", "影响", "效果", "质量", "时效",
            "利益", "获得感", "认可", "知晓率", "覆盖率"
        ]
        return any(kw in name for kw in keywords)

    def _collect_level3_indicators(
        self, indicators: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """收集所有三级指标 — 支持嵌套结构 level2_indicators/level3_indicators"""
        result = []
        for ind in indicators:
            # 三级指标：在 level3_indicators 列表中
            l3_list = ind.get("level3_indicators", []) or ind.get("children", []) or []
            for l3 in l3_list:
                result.append(l3)
            # 二级指标：继续递归其下级
            l2_list = ind.get("level2_indicators", []) or ind.get("children", []) or []
            result.extend(self._collect_level3_indicators(l2_list))
        return result

    def _collect_level2_indicators(
        self, indicators: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """收集所有二级指标 — 支持嵌套结构"""
        result = []
        for ind in indicators:
            # 二级指标：在 level2_indicators 列表中
            l2_list = ind.get("level2_indicators", []) or ind.get("children", []) or []
            for l2 in l2_list:
                result.append(l2)
            # 一级指标：继续递归其下级
            result.extend(self._collect_level2_indicators(l2_list))
        return result

    def _find_indicator_by_id(
        self, indicators: List[Dict[str, Any]], target_id: str
    ) -> Optional[Dict[str, Any]]:
        """根据ID递归查找指标"""
        for ind in indicators:
            if ind.get("id") == target_id:
                return ind
            children = ind.get("children", []) or []
            result = self._find_indicator_by_id(children, target_id)
            if result:
                return result
        return None

    def add_open_ended_question(
        self,
        questions: List[Dict[str, Any]],
        question_text: str,
    ) -> Dict[str, Any]:
        """
        添加开放性填空问题

        Args:
            questions: 现有问卷列表
            question_text: 问题文本

        Returns:
            新添加的问题
        """
        new_question = {
            "question_id": str(uuid.uuid4()),
            "index": len(questions) + 1,
            "question_text": question_text,
            "related_indicator_id": "",
            "related_indicator_name": "",
            "scale_levels": [],
            "question_type": "open_ended",
            "created_by": "user",
        }
        questions.append(new_question)
        return new_question

    def format_questionnaire_for_display(
        self,
        questions: List[Dict[str, Any]],
        title: str = "调查问卷"
    ) -> str:
        """格式化问卷为Markdown显示文本"""
        lines = [f"# {title}", ""]

        for q in questions:
            lines.append(f"**{q.get('index', '')}. {q.get('question_text', q.get('question', ''))}**")
            if q["question_type"] == "likert":
                levels = q.get("scale_levels", [])
                lines.append(f"  {' | '.join(levels)} |")
            else:
                lines.append(f"  [请填写：______________________]")
            lines.append("")

        lines.append(f"\n*共 {len(questions)} 题*")
        return "\n".join(lines)

    def to_dict(self, questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """导出为题列表"""
        return questions


# 便捷函数
def generate_default_questionnaire(
    indicator_system: Dict[str, Any],
    project_core_content: str = "",
    max_questions: int = 30,
) -> List[Dict[str, Any]]:
    """便捷生成默认问卷"""
    generator = QuestionnaireGenerator()
    return generator.generate_questions(
        indicator_system,
        project_core_content,
        max_questions=max_questions
    )
