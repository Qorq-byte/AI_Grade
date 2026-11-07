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
import time, json, uuid, re
import tempfile
from pathlib import Path
from threading import RLock

from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt
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
from backend.agents.tools.score_validator import (
    ScoreValidator, actual_indicator_score, allocation_valid, indicator_tree_error, score_number, validate_indicator_scores,
)
from backend.agents.tools.indicator_manager import IndicatorManager
from backend.agents.prompts.prompt_manager import get_prompt_manager, TemperatureProfile
from backend.agents.prompts.default_prompts import STEP_META, PromptTemplateFactory
from backend.database import SessionLocal

from backend.services.workflow_evidence import retrieve_step_evidence, render_evidence, report_evidence_index


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

STATE_BACKUP_DIR = Path(__file__).resolve().parents[2] / "state_backups"
_STATE_BACKUP_LOCK = RLock()


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
        self.use_rag = use_rag
        
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

        workflow.add_node(_NODE_INIT, self._node_init)
        workflow.set_entry_point(_NODE_INIT)
        previous = _NODE_INIT
        node_names = (
            _NODE_STEP_1, _NODE_STEP_2, _NODE_STEP_3, _NODE_STEP_4, _NODE_STEP_5, _NODE_STEP_6,
            _NODE_STEP_7, _NODE_STEP_8, _NODE_STEP_9, _NODE_STEP_10, _NODE_STEP_11, _NODE_STEP_12,
            _NODE_STEP_13, _NODE_STEP_14,
        )
        for step, name in enumerate(node_names, 1):
            workflow.add_node(name, self._tracked_node(step))
            workflow.add_edge(previous, name)
            previous = name
            if step in (3, 6, 11):
                gate = f"approval_{step}"
                workflow.add_node(gate, self._approval_node(step))
                workflow.add_edge(name, gate)
                previous = gate
        workflow.add_edge(previous, END)

        return workflow

    def _tracked_node(self, step):
        execute = getattr(self, f"_node_step{step}")

        def run(state: PerformanceState):
            from backend.routes.agent_routes import _WORKFLOW_PROGRESS

            progress = _WORKFLOW_PROGRESS.get(self.session_id)
            if progress is not None:
                if progress["status"] == "retrying" and progress["current_step"] != step:
                    progress["status"] = "running"
                progress["current_step"] = step
                progress.setdefault("started_steps", []).append(step)
            result = execute(state)
            self._persist_state_backup(result)
            return result

        return run

    def _approval_node(self, step):
        def review(state: PerformanceState):
            if not state.get("require_approval"):
                return {}
            decision = interrupt({"step_number": step})
            while not decision.get("approved"):
                decision = interrupt({"step_number": step, "feedback": decision.get("feedback", "")})
            decisions = dict(state.get("approvals", {}))
            decisions[str(step)] = {"approved": True, "feedback": decision.get("feedback", ""),
                                    "version": state["step_results"][step].get("version", 1),
                                    "user_id": decision.get("user_id")}
            self.update_state_backup(self.session_id, {"approvals": decisions})
            return {"approvals": decisions}

        return review

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
            try:
                evidence = retrieve_step_evidence(db, state, step_number, self.use_rag)
            except Exception:
                logger.warning("步骤 {} 的证据检索失败", step_number)
                evidence = []
            state.setdefault("step_evidence", {})[str(step_number)] = evidence
        finally:
            db.close()

        full_human_prompt = human_prompt + "\n\n## 可追溯证据\n" + render_evidence(evidence)
        if step_number == 7:
            full_human_prompt += (
                "\n正文后可附 JSON 对象，字段 scores 按指标 ID 映射到"
                "{actual_score, evidence_id, reason}。仅为有项目资料及评分依据的指标赋分。"
                "evidence_id 必须引用本步骤的项目资料编号。没有依据的指标写待核实，不输出分值。"
            )
        if step_number == 11:
            full_human_prompt += "\n程序评分结果：" + json.dumps(state.get("score_summary", {}), ensure_ascii=False)
        full_human_prompt += (
            "\n引用关键项目事实、金额、时间和扣分依据时保留对应证据编号。"
            "知识库条目只用于评价规则，不能证明项目实际发生的事实。"
            "不能核实的内容写「待核实」或「资料不足」。建议须对应已核实问题、责任主体和验收方式。"
            "保持项目名称与输入一致；对相互冲突的金额、时间列出两处来源并标注待核实。"
        )

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
                        if res.get("success") and res.get("message"):
                            return res["message"]
                        raise RuntimeError("模型调用失败") from e2
            raise RuntimeError("模型未初始化")

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
            if outputs:
                return next(iter(outputs.values()))
            raise RuntimeError("对比模型未返回结果")

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
            except Exception as exc:
                raise RuntimeError("模型调用失败") from exc

        raise RuntimeError("模型未初始化")

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
            "raw_text_full": (state.get("raw_text") or "")[:12000],
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
        v["effective_data_summary"] = (state.get("effective_data") or state.get("raw_text") or "")[:3000]


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
        if not content or not content.strip():
            raise ValueError("步骤未返回有效内容")
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
                    "evidence": state.get("step_evidence", {}).get(str(step_number), []),
                }
        except Exception as e:
            logger.debug(f"无法写入工作流进度(非关键): {e}")

    def _persist_state_backup(self, state: PerformanceState):
        """持久化关键状态到文件，防止服务器重启丢失"""
        try:
            sid = state.get("session_id", "") or self.session_id
            if not sid:
                return
            fields = (
                "session_id", "project_name", "indicator_type", "indicator_system", "indicator_scores",
                "indicator_standards", "cost_list", "effective_data", "scoring_standards",
                "evaluation_table", "indicator_analysis", "experience_summary", "problems_analysis",
                "suggestions", "comprehensive_evaluation", "basic_info", "work_introduction",
                "final_report", "score_summary", "score_validation", "step_evidence",
                "completed_steps", "warnings", "approvals", "review_steps", "indicator_score_map",
            )
            backup = {key: state[key] for key in fields if key in state}
            backup["step_results"] = {
                key: {**item, "created_at": str(item.get("created_at", "")),
                      "updated_at": str(item.get("updated_at", ""))}
                for key, item in state.get("step_results", {}).items()
            }
            self.update_state_backup(sid, backup)
            logger.info(f"状态备份已保存: {sid}")
        except Exception as e:
            logger.warning(f"状态备份失败(非关键): {e}")

    @staticmethod
    def update_state_backup(session_id: str, updates: Dict[str, Any]) -> None:
        """Merge a backup update and publish it with an atomic replacement."""
        with _STATE_BACKUP_LOCK:
            backup = PerformanceAgent.load_state_backup(session_id)
            backup.update(updates)
            STATE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            path = STATE_BACKUP_DIR / f"{session_id}.json"
            with tempfile.NamedTemporaryFile(mode="w", dir=STATE_BACKUP_DIR, encoding="utf-8",
                                             delete=False) as target:
                temp_path = Path(target.name)
                try:
                    json.dump(backup, target, ensure_ascii=False, indent=2)
                    target.close()
                    temp_path.replace(path)
                finally:
                    target.close()
                    temp_path.unlink(missing_ok=True)

    @staticmethod
    def load_state_backup(session_id: str) -> Dict[str, Any]:
        """从文件恢复关键状态"""
        try:
            with _STATE_BACKUP_LOCK:
                with (STATE_BACKUP_DIR / f"{session_id}.json").open(encoding="utf-8") as f:
                    backup = json.load(f)
                    return backup if isinstance(backup, dict) else {}
        except (OSError, ValueError):
            return {}

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
                        name = r.get("filename") or r.get("file_path", "")
                        extracted[name] = r.get("content", "")
                state["raw_text"] = "\n\n".join(extracted.values())
                state["all_raw_texts"] = extracted
                state["uploaded_files"] = [{"original_name": re.sub(r"^u\d+_[a-f0-9]{32}_", "", name)}
                                           for name in extracted]
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
            isys = json.loads(self._extract_json(content) or "null")
        except ValueError:
            isys = None
        issue = indicator_tree_error(isys)
        if issue:
            state.setdefault("warnings", []).append("步骤 3 待人工复核：" + issue)
            content = "资料不足或指标格式异常，待人工复核：" + issue + "\n\n" + content
        else:
            isys["indicator_type"] = itype
            state["indicator_system"] = isys
        self._record_step_result(state, 3, content)
        return state

    # Step 4
    def _node_step4(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 4] 生成指标分值")
        content = self._execute_step(state, 4)
        import copy
        system = state.get("indicator_system") or {}
        try:
            parsed = json.loads(self._extract_json(content) or "{}")
            scores = parsed.get("scores", parsed) if isinstance(parsed, dict) else {}
        except ValueError:
            scores = {}
        candidate = copy.deepcopy(system)
        applied = {}

        def apply(nodes):
            for node in nodes:
                identifier = node.get("id")
                value = score_number(scores.get(identifier))
                if value is not None:
                    node["score"] = value
                    applied[identifier] = value
                apply(node.get("children", []))

        if isinstance(scores, dict):
            apply(candidate.get("level1_indicators", []))
        if applied and allocation_valid(validate_indicator_scores(candidate)):
            state["indicator_system"] = candidate
            state["indicator_scores"] = applied
        elif scores or not system:
            state.setdefault("warnings", []).append("步骤 4 分值待复核，保留最近一次有效指标分值")
        if system:
            validation = validate_indicator_scores(state["indicator_system"])
            state["score_validation"] = validation
            if not allocation_valid(validation):
                state.setdefault("warnings", []).append("指标分值合计或层级分配未通过程序校验")
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
        content = (self._execute_step(state, 6) if state.get("indicator_system")
                   else "资料不足，指标体系待复核后方可生成评价表。")
        state["evaluation_table"] = content
        # 标记问卷可生成：指标体系表完成后可触发问卷生成
        state["questionnaire_ready"] = True
        self._record_step_result(state, 6, content)
        return state

    # Step 7
    def _node_step7(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 7] 生成指标得分分析")
        content = (self._execute_step(state, 7) if state.get("indicator_system")
                   else "资料不足，指标体系待复核后方可进行评分。")
        from backend.agents.tools.questionnaire_gen import indicator_leaves

        ids = {item["id"]: item for item in indicator_leaves(
            state.get("indicator_system", {}).get("level1_indicators", [])
        )}
        evidence_ids = {hit["id"] for hit in state.get("step_evidence", {}).get("7", [])
                        if hit["kind"] == "project"}
        scores = dict(state.get("indicator_score_map", {}))
        try:
            parsed = json.loads(self._extract_json(content) or "{}")
            candidates = parsed.get("scores", {}) if isinstance(parsed, dict) else {}
        except ValueError:
            candidates = {}
        for key, item in candidates.items() if isinstance(candidates, dict) else ():
            if key not in ids or key in scores or not isinstance(item, dict):
                continue
            evidence = item.get("evidence_id")
            reason = item.get("reason")
            value = actual_indicator_score(ids[key], {key: item})
            if isinstance(evidence, str) and evidence in evidence_ids and isinstance(reason, str) and reason.strip() and value is not None:
                scores[key] = {"actual_score": value, "evidence_id": evidence, "score_reason": reason,
                               "full_score": score_number(ids[key].get("score")), "source": "project_evidence"}
        state["indicator_score_map"] = scores
        if ids and len(scores) < len(ids):
            content = "部分指标缺少可核实的评分依据，未确认分值列为待核实。\n\n" + content
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
        indicator_scores = state.get("indicator_score_map", {})
        indicator_system = state.get("indicator_system", {})

        # 从indicator_system中提取各级指标
        def _extract_from_tree(indicators, parent_name="", level=1):
            for ind in (indicators or []):
                name = ind.get("name", "未命名")
                full_name = f"{parent_name}>{name}" if parent_name else name
                score_val = score_number(ind.get("score", ind.get("max_score"))) or 0
                actual_score = actual_indicator_score(ind, indicator_scores)
                children = ind.get("children") or []
                if not children and actual_score is not None and actual_score < score_val:
                    deduction_points.append({
                        "name": full_name,
                        "level": level,
                        "full_score": score_val,
                        "score": actual_score,
                        "deduction": round(score_val - actual_score, 2),
                        "reason": indicator_scores.get(ind.get("id"), {}).get("score_reason")
                                  or ind.get("deduction_reason") or "资料不足，原因待核实",
                    })
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
        summary = self._compute_score_summary(state)
        state["score_summary"] = summary
        content = self._execute_step(state, 11)
        score_line = (f"综合得分：{summary['total_score']:.2f} 分，评价等级：{summary['grade']}"
                      if summary["complete"] else "综合得分与评价等级：资料不足，待核实")
        content = re.sub(
            r"(?m)^.*(?:综合得分|评价等级)\s*[：:].*$", "", content
        ).strip()
        content = score_line + ("\n\n" + content if content else "")
        state["comprehensive_evaluation"] = content
        if summary["complete"]:
            state["evaluation_total_score"] = summary["total_score"]
        state["evaluation_grade"] = summary["grade"]
        self._record_step_result(state, 11, content)
        return state

    def _compute_score_summary(self, state: PerformanceState) -> dict:
        from backend.agents.tools.questionnaire_gen import indicator_leaves

        system = state.get("indicator_system") or {}
        items = list(indicator_leaves(system.get("level1_indicators", [])))
        scores = state.get("indicator_score_map") or {}
        actuals = [actual_indicator_score(item, scores) for item in items]
        total = round(sum(value for value in actuals if value is not None), 2)
        valid = bool(items) and allocation_valid(validate_indicator_scores(system))
        complete = valid and all(value is not None for value in actuals)
        grade = "待核实"
        if complete:
            grade = next((label for threshold, label in (
                (90, "优秀"), (80, "良好"), (60, "合格"),
            ) if total >= threshold), "不合格")
        return {"total_score": total, "complete": complete, "grade": grade,
                "scored_count": sum(value is not None for value in actuals), "total_count": len(items)}

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

        evidence_index = report_evidence_index(state.get("step_evidence", {}))
        if evidence_index:
            sections.append(evidence_index)
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

        try:
            result = self.app.invoke(initial_state, config)
            elapsed = time.time() - t0
            logger.info(f"<=== 工作流完成: {elapsed:.2f}s")
            return {
                "success": True,
                "session_id": self.session_id,
                "completed_steps": result.get("completed_steps", []),
                "current_step": result.get("step_control", {}).get("current_step", 0),
                "final_report": result.get("final_report", ""),
                "errors": result.get("errors", []),
                "warnings": result.get("warnings", []),
                "data": result,
            }
        except Exception as e:
            # GraphInterrupt 是正常中断（审批点），不是错误
            if "Interrupt" in type(e).__name__:
                logger.info(f"工作流在审批点中断: {e}")
                state = self.get_state(config)
                values = getattr(state, 'values', {}) if state else {}
                return {
                    "success": True,
                    "interrupted": True,
                    "session_id": self.session_id,
                    "completed_steps": values.get("completed_steps", []),
                    "current_step": values.get("step_control", {}).get("current_step", 0),
                    "final_report": values.get("final_report", ""),
                    "errors": values.get("errors", []),
                    "warnings": values.get("warnings", []),
                    "data": values,
                }
            logger.error(f"工作流失败: {e}")
            return {"success": False, "session_id": self.session_id, "error": str(e)}

    def run_step_by_step(
        self,
        initial_state: Dict[str, Any],
        config: Optional[RunnableConfig] = None,
    ):
        """流式逐步执行 (astream)，生成器 yield 每一步状态"""
        logger.info(f"===> 开始流式执行: session={self.session_id}")
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}

        async def _stream():
            try:
                async for event in self.app.astream(initial_state, config):
                    yield event
            except Exception as e:
                logger.error(f"流式执行失败: {e}")
                yield {"error": str(e)}

        return _stream()

    def run_with_events(
        self,
        initial_state: Dict[str, Any],
        config: Optional[RunnableConfig] = None,
    ):
        """带事件的流式执行 (astream_events)"""
        logger.info(f"===> 开始事件流: session={self.session_id}")
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}

        async def _stream_events():
            try:
                async for event in self.app.astream_events(initial_state, config, version="v2"):
                    yield {
                        "event": event.get("event", ""),
                        "node": event.get("name", ""),
                        "data": event.get("data", {}),
                        "timestamp": datetime.now().isoformat(),
                    }
            except Exception as e:
                logger.error(f"事件流失败: {e}")
                yield {"event": "error", "error": str(e)}

        return _stream_events()

    def resume_from_checkpoint(
        self,
        resume_value: Any = None,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        """从 checkpoint 恢复（人工确认后继续）"""
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}
        logger.info(f"===> 从checkpoint恢复: {config.get('configurable',{}).get('thread_id')}")
        try:
            if resume_value is not None:
                result = self.app.invoke(Command(resume=resume_value), config)
            else:
                result = self.app.invoke(None, config)
            return {
                "success": True,
                "session_id": self.session_id,
                "completed_steps": result.get("completed_steps", []),
                "current_step": result.get("step_control", {}).get("current_step", 0),
                "final_report": result.get("final_report", ""),
                "data": result,
            }
        except Exception as e:
            # GraphInterrupt 是 LangGraph 正常中断（步骤3/6/11审批点），不是错误
            if "Interrupt" in type(e).__name__:
                logger.info(f"恢复后在工作流中遇到中断: {e}")
                state = self.get_state(config)
                values = getattr(state, 'values', {}) if state else {}
                sc = values.get("step_control", {})
                approval_step = sc.get("approval_step")
                return {
                    "success": True,
                    "interrupted": True,
                    "session_id": self.session_id,
                    "completed_steps": values.get("completed_steps", []),
                    "current_step": sc.get("current_step", 0),
                    "approval_step": approval_step,
                    "approval_step_name": STEP_META.get(approval_step or 0, {}).get("name", ""),
                    "final_report": values.get("final_report", ""),
                    "data": values,
                }
            logger.error(f"恢复失败: {e}")
            return {"success": False, "error": str(e)}

    def get_state(self, config: Optional[RunnableConfig] = None):
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}
        try:
            return self.app.get_state(config)
        except Exception as e:
            logger.error(f"get_state失败: {e}")
            return None

    def resume_through_all(
        self,
        resume_value: Any = None,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        """
        自动穿越所有中断点，一次性完成剩余全部步骤。
        首次resume后如果遇到步骤6/11的GraphInterrupt，自动以 approved=True 继续，直到全部14步完成。
        """
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}

        total_loops = 0
        max_loops = 5  # 最多3个审批点 + 2次容错

        result = self.resume_from_checkpoint(resume_value=resume_value, config=config)
        total_loops += 1

        while result.get("interrupted") and total_loops < max_loops:
            logger.info(f"自动穿越中断点，第{total_loops}次自动审批后继续...")
            result = self.resume_from_checkpoint(
                resume_value={"approved": True, "feedback": "系统自动审批"},
                config=config,
            )
            total_loops += 1

        return result

    def get_state_history(self, config: Optional[RunnableConfig] = None) -> list:
        if config is None:
            config = {"configurable": {"thread_id": self.session_id}}
        try:
            return list(self.app.get_state_history(config))
        except Exception as e:
            logger.error(f"get_state_history失败: {e}")
            return []

    def switch_model(self, model_name: str, temperature: Optional[float] = None):
        self.model_name = model_name
        if temperature is not None:
            self.temperature = temperature
        if self.base_agent:
            self.base_agent.switch_model(model_name, temperature)

    def close(self):
        if self.base_agent:
            self.base_agent.close()
        logger.info(f"PerformanceAgent closed: session={self.session_id}")
