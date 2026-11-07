"""
调查问卷生成器
基于里克特量表，根据指标体系自动生成调查问卷
"""
import re
import unicodedata
import uuid
from typing import List, Dict, Any, Optional


DEFAULT_LIKERT_LEVELS = ["非常不满意", "不满意", "一般", "满意", "非常满意"]
QUESTION_TEMPLATES = {"default": "关于「{indicator_name}」，您的满意程度如何？"}
SURVEY_TERMS = ("效益", "满意", "影响", "效果", "质量", "时效", "获得感", "认可", "体验", "便利")


def indicator_leaves(indicators):
    for indicator in indicators:
        children = (indicator.get("children") or indicator.get("level2_indicators")
                    or indicator.get("level3_indicators") or [])
        if children:
            yield from indicator_leaves(children)
        else:
            yield indicator


def _question_key(name):
    normalized = unicodedata.normalize("NFKC", name).casefold()
    normalized = re.sub(r"[^\w]", "", normalized)
    return re.sub(r"(满意程度|满意度)$", "满意", normalized)


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
        """Generate one consistently scaled question per eligible, distinct indicator."""
        selected = set(custom_indicators) if custom_indicators is not None else None
        questions = []
        ids, names = set(), set()
        for indicator in indicator_leaves(indicator_system.get("level1_indicators", [])):
            indicator_id = indicator.get("id")
            name = str(indicator.get("name") or "").strip()
            key = _question_key(name)
            if not indicator_id or not key or indicator_id in ids or key in names:
                continue
            if re.search(r"、|/|，|以及|质量[和与及]时效", name):
                continue
            if selected is not None:
                if indicator_id not in selected:
                    continue
            elif not any(term in name for term in SURVEY_TERMS):
                continue
            if len(questions) >= max_questions:
                break
            ids.add(indicator_id)
            names.add(key)
            questions.append({
                "question_id": str(uuid.uuid4()),
                "index": len(questions) + 1,
                "question_text": self.question_templates["default"].format(indicator_name=name),
                "related_indicator_id": indicator_id,
                "related_indicator_name": name,
                "scale_levels": list(self.scale_levels),
                "question_type": "likert",
                "created_by": "system",
            })
        return questions

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
