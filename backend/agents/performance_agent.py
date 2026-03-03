"""
PerformanceAgent — 绩效评价报告生成Agent（企业级LangGraph编排 v2）
========================================================================
特性：
  1. 基于 langgraph.StateGraph + 新版 PerformanceState (总步数修正:15步, 0~14)
  2. MemorySaver checkpoint → 断点续跑 / 步骤回退 / 恢复
  3. interrupt() 人机交互 → Step3/6/11 等待用户确认后才继续
  4. 流式输出 astream / astream_events → 前端实时进度
  5. 工具注入：ContentIndexer / ScoreValidator / IndicatorManager
  6. PromptManager → DB动态Prompt / 默认兜底 / 温度三档
  7. 多模型对比模式切换
"""
from typing import TypedDict, List, Dict, Any, Optional, AsyncIterator
from datetime import datetime
import time, json, uuid, re, os

from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from backend.agents.states.performance_state import (
    PerformanceState,
    StepControl,
    StepResult,
    StepStatusEnum,
    ComparisonModeEnum,
    TemperatureProfileEnum,
    SystemConfig,
    IndicatorSystem,
    ScoreValidationResult,
)
from backend.agents.base_agent import BaseAgent
from backend.agents.tools.content_indexer import ContentIndexer, categorize_content
from backend.agents.tools.score_validator import ScoreValidator, validate_indicator_scores
from backend.agents.tools.indicator_manager import IndicatorManager
from backend.agents.prompts.prompt_manager import get_prompt_manager, TemperatureProfile
from backend.agents.prompts.default_prompts import STEP_META, PromptTemplateFactory
from backend.database import SessionLocal

# 可选依赖
try:
    from backend.services.rag_service import get_rag_service
    _HAS_RAG = True
except ImportError:
    _HAS_RAG = False


# ==================== 节点名称常量 ====================

_NODE_INIT       = "init"
_NODE_STEP_1     = "step1_cost_list"
_NODE_STEP_2     = "step2_effective_data"
_NODE_STEP_3     = "step3_indicator_system"
_NODE_STEP_4     = "step4_indicator_scores"
_NODE_STEP_5     = "step5_scoring_standards"
_NODE_STEP_6     = "step6_evaluation_table"
_NODE_STEP_7     = "step7_indicator_analysis"
_NODE_STEP_8     = "step8_experience"
_NODE_STEP_9     = "step9_problems"
_NODE_STEP_10    = "step10_suggestions"
_NODE_STEP_11    = "step11_comprehensive"
_NODE_STEP_12    = "step12_basic_info"
_NODE_STEP_13    = "step13_work_intro"
_NODE_STEP_14    = "step14_final_report"


# ==================== 状态工具函数 ====================

def _ensure_step_control(state: PerformanceState) -> dict:
    if "step_control" not in state or not state.get("step_control"):
        state["step_control"] = {
            "current_step": 0,
            "total_steps": 14,
            "step_statuses": {},
            "can_proceed": True,
            "requires_approval": False,
            "approval_step": None,
            "comparison_mode": ComparisonModeEnum.SINGLE.value,
            "temperature_profile": TemperatureProfileEnum.NORMAL.value,
            "language_style": "中立",
        }
    return state["step_control"]


def _ensure_step_results(state: PerformanceState):
    if "step_results" not in state or not state.get("step_results"):
        state["step_results"] = {}


def _ensure_system_config(state: PerformanceState):
    if "system_config" not in state or not state.get("system_config"):
        state["system_config"] = {
            "enabled_models": [state.get("model_name", "qwen-plus")],
            "default_model": state.get("model_name", "qwen-plus"),
            "rag_enabled": state.get("use_rag", False),
            "rag_collection_name": f"performance_{state.get('session_id', 'default')}",
        }


# ==================== PerformanceAgent ====================

class PerformanceAgent:
    """企业级绩效评价报告生成Agent v2"""

    def __init__(
        self,
        session_id: Optional[str] = None,
        model_name: str = "qwen-plus",
        temperature: float = 0.7,
        top_p: float = 0.9,
        use_rag: bool = False,
        comparison_mode: ComparisonModeEnum = ComparisonModeEnum.SINGLE,
        temperature_profile: TemperatureProfile = TemperatureProfile.NORMAL,
    ):
        self.session_id = session_id or str(uuid.uuid4())
        self.model_name = model_name
        self.temperature = temperature
        self.top_p = top_p
        self.use_rag = use_rag and _HAS_RAG
        
        # 将字符串参数规范化到对应枚举（兼容前端传字符串的场景）
        if isinstance(comparison_mode, str):
            try:
                comparison_mode = ComparisonModeEnum(comparison_mode)
            except ValueError:
                logger.warning(f"无效的comparison_mode: {comparison_mode}，回退为SINGLE")
                comparison_mode = ComparisonModeEnum.SINGLE
        if isinstance(temperature_profile, str):
            tp_lower = temperature_profile.lower()
            if tp_lower in ("creative", "发散"):
                temperature_profile = TemperatureProfile.CREATIVE
            elif tp_lower in ("precise", "严谨"):
                temperature_profile = TemperatureProfile.PRECISE
            else:
                temperature_profile = TemperatureProfile.NORMAL
        
        self.comparison_mode = comparison_mode
        self.temperature_profile = temperature_profile

        # BaseAgent（单模型模式）
        try:
            self.base_agent = BaseAgent(
                session_id=self.session_id,
                model_name=model_name,
                temperature=temperature,
                top_p=top_p,
            )
        except Exception as e:
            logger.warning(f"BaseAgent初始化失败: {e}，将使用裸LLM调用")
            self.base_agent = None

        # RAG
        self.rag_service = None
        if self.use_rag:
            try:
                self.rag_service = get_rag_service(f"performance_{self.session_id}")
            except Exception as e:
                logger.warning(f"RAG初始化失败: {e}")

        # 工具
        self.content_indexer = ContentIndexer(use_semantic=False)
        self.score_validator = ScoreValidator()
        self.indicator_manager = IndicatorManager()

        # Prompt管理器
        self.prompt_manager = get_prompt_manager()

