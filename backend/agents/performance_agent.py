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

        # ── 多模型对比 ──
        if mode in (
            ComparisonModeEnum.MULTI_COMPARE.value,
            ComparisonModeEnum.MULTI_DIALOGUE.value,
        ):
            from backend.agents.tools.multi_model_compare import MultiModelCompareService
            models_list = state.get("system_config", {}).get("enabled_models", [self.model_name])
            cmp = MultiModelCompareService(
                model_names=models_list,
                temperature=self.temperature,
                top_p=self.top_p,
            )
            result = cmp.compare(prompt=full_human_prompt, system_prompt=system_prompt)
            # 保存到 comparison_history
            if "comparison_history" not in state:
                state["comparison_history"] = {}
            state["comparison_history"][step_number] = {
                "step_number": step_number,
                "step_name": STEP_META.get(step_number, {}).get("name", ""),
                "models_used": result.get("models_used", []),
                "individual_outputs": result.get("individual_outputs", {}),
                "comparison_analysis": result.get("comparison_analysis", ""),
                "selected_best": None,
            }
            outputs = result.get("individual_outputs", {})
            return next(iter(outputs.values()), "") if outputs else ""

        # 回退
        if self.base_agent:
            try:
                from langchain_core.messages import SystemMessage, HumanMessage
                messages = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=full_human_prompt),
                ]
                response = self.base_agent.llm.invoke(messages, config={"timeout": 300})
                self._record_token(response, step_number, state)
                return response.content if hasattr(response, 'content') else str(response)
            except Exception:
                return "[模型调用失败]"

        return "[模型调用失败]"

    def _record_token(self, response, step_number: int, state: PerformanceState):
        """从LLM响应中提取并记录token消耗"""
        try:
            from backend.services.token_service import extract_token_usage_from_response, record_token_usage
            from backend.database import SessionLocal as SL
            from backend.models.user_models import WorkflowSession

            usage = extract_token_usage_from_response(response)
            total = usage.get("total_tokens", 0)
            if total <= 0:
                return

            # 从WorkflowSession获取user_id和project_id
            db2 = SL()
            try:
                ws = db2.query(WorkflowSession).filter(
                    WorkflowSession.session_id == self.session_id
                ).first()
                if ws and ws.user_id:
                    project_id = getattr(ws, "project_id", None) or (
                        state.get("project_id") if "project_id" in state else None
                    )
                    record_token_usage(
                        db=db2,
                        user_id=ws.user_id,
                        model_name=self.model_name,
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                        total_tokens=total,
                        session_id=self.session_id,
                        project_id=project_id,
                        step_number=step_number,
                    )
                    logger.info(
                        f"[Token] step={step_number}, user={ws.user_id}, "
                        f"prompt={usage['prompt_tokens']}, completion={usage['completion_tokens']}, "
                        f"total={total}"
                    )
            finally:
                db2.close()
        except Exception as e:
            logger.debug(f"Token记录失败(非关键): {e}")

    def _build_human_vars(self, state: PerformanceState, step_number: int) -> Dict[str, Any]:
        """为Human Prompt模板构建变量"""
        v = {
            "project_name": state.get("project_name", "未命名项目"),
            "project_type": state.get("project_type", "部门评价"),
            "raw_text_preview": (state.get("raw_text") or "")[:8000],
            "raw_text_full": state.get("raw_text") or "",
        }

        # 数据充足性检测：如果文档为空或极少，在prompt中添加醒目警告
        raw_full = state.get("raw_text") or ""
        raw_len = len(raw_full.strip()) if raw_full else 0
        if raw_len < 100:
            data_warning = (
                "\n\n### ⚠️⚠️⚠️ 数据严重不足警告 ⚠️⚠️⚠️\n"
                f"当前提供的原始文本仅 {raw_len} 个字符。\n"
                "**严禁编造任何具体信息！** 对于缺乏资料支撑的内容，\n"
                "必须逐条标注：「⚠️ 现有资料中未提供该信息，无法判断」。\n"
                "如果无法完成有意义的分析，请直接说明资料不足，不要输出虚假报告。\n"
            )
        elif raw_len < 1000:
            data_warning = (
                "\n\n### ⚠️ 数据不足警告 ⚠️\n"
                f"当前提供的原始文本仅 {raw_len} 个字符，信息量有限。\n"
                "请仅基于已有内容进行分析，缺失信息处标注「资料不足」。\n"
            )
        else:
            data_warning = ""

        uploaded = state.get("uploaded_files", [])
        v["uploaded_files_summary"] = (
            "\n".join(f"- {f.get('original_name', f.get('file_path', '?'))}" for f in uploaded)
            if uploaded else "暂无上传文件"
        )

        v["cost_list_summary"] = (state.get("cost_list") or "")[:2000]
        v["effective_data_categorized"] = json.dumps(
            state.get("effective_data_categorized", {}), ensure_ascii=False, indent=2
        )
        cp_idx = state.get("content_index", {})
        v["content_index_summary"] = "\n".join(
            f"- {k[:80]}: {v1}" for k, v1 in list(cp_idx.items())[:30]
        ) if cp_idx else "暂无索引"

        isys = state.get("indicator_system", {})
        isys_json = json.dumps(isys, ensure_ascii=False, indent=2)
        v["indicator_system_json"] = isys_json
        v["indicator_system_with_scores_json"] = isys_json
        v["indicator_system_full_json"] = isys_json

        v["evaluation_table_markdown"] = state.get("evaluation_table", "")
        v["indicator_analysis_summary"] = (state.get("indicator_analysis") or "")[:3000]
        v["indicator_analysis_full"] = state.get("indicator_analysis") or ""
        v["experience_summary"] = (state.get("experience_summary") or "")[:2000]
        v["problems_analysis_summary"] = (state.get("problems_analysis") or "")[:2000]
        v["problems_analysis_full"] = state.get("problems_analysis") or ""
        v["suggestions_summary"] = (state.get("suggestions") or "")[:2000]

        # 步骤9扣分点上下文
        v["deduction_context"] = state.get("_step9_deduction_context", "")
        # 步骤11评分汇总
        score_summary = state.get("score_summary", {})
        v["score_summary_json"] = json.dumps(score_summary, ensure_ascii=False, indent=2) if score_summary else ""

        ed = state.get("effective_data") or state.get("raw_text") or ""
        v["effective_data_summary"] = ed[:3000]

        v["work_introduction"] = state.get("work_introduction") or ""
        v["basic_info"] = state.get("basic_info") or ""
        v["evaluation_table"] = state.get("evaluation_table") or ""
        v["indicator_analysis"] = state.get("indicator_analysis") or ""
        v["comprehensive_evaluation"] = state.get("comprehensive_evaluation") or ""
        v["problems_analysis"] = state.get("problems_analysis") or ""
        v["suggestions"] = state.get("suggestions") or ""

        return v

    # ─── 结果记录 ────────────────────────────────────────

    def _record_step_result(self, state: PerformanceState, step_number: int, content: str):
        _ensure_step_results(state)
        _ensure_step_control(state)

        step_name = STEP_META.get(step_number, {}).get("name", f"Step {step_number}")
        now = datetime.now()

        state["step_results"][step_number] = {
            "step_number": step_number,
            "step_name": step_name,
            "status": StepStatusEnum.COMPLETED.value,
            "content": content,
            "raw_outputs": {"default": content},
            "comparison_results": None,
            "selected_model": self.model_name,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }

        sc = state["step_control"]
        sc["current_step"] = step_number
        sc["step_statuses"][step_number] = StepStatusEnum.COMPLETED.value
        sc["can_proceed"] = True

        if "completed_steps" in state:
            if step_number not in state["completed_steps"]:
                state["completed_steps"].append(step_number)
        if "errors" not in state:
            state["errors"] = []

        # 发布进度到全局字典，供SSE轮询端点读取
        try:
            from backend.routes.agent_routes import _WORKFLOW_PROGRESS
            sid = state.get("session_id", "")
            if sid and sid in _WORKFLOW_PROGRESS:
                p = _WORKFLOW_PROGRESS[sid]
                p["last_step"] = step_number
                p["step_results"][str(step_number)] = {
                    "step_name": step_name,
                    "content": content,
                    "status": StepStatusEnum.COMPLETED.value,
                }
        except Exception as e:
            logger.debug(f"无法写入工作流进度(非关键): {e}")

    def _persist_state_backup(self, state: PerformanceState):
        """持久化关键状态到文件，防止服务器重启丢失"""
        try:
            sid = state.get("session_id", "") or self.session_id
            if not sid:
                return
            backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
            os.makedirs(backup_dir, exist_ok=True)
            backup = {
                "session_id": sid,
                "indicator_system": state.get("indicator_system", {}),
                "effective_data": state.get("effective_data", "") or "",
                "project_name": state.get("project_name", ""),
                "indicator_scores": state.get("indicator_scores", {}),
                "indicator_standards": state.get("indicator_standards", {}),
                "step_results": {k: {"step_name": v.get("step_name", ""), "content": v.get("content", "")}
                                 for k, v in state.get("step_results", {}).items()},
            }
            with open(os.path.join(backup_dir, f"{sid}.json"), "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False, indent=2)
            logger.info(f"状态备份已保存: {sid}")
        except Exception as e:
            logger.warning(f"状态备份失败(非关键): {e}")

    @staticmethod
    def load_state_backup(session_id: str) -> Dict[str, Any]:
        """从文件恢复关键状态"""
        try:
            backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
            backup_path = os.path.join(backup_dir, f"{session_id}.json")
            if not os.path.exists(backup_path):
                return {}
            with open(backup_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _maybe_interrupt(self, state: PerformanceState, step_number: int) -> Optional[Any]:
        """(已禁用) 原步骤审批中断点 — 全部自动执行"""
        return None

    # ==================== 节点实现 ========================

    def _node_init(self, state: PerformanceState) -> PerformanceState:
        logger.info(f"[Init] session={state.get('session_id')}")

        file_paths = state.get("file_paths", [])
        if file_paths:
            try:
                from backend.services.file_processor import FileProcessor
                results = FileProcessor.process_multiple_files(file_paths)
                extracted = {}
                for r in results:
                    if r.get("success"):
                        name = r.get("original_name", r.get("file_path", ""))
                        extracted[name] = r.get("content", "")
                state["raw_text"] = "\n\n".join(extracted.values())
                state["all_raw_texts"] = extracted
                logger.info(f"[Init] 处理了 {len(results)} 个文件")
            except Exception as e:
                logger.error(f"[Init] 文件处理失败: {e}")
                state["raw_text"] = ""
                state["all_raw_texts"] = {}
                state.setdefault("errors", []).append(f"文件处理失败: {e}")

        _ensure_step_control(state)
        _ensure_step_results(state)
        _ensure_system_config(state)

        sc = state["step_control"]
        sc["current_step"] = 0
        sc["step_statuses"] = {}
        sc["can_proceed"] = True
        sc["requires_approval"] = False
        sc["comparison_mode"] = self.comparison_mode.value if hasattr(self.comparison_mode, 'value') else str(self.comparison_mode)
        sc["temperature_profile"] = self.temperature_profile.value if hasattr(self.temperature_profile, 'value') else str(self.temperature_profile)

        state.setdefault("completed_steps", []).clear()
        state.setdefault("errors", [])
        state.setdefault("warnings", [])
        state.setdefault("token_usage", {})
        state.setdefault("comparison_history", {})

        logger.info("[Init] 完成")
        return state

    # Step 1
    def _node_step1(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 1] 整理资料清单")
        content = self._execute_step(state, 1)
        state["cost_list"] = content
        self._record_step_result(state, 1, content)
        return state

    # Step 2
    def _node_step2(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 2] 核心有效信息提取与索引")
        content = self._execute_step(state, 2)
        state["effective_data"] = content

        try:
            raw_texts = state.get("all_raw_texts", {})
            if not raw_texts and state.get("raw_text"):
                raw_texts = {"原始资料": state["raw_text"]}
            if raw_texts:
                self.content_indexer.index(raw_texts)
                state["content_index"] = self.content_indexer.get_index()
                state["effective_data_categorized"] = self.content_indexer.get_categorized_content()
                contradictions = self.content_indexer.get_contradictions()
                if contradictions:
                    wl = state.setdefault("warnings", [])
                    for c in contradictions[:5]:
                        wl.append(f"[内容矛盾] {c.get('category','')}: {c.get('description','')}")
        except Exception as e:
            logger.warning(f"[Step 2] 内容索引失败（非致命）: {e}")

        self._record_step_result(state, 2, content)
        return state

    # Step 3
    def _node_step3(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 3] 构建指标体系")
        itype = state.get("indicator_type") or "项目支出指标体系"
        content = self._execute_step(state, 3)
        try:
            j = self._extract_json(content)
            isys = json.loads(j) if j else {"raw_text": content}
        except Exception:
            isys = {"raw_text": content, "indicator_type": itype, "level1_indicators": []}
        if isinstance(isys, dict):
            isys.setdefault("indicator_type", itype)
        state["indicator_system"] = isys
        try:
            self.indicator_manager.import_custom_system(isys)
            state["indicator_system"] = self.indicator_manager.to_dict()
        except Exception:
            pass
        self._record_step_result(state, 3, content)
        self._persist_state_backup(state)  # 持久化关键状态，防止服务器重启丢失
        return state

    # Step 4
    def _node_step4(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 4] 生成指标分值")
        content = self._execute_step(state, 4)
        try:
            j = self._extract_json(content)
            sd = json.loads(j) if j else {}
            state["indicator_scores"] = sd.get("scores", sd)
        except Exception:
            state["indicator_scores"] = {}

        try:
            isys2 = state.get("indicator_system", {})
            if isys2.get("level1_indicators"):
                val = validate_indicator_scores(isys2)
                state["score_validation"] = val
                if not val.get("overall_passed"):
                    wl = state.setdefault("warnings", [])
                    for vv in val.get("validations", []):
                        if not vv.get("passed"):
                            wl.append(vv.get("message", ""))
        except Exception as e:
            logger.warning(f"[Step 4] 分值校验失败: {e}")

        self._record_step_result(state, 4, content)
        return state

    # Step 5
    def _node_step5(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 5] 生成评分标准")
        content = self._execute_step(state, 5)
        state["scoring_standards"] = {"raw": content}
        self._record_step_result(state, 5, content)
        return state

    # Step 6
    def _node_step6(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 6] 生成评价指标体系表")
        content = self._execute_step(state, 6)
        state["evaluation_table"] = content
        # 标记问卷可生成：指标体系表完成后可触发问卷生成
        state["questionnaire_ready"] = True
        self._record_step_result(state, 6, content)
        return state

    # Step 7
    def _node_step7(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 7] 生成指标得分分析")
        content = self._execute_step(state, 7)
        state["indicator_analysis"] = content
        self._record_step_result(state, 7, content)
        return state

    # Step 8
    def _node_step8(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 8] 总结经验做法")
        content = self._execute_step(state, 8)
        state["experience_summary"] = content
        self._record_step_result(state, 8, content)
        return state

    # Step 9
    def _node_step9(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 9] 分析问题及原因")
        # 自动从步骤7的指标分析中提取扣分点，传递给LLM作为参考
        indicator_analysis = state.get("indicator_analysis") or ""
        if indicator_analysis:
            # 提取扣分点：从指标分析中识别得分低于满分的指标
            deduction_points = self._extract_deduction_points(state)
            state["deduction_points"] = deduction_points
            # 将扣分点信息附加给步骤9的prompt（通过临时存储）
            state["_step9_deduction_context"] = "\n".join(
                f"- {d.get('name', '?')}: 满分{d.get('full_score', '?')}，得分{d.get('score', '?')}，扣分原因：{d.get('reason', '资料不足')}"
                for d in deduction_points[:10]
            ) if deduction_points else "（未检测到明确扣分项）"
        content = self._execute_step(state, 9)
        state["problems_analysis"] = content
        self._record_step_result(state, 9, content)
        return state

    def _extract_deduction_points(self, state: PerformanceState) -> list:
        """从步骤7的指标得分分析中提取扣分点列表"""
        deduction_points = []
        indicator_scores = state.get("indicator_scores", {})
        indicator_system = state.get("indicator_system", {})

        # 从indicator_system中提取各级指标
        def _extract_from_tree(indicators, parent_name="", level=1):
            for ind in (indicators or []):
                name = ind.get("name", "未命名")
                full_name = f"{parent_name}>{name}" if parent_name else name
                score_val = ind.get("score", ind.get("max_score", 0)) or 0
                actual_score = ind.get("actual_score")
                if actual_score is not None and actual_score < score_val:
                    deduction_points.append({
                        "name": full_name,
                        "level": level,
                        "full_score": score_val,
                        "score": actual_score,
                        "deduction": round(score_val - actual_score, 2),
                        "reason": ind.get("deduction_reason", f"指标「{name}」得分低于满分"),
                    })
                children = ind.get("children", []) or []
                _extract_from_tree(children, full_name, level + 1)

        level1 = indicator_system.get("level1_indicators", [])
        _extract_from_tree(level1)
        return deduction_points

    # Step 10
    def _node_step10(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 10] 提出改进建议")
        content = self._execute_step(state, 10)
        state["suggestions"] = content
        self._record_step_result(state, 10, content)
        return state

    # Step 11
    def _node_step11(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 11] 综合评价分析及结论")
        content = self._execute_step(state, 11)
        state["comprehensive_evaluation"] = content

        # ── 自动提取评价等级和综合得分 ──
        try:
            m = re.search(r'评价等级[：:]\s*(优秀|良好|中等|合格|差|\w)', content)
            if m:
                state["evaluation_grade"] = m.group(1)
            m2 = re.search(r'综合得分[：:]\s*([\d.]+)\s*分', content)
            if m2:
                state["evaluation_total_score"] = float(m2.group(1))
        except Exception:
            pass

        # ── 自动计算指标得分汇总 ──
        try:
            score_summary = self._compute_score_summary(state)
            state["score_summary"] = score_summary
        except Exception as e:
            logger.warning(f"[Step 11] 评分汇总计算失败（非致命）: {e}")

        self._record_step_result(state, 11, content)
        return state

    def _compute_score_summary(self, state: PerformanceState) -> dict:
        """根据指标体系中的实际得分，汇总计算各级指标得分"""
        indicator_system = state.get("indicator_system", {})
        summary = {
            "total_score": 0,
            "level1_scores": [],
            "level2_scores": [],
            "level3_scores": [],
            "deduction_details": [],
        }

        def _traverse(indicators, parent_name="", level=1, is_leaf=True):
            for ind in (indicators or []):
                name = ind.get("name", "未命名")
                full_name = f"{parent_name}>{name}" if parent_name else name
                max_score = ind.get("score", ind.get("max_score", 0)) or 0
                actual = ind.get("actual_score", max_score)  # 默认满分
                try:
                    actual = float(actual) if actual is not None else float(max_score)
                except (ValueError, TypeError):
                    actual = float(max_score) if max_score else 0

                children = ind.get("children", []) or []
                has_children = len(children) > 0

                # 有子指标：递归处理子指标（一级指标总分 = 子指标总分之和，不重复计）
                if has_children:
                    _traverse(children, full_name, level + 1, False)
                else:
                    # 叶子节点：只记录叶子节点的实际分值
                    entry = {
                        "name": full_name,
                        "level": level,
                        "max_score": max_score,
                        "actual_score": actual,
                        "deduction": round(max_score - actual, 2) if actual < max_score else 0,
                    }
                    if level == 1:
                        summary["level1_scores"].append(entry)
                    elif level == 2:
                        summary["level2_scores"].append(entry)
                    else:
                        summary["level3_scores"].append(entry)

                    if actual < max_score:
                        summary["deduction_details"].append(entry)

                    summary["total_score"] += actual

        level1 = indicator_system.get("level1_indicators", [])
        _traverse(level1)

        # 四舍五入保留两位小数
        summary["total_score"] = round(summary["total_score"], 2)

        # 根据得分判定等级（使用三等九级制）
        ts = summary["total_score"]
        if ts >= 90:
            summary["grade"] = "优秀"
        elif ts >= 80:
            summary["grade"] = "良好"
        elif ts >= 70:
            summary["grade"] = "中等"
        elif ts >= 60:
            summary["grade"] = "合格"
        else:
            summary["grade"] = "不合格"

        return summary

    # Step 12
    def _node_step12(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 12] 生成基本情况")
        content = self._execute_step(state, 12)
        state["basic_info"] = content
        self._record_step_result(state, 12, content)
        return state

    # Step 13
    def _node_step13(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 13] 生成评价工作简介")
        content = self._execute_step(state, 13)
        state["work_introduction"] = content
        self._record_step_result(state, 13, content)
        return state

    # Step 14
    def _node_step14(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 14] 组装最终报告")
        # 不依赖LLM，直接从各步骤结果拼装完整报告
        # 支持自定义章节排序（通过 state 中的 report_section_order）
        content = self._assemble_final_report(state)
        state["final_report"] = content
        self._record_step_result(state, 14, content)
        self._persist_state_backup(state)  # 关键节点保存最终备份
        logger.info("[Step 14] 报告生成完成")
        return state

    def _assemble_final_report(self, state: PerformanceState) -> str:
        """从各步骤结果拼装最终绩效评价报告，支持自定义章节排序"""
        sections = []

        # 标题
        project_name = state.get("project_name", "绩效评价报告")
        sections.append(f"# {project_name}\n")

        # 默认章节映射（章节编号 → 标题 → 内容key）
        DEFAULT_CHAPTERS = [
            ("一", "绩效评价工作开展情况", "work_introduction"),
            ("二", "项目基本情况", "basic_info"),
            ("三", "评价指标体系", "evaluation_table"),
            ("四", "指标得分及分析", "indicator_analysis"),
            ("五", "综合评价分析及结论", "comprehensive_evaluation"),
            ("六", "主要经验做法", "experience_summary"),
            ("七", "存在问题及原因分析", "problems_analysis"),
            ("八", "改进建议", "suggestions"),
        ]

        # 检查是否有自定义章节排序
        custom_order = state.get("report_section_order")
        if custom_order and isinstance(custom_order, list):
            # 使用自定义排序
            ordered_chapters = []
            for key in custom_order:
                for num, name, content_key in DEFAULT_CHAPTERS:
                    if content_key == key or f"{num}、{name}" == key:
                        ordered_chapters.append((f"{num}、{name}", state.get(content_key)))
                        break
            chapter_map = ordered_chapters if ordered_chapters else [
                (f"{num}、{name}", state.get(content_key))
                for num, name, content_key in DEFAULT_CHAPTERS
            ]
        else:
            chapter_map = [
                (f"{num}、{name}", state.get(content_key))
                for num, name, content_key in DEFAULT_CHAPTERS
            ]

        # 目录
        toc = ["## 目 录\n"]
        for title, _ in chapter_map:
            toc.append(title)
        sections.append("\n".join(toc))
        sections.append("\n---\n")

        for title, content_section in chapter_map:
            clean_content = (content_section or "").strip()
            # 过滤掉明显的错误消息
            if "[调用失败" in clean_content or "[模型" in clean_content:
                clean_content = "（本部分内容尚未生成，请检查AI模型配置后重新运行工作流）"
            if not clean_content:
                clean_content = "（本部分内容尚未生成）"

            # 如果内容不以标题开头，添加标题
            if not clean_content.startswith("#"):
                sections.append(f"## {title}\n\n{clean_content}\n")
            else:
                sections.append(f"{clean_content}\n")

        sections.append("\n---\n")
        sections.append(f"*报告由云海睿评AI生成系统自动生成*")

        return "\n".join(sections)

    # ─── 工具 ───────────────────────────────────────────

    def _extract_json(self, text: str) -> Optional[str]:
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if m:
            return m.group(1).strip()
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            return text[start:end + 1]
        return None

    # ==================== 执行方法 ========================

    def run_workflow(
        self,
        initial_state: Dict[str, Any],
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        logger.info(f"===> 开始执行工作流: session={self.session_id}")
        t0 = time.time()
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}
        elif "configurable" not in config:
            config["configurable"] = {"thread_id": self.session_id}
        elif "thread_id" not in config["configurable"]:
            config["configurable"]["thread_id"] = self.session_id

