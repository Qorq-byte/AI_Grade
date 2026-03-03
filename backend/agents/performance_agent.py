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

        # 构建 & 编译
        self.workflow = self._build_workflow()
        self.checkpointer = MemorySaver()
        self.app: CompiledStateGraph = self.workflow.compile(checkpointer=self.checkpointer)

        _cm = comparison_mode.value if hasattr(comparison_mode, 'value') else str(comparison_mode)
        _tp = temperature_profile.value if hasattr(temperature_profile, 'value') else str(temperature_profile)
        logger.info(
            f"PerformanceAgent v2 init: session={self.session_id}, "
            f"model={model_name}, mode={_cm}, "
            f"temp={_tp}"
        )

    # ─── 工作流图构建 ────────────────────────────────────

    def _build_workflow(self) -> StateGraph:
        workflow = StateGraph(PerformanceState)

        workflow.add_node(_NODE_INIT,    self._node_init)
        workflow.add_node(_NODE_STEP_1,  self._node_step1)
        workflow.add_node(_NODE_STEP_2,  self._node_step2)
        workflow.add_node(_NODE_STEP_3,  self._node_step3)
        workflow.add_node(_NODE_STEP_4,  self._node_step4)
        workflow.add_node(_NODE_STEP_5,  self._node_step5)
        workflow.add_node(_NODE_STEP_6,  self._node_step6)
        workflow.add_node(_NODE_STEP_7,  self._node_step7)
        workflow.add_node(_NODE_STEP_8,  self._node_step8)
        workflow.add_node(_NODE_STEP_9,  self._node_step9)
        workflow.add_node(_NODE_STEP_10, self._node_step10)
        workflow.add_node(_NODE_STEP_11, self._node_step11)
        workflow.add_node(_NODE_STEP_12, self._node_step12)
        workflow.add_node(_NODE_STEP_13, self._node_step13)
        workflow.add_node(_NODE_STEP_14, self._node_step14)

        workflow.set_entry_point(_NODE_INIT)

        workflow.add_edge(_NODE_INIT,   _NODE_STEP_1)
        workflow.add_edge(_NODE_STEP_1, _NODE_STEP_2)
        workflow.add_edge(_NODE_STEP_2, _NODE_STEP_3)
        workflow.add_edge(_NODE_STEP_3, _NODE_STEP_4)
        workflow.add_edge(_NODE_STEP_4, _NODE_STEP_5)
        workflow.add_edge(_NODE_STEP_5, _NODE_STEP_6)
        workflow.add_edge(_NODE_STEP_6, _NODE_STEP_7)
        workflow.add_edge(_NODE_STEP_7, _NODE_STEP_8)
        workflow.add_edge(_NODE_STEP_8, _NODE_STEP_9)
        workflow.add_edge(_NODE_STEP_9, _NODE_STEP_10)
        workflow.add_edge(_NODE_STEP_10, _NODE_STEP_11)
        workflow.add_edge(_NODE_STEP_11, _NODE_STEP_12)
        workflow.add_edge(_NODE_STEP_12, _NODE_STEP_13)
        workflow.add_edge(_NODE_STEP_13, _NODE_STEP_14)
        workflow.add_edge(_NODE_STEP_14, END)

        return workflow

    # ─── 统一执行引擎 ────────────────────────────────────

    def _execute_step(self, state: PerformanceState, step_number: int) -> str:
        """
        统一步骤执行：
        1. PromptManager 获取 System/Human Prompt
        2. 按 comparison_mode 选单模型 / 多模型对比
        3. 可选 RAG 检索增强
        4. 返回 LLM 生成文本，同时自动记录token消耗
        """
        sc = _ensure_step_control(state)
        mode = sc.get("comparison_mode", ComparisonModeEnum.SINGLE.value)

        # Prompt（使用数据库会话加载自定义Prompt，无记录时回退到默认模板）
        db = SessionLocal()
        try:
            # 语言风格指令（步骤9、10使用）
            lang_style_kwargs = {}
            sc = _ensure_step_control(state)
            lang_style = sc.get("language_style", "中立")
            if step_number in (9, 10):
                from backend.agents.prompts.default_prompts import _LANGUAGE_STYLE_INSTRUCTIONS
                lang_instr = _LANGUAGE_STYLE_INSTRUCTIONS.get(lang_style, _LANGUAGE_STYLE_INSTRUCTIONS["中立"])
                lang_style_kwargs["language_style_instruction"] = lang_instr

            system_prompt = self.prompt_manager.get_system_prompt(
                step_number,
                db_session=db,
                temperature=self.temperature_profile,
                **lang_style_kwargs,
            )

            vars_for_human = self._build_human_vars(state, step_number)
            human_prompt = self.prompt_manager.get_human_prompt(
                step_number, db_session=db, **vars_for_human
            )
        finally:
            db.close()

        # RAG
        rag_context = ""
        if self.use_rag and self.rag_service:
            try:
                rag_docs = self.rag_service.search(human_prompt[:500], k=3)
                if rag_docs:
                    rag_context = "\n\n【知识库参考】\n" + "\n---\n".join(
                        d["content"][:500] for d in rag_docs
                    )
            except Exception as e:
                logger.warning(f"RAG检索失败: {e}")

        full_human_prompt = human_prompt + rag_context

        # 步骤3：注入用户选择的指标体系类型（6种类型之一，真正影响生成结果）
        if step_number == 3:
            itype = state.get("indicator_type") or "项目支出指标体系"
            type_instruction = (
                f"\n\n### 指标体系类型要求（必须遵守）\n"
                f"本次评价请按照「{itype}」类型构建三级指标体系。"
            )
            if itype == "自定义":
                type_instruction += "请完全根据项目自身特点自由设计指标体系，不要套用固定模板。"
            else:
                type_instruction += (
                    "一级指标结构、指标名称与分值分配均须符合该类型财政绩效评价的规范要求，"
                    "并在输出JSON的 indicator_type 字段中写为该类型名称。"
                )
            full_human_prompt += type_instruction

        # 数据不足时在prompt前添加醒目警告
        raw_data = state.get("raw_text") or ""
        raw_len = len(raw_data.strip()) if raw_data else 0
        if raw_len < 100:
            full_human_prompt = (
                "### ⚠️⚠️⚠️ 严重警告：现有资料文本仅 {} 个字符，信息严重不足！\n"
                "严禁编造任何数据！缺失处必须标注「⚠️ 资料不足，无法判断」\n\n"
            ).format(raw_len) + full_human_prompt
        elif raw_len < 1000:
            full_human_prompt = (
                "### ⚠️ 注意：现有资料文本仅 {} 个字符，信息量有限，仅基于已有内容分析。\n\n"
            ).format(raw_len) + full_human_prompt

        # ── 单模型 ──
        if mode == ComparisonModeEnum.SINGLE.value:
            if self.base_agent:
                # 使用 langchain SystemMessage/HumanMessage 确保兼容性
                try:
                    from langchain_core.messages import SystemMessage, HumanMessage
                    messages = [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=full_human_prompt),
                    ]
                    response = self.base_agent.llm.invoke(messages, config={"timeout": 300})
                    self._record_token(response, step_number, state)
                    return response.content if hasattr(response, 'content') else str(response)
                except Exception as e:
                    logger.warning(f"LLM invoke失败: {e}，回退到合并消息模式")
                    try:
                        combined = system_prompt + "\n\n" + full_human_prompt
                        response = self.base_agent.llm.invoke(combined, config={"timeout": 300})
                        self._record_token(response, step_number, state)
                        return response.content if hasattr(response, 'content') else str(response)
                    except Exception as e2:
                        logger.warning(f"LLM合并调用也失败: {e2}，回退到chat方法")
                        res = self.base_agent.chat(full_human_prompt, use_memory=False)
                        return res.get("message", "") if res.get("success") else f"[调用失败: {res.get('message', str(e2))}]"
            return "[模型未初始化]"

