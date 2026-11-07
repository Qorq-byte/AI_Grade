"""
多模型对比工具
支持同时调用多个大模型、对比分析、对比后单独对话
"""
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
from loguru import logger
import asyncio
import time

from backend.utils.llm_helper import get_supported_models, get_model_provider


class MultiModelCompareService:
    """多模型对比服务"""

    def __init__(
        self,
        model_names: List[str],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 4000,
    ):
        """
        初始化多模型对比服务

        Args:
            model_names: 要对比的模型名称列表
            temperature: 温度参数
            top_p: Top-P参数
            max_tokens: 最大token数
        """
        self.model_names = model_names
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.results: Dict[str, Dict[str, Any]] = {}

    def compare(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        chat_function: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        使用多个模型同时分析同一个prompt（同步版本）

        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词
            chat_function: 对话函数，接受(model_name, messages)返回response_text
                           如果不提供，则使用内置的BaseAgent

        Returns:
            多模型对比结果
        """
        logger.info(f"开始多模型对比: models={self.model_names}")
        start_time = time.time()

        individual_outputs: Dict[str, str] = {}
        errors: Dict[str, str] = {}

        if chat_function is None:
            # 使用内置的BaseAgent逐模型调用
            from backend.agents.base_agent import BaseAgent
            for model_name in self.model_names:
                try:
                    agent = BaseAgent(
                        session_id=None,
                        model_name=model_name,
                        temperature=self.temperature,
                        top_p=self.top_p,
                    )
                    result = agent.chat(prompt, use_memory=False)
                    if result.get("success"):
                        individual_outputs[model_name] = result["message"]
                    else:
                        errors[model_name] = result.get("error", "未知错误")
                    agent.close()
                except Exception as e:
                    errors[model_name] = str(e)
                    logger.error(f"模型 {model_name} 调用失败: {e}")
        else:
            # 使用提供的回调函数
            for model_name in self.model_names:
                try:
                    response = chat_function(model_name, prompt, system_prompt)
                    individual_outputs[model_name] = response
                except Exception as e:
                    errors[model_name] = str(e)

        elapsed = time.time() - start_time

        result = {
            "models_used": self.model_names,
            "individual_outputs": individual_outputs,
            "errors": errors,
            "successful_models": list(individual_outputs.keys()),
            "failed_models": list(errors.keys()),
            "execution_time": round(elapsed, 2),
            "timestamp": datetime.now().isoformat(),
        }

        self.results = result
        logger.info(
            f"多模型对比完成: 成功{len(individual_outputs)}/{len(self.model_names)}, "
            f"耗时{elapsed:.2f}s"
        )
        return result

    def generate_comparison_summary(
        self,
        comparison_result: Dict[str, Any],
        summary_prompt: str = "",
    ) -> str:
        """
        生成对比分析总结

        Args:
            comparison_result: 多模型对比结果
            summary_prompt: 额外的总结指导

        Returns:
            对比分析总结文本
        """
        outputs = comparison_result.get("individual_outputs", {})
        if not outputs:
            return "无有效输出可对比"

        if len(outputs) == 1:
            model_name = list(outputs.keys())[0]
            return f"仅有一个模型 ({model_name}) 成功输出结果。"

        # 构建总结的分析文本
        parts = []
        parts.append(f"## 多模型对比分析")
        parts.append(f"- 成功参与的模型：{', '.join(outputs.keys())}")
        parts.append(f"- 总共 {len(outputs)} 个模型产生了有效输出")

        if comparison_result.get("errors"):
            parts.append(f"- 失败的模型：{', '.join(comparison_result['errors'].keys())}")

        parts.append("")
        parts.append("### 各模型输出")

        for model_name, output in outputs.items():
            preview = output[:300] + "..." if len(output) > 300 else output
            parts.append(f"**{model_name}**: {preview}")
            parts.append("")

        return "\n".join(parts)


class ModelDialogueManager:
    """
    对比后单独对话管理器
    允许用户在多模型对比完成后，与任意模型单独继续对话
    """

    def __init__(self):
        self.active_dialogues: Dict[str, Any] = {}

    def start_dialogue(
        self,
        model_name: str,
        context: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """
        与指定模型开始单独对话

        Args:
            model_name: 模型名称
            context: 对话上下文（来自多模型对比的输出）
            session_id: 会话ID

        Returns:
            对话信息
        """
        dialogue_info = {
            "model_name": model_name,
            "session_id": session_id,
            "context": context,
            "started_at": datetime.now().isoformat(),
            "message_count": 0,
        }

        key = f"{session_id}_{model_name}"
        self.active_dialogues[key] = dialogue_info

        logger.info(f"开始单独对话: model={model_name}, session={session_id}")
        return dialogue_info

    def send_message(
        self,
        model_name: str,
        session_id: str,
        message: str,
        chat_function: Callable,
    ) -> str:
        """
        在单独对话中发送消息

        Args:
            model_name: 模型名称
            session_id: 会话ID
            message: 用户消息
            chat_function: 对话函数

        Returns:
            模型回复
        """
        key = f"{session_id}_{model_name}"
        if key not in self.active_dialogues:
            raise ValueError(f"未找到对话: model={model_name}, session={session_id}")

        response = chat_function(model_name, message)
        self.active_dialogues[key]["message_count"] += 1

        return response

    def end_dialogue(self, model_name: str, session_id: str):
        """结束单独对话"""
        key = f"{session_id}_{model_name}"
        self.active_dialogues.pop(key, None)
        logger.info(f"结束单独对话: model={model_name}, session={session_id}")


# 便捷函数
def compare_models(
    model_names: List[str],
    prompt: str,
    temperature: float = 0.7,
    top_p: float = 0.9,
    system_prompt: Optional[str] = None,
    chat_function: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    便捷多模型对比函数

    Args:
        model_names: 模型名称列表
        prompt: 提示词
        temperature: 温度
        top_p: Top-P
        system_prompt: 系统提示
        chat_function: 自定义对话函数

    Returns:
        对比结果
    """
    service = MultiModelCompareService(
        model_names=model_names,
        temperature=temperature,
        top_p=top_p,
    )
    return service.compare(prompt, system_prompt, chat_function)
