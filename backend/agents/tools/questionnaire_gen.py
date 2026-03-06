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

