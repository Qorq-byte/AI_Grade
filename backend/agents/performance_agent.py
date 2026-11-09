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
import os
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
from backend.agents.tools import structured_output
from backend.agents.prompts.prompt_manager import get_prompt_manager, TemperatureProfile
from backend.agents.prompts.default_prompts import STEP_META, PromptTemplateFactory
from backend.database import SessionLocal
from backend.utils.llm_helper import get_model_provider

from backend.services.workflow_evidence import retrieve_step_evidence, render_evidence, report_evidence_index
from backend.services import doc_coverage


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

# 备份的发布与删除都是「先写临时文件，再原子替换/删除」。Windows 上目标文件只要
# 还挂着读句柄（本进程的列表接口、别的 worker、杀毒软件扫描都算），`os.replace`
# 与 `unlink` 就会抛 `PermissionError [WinError 5]`——`_STATE_BACKUP_LOCK` 只挡得住
# 本进程，挡不住这些。所以在预算内退避重试；预算耗尽仍失败就抛出去，让调用方
# 决定是吞掉还是报错，绝不静默丢数据。
STATE_BACKUP_IO_BUDGET_SEC = 2.0
STATE_BACKUP_IO_BASE_DELAY_SEC = 0.02
STATE_BACKUP_IO_MAX_DELAY_SEC = 0.25


def _retry_on_transient_permission(op, budget: Optional[float] = None):
    """反复执行 op，直到成功、或遇到非权限类异常、或重试预算耗尽。"""
    budget = STATE_BACKUP_IO_BUDGET_SEC if budget is None else budget
    deadline = time.monotonic() + budget
    delay = STATE_BACKUP_IO_BASE_DELAY_SEC
    while True:
        try:
            return op()
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, STATE_BACKUP_IO_MAX_DELAY_SEC)


def _publish_backup(temp_path: Path, path: Path, budget: Optional[float] = None) -> None:
    """把临时文件原子替换成正式备份；目标被占用时在预算内重试。"""
    _retry_on_transient_permission(lambda: temp_path.replace(path), budget)


def _discard_backup(path: Path, budget: Optional[float] = None) -> bool:
    """删除备份文件，文件本来就不存在时返回 False；被占用时在预算内重试。"""
    def remove() -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
    return _retry_on_transient_permission(remove, budget)



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


# ==================== 步骤 7 评分落地处置口径 ====================

# 一个叶子指标没进 indicator_score_map，原因至少有 6 种，对外必须分开说：
# 实测一轮真实 14 步里 23 个叶子只落地 2 个，其中 16 个是模型按提示词要求
# 明确写「待核实」——那是**正确的拒绝**，与「系统把评分丢了」完全不是一回事。
DISPOSITION_LABELS = {
    "accepted": "已落地",
    "model_pending": "模型判定资料不足（待核实）",
    "anchor_weak": "引用可定位到原文行、但该行未进入检索证据",
    "anchor_unresolved": "引用无法定位",
    "anchor_too_coarse": "引用只到文件（没到行）",
    "score_invalid": "分值不合法",
    "reason_missing": "缺少评分理由",
    "not_mentioned": "模型未提及",
}

# 解析失败的原因 → 对外状态。默认仍是「引用无法定位」：编造编号、引了别的文件、
# 行号越界这几类，对用户来说是同一件事（引不到），不必再拆。
# 只有「只写到文件名」值得单列：它不是模型偷懒，而是材料里没有更细的东西可引
# （详见 workflow_evidence 的 PROJECT_CHAR_BUDGET）。
#
# 曾经想做 `anchor_missing`（根本没写引用），最后没收：实测只有 2 条，而且
# `evidence_id` 为空时会回退去读 `reason`，所以「没写引用」在台账里通常表现为
# `no_match` 而不是 `empty`——再加一个几乎不进人的状态只会增加口径。
ANCHOR_STATUS_BY_REASON = {
    "too_coarse": "anchor_too_coarse",
}

# 模型按「未取得佐证」计分时，理由必须以这个前缀开头（提示词第 3 条要求）。
# 作用只有一个：把这批分值和「实测表现得分」分开计数，好在报告里如实披露。
NO_EVIDENCE_MARKER = "未提供佐证"

# 步骤 3 的 `scoring_standard` 是「尺子」，不该写本项目的资料核对结论。
# 实测 27 份备份里 400/543（74%）的叶子把结论写进了这一栏，报告因此自相矛盾：
# 标准栏写着「无法判断」，同行的分值却已经落地。这里只做计数，不改原文。
STANDARD_VERDICT_MARKERS = ("待核实", "无法判断", "资料不足", "现有资料未", "本项资料未")

# ── 单次输出的 token 预算（按步骤）────────────────────────
# 为什么需要这张表：`settings.MAX_TOKENS`（4000）**从来没有生效过**。
# langchain-openai 1.2.1 把 `max_tokens` 发成 `max_completion_tokens`，
# 而 DeepSeek 的兼容接口只认请求体里的 `max_tokens`——这个字段被当成未知参数丢掉，
# 模型于是一路跑到自己的单次硬顶 8192 token。证据：`token_usage` 表里会话
# 72111c2c 的步骤 7 正好 8192，且那一次输出在 12,862 字处断在半截 JSON 上。
# 所以预算必须显式塞进 `extra_body.max_tokens`（见 `_llm_for_step`）。
#
# 为什么只给步骤 7、而不是全步骤都设 4000：实测（token_usage 全表）
# 步骤 1/2/3/4/5/6/7/9/10 的最大 completion 都超过 4000，步骤 5 达 10,352。
# 把 4000 变成真的，等于一次性截断 9 个步骤——那是另一个批次的量级与风险，
# 不在「步骤 7 输出长度纪律」的范围内。这里只收紧本批真正量过的那一步。
STEP_OUTPUT_BUDGETS = {7: 6000}

# 这些 finish_reason 表示「不是模型自己写完的」，而是被长度上限砍断的。
TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})


def step_output_budget(step_number: int) -> Optional[int]:
    """该步骤显式配置的输出 token 预算；没配就返回 None（沿用模型默认）。"""
    return STEP_OUTPUT_BUDGETS.get(int(step_number))


def audit_standard_purity(system: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """数一数有多少条叶子指标的 `scoring_standard` 夹带了资料核对结论。

    只计数、不改写：把命中数暴露出来，好判断提示词的约束到底有没有生效；
    若在这里顺手删掉那些话，模型行为就被掩盖了，报告里的矛盾也只是从
    「看得见」变成「看不见」。
    """
    from backend.agents.tools.questionnaire_gen import indicator_leaves

    leaves = list(indicator_leaves((system or {}).get("level1_indicators") or []))
    hits = [leaf.get("id") for leaf in leaves
            if any(marker in str(leaf.get("scoring_standard") or "")
                   for marker in STANDARD_VERDICT_MARKERS)]
    return {"leaves": len(leaves), "with_verdict": len(hits), "ids": hits}



def summarize_disposition(items: Dict[str, Dict[str, Any]],
                          unmatched: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把逐指标的处置结果汇总成可展示的台账（含一行可直接写进正文/告警的结论）。

    `unmatched` 是模型给了分、但指标 id 不在当前体系内的条目（实测真实一轮里 6 条评分
    有 4 条是这种）。它们不进 `items`（那份台账按叶子指标建），但必须单独计数——
    否则「模型编了 id」和「模型没提这个指标」在对外口径里长得一模一样。
    """
    unmatched = list(unmatched or [])
    if not items and not unmatched:
        return {}
    counts = {key: 0 for key in DISPOSITION_LABELS}
    accepted_score = 0.0
    no_evidence_scored = 0
    for item in items.values():
        status = item.get("status")
        counts[status] = counts.get(status, 0) + 1
        if status == "accepted":
            value = score_number(item.get("actual_score"))
            if value is not None:
                accepted_score += value
            if item.get("no_evidence"):
                no_evidence_scored += 1
    total = len(items)
    accepted = counts.get("accepted", 0)
    reasons = [
        f"{label} {counts[key]} 项"
        for key, label in DISPOSITION_LABELS.items()
        if key != "accepted" and counts.get(key)
    ]
    line = (f"步骤 7 评分落地 {accepted}/{total} 项"
            f"（已落地分值合计 {round(accepted_score, 2)} 分）")
    if reasons:
        line += "：" + "、".join(reasons)
    if no_evidence_scored:
        line += (f"；其中 {no_evidence_scored} 项按「未取得佐证」计分"
                 f"（非实测结果，须在评价局限中说明）")
    if unmatched:
        keys = [str(item.get("indicator_id") or "") for item in unmatched]
        shown = "、".join(keys[:5]) + ("等" if len(keys) > 5 else "")
        line += f"；另有 {len(unmatched)} 条评分对应的指标 ID 不在当前指标体系内（{shown}）"
    return {
        "step": 7,
        "total": total,
        "counts": counts,
        "accepted_score": round(accepted_score, 2),
        "no_evidence_scored": no_evidence_scored,
        "summary_line": line,
        "items": items,
        "unmatched": unmatched,
    }


def refresh_disposition(previous: Dict[str, Any], score_map: Dict[str, Any],
                        leaf_ids: List[str]) -> Dict[str, Any]:
    """人工改过评分后，把处置台账对齐到当前 `score_map`。

    只改「已落地」这一栏，不重算「为什么没落地」——那个原因只有步骤 7 当时知道，
    事后重新推断等于编。这样人工补分后界面不会继续显示旧结论（Batch 9 踩过同类坑：
    status 返回落库旧结论，徽标还是绿的）。
    """
    items = dict((previous or {}).get("items") or {})
    for leaf_id in leaf_ids:
        entry = score_map.get(leaf_id)
        if entry:
            items[leaf_id] = {
                "status": "accepted",
                "actual_score": score_number(entry.get("actual_score")),
                "evidence": entry.get("evidence_id"),
                "evidence_anchor": entry.get("evidence_anchor"),
                "note": "沿用已有评分" if entry.get("source") == "project_evidence" else "人工补分",
            }
        elif leaf_id in items:
            if items[leaf_id].get("status") == "accepted":
                items[leaf_id] = {"status": "not_mentioned", "note": "人工移除后未再赋分"}
        else:
            items[leaf_id] = {"status": "not_mentioned"}
    # unmatched 记录的是「模型给了体系里不存在的 id」，人工补分不会让这些 id 变合法，原样保留
    return summarize_disposition(items, (previous or {}).get("unmatched"))


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

        # 档位真正生效：temperature / top_p 由档位决定，请求参数只在档位为普通时作为覆盖
        profile_params = {
            TemperatureProfileEnum.PRECISE: (0.3, 0.5),
            TemperatureProfileEnum.NORMAL: (0.7, 0.9),
            TemperatureProfileEnum.CREATIVE: (0.9, 0.95),
        }
        profile_temp, profile_top_p = profile_params.get(
            self.temperature_profile, (temperature, top_p)
        )
        if self.temperature_profile is TemperatureProfileEnum.NORMAL:
            profile_temp, profile_top_p = temperature, top_p
        self.temperature, self.top_p = profile_temp, profile_top_p

        # BaseAgent（单模型模式）
        try:
            self.base_agent = BaseAgent(
                session_id=self.session_id,
                model_name=model_name,
                temperature=self.temperature,
                top_p=self.top_p,
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
            from backend.services import progress_bus

            progress = progress_bus.get_progress(self.session_id)
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

        # ── 资料覆盖度：明确告诉模型「这份资料它看到了多少」 ──
        coverage_note = vars_for_human.get("document_coverage") or ""
        if coverage_note:
            full_human_prompt += "\n\n" + coverage_note
        omitted_digest = vars_for_human.get("omitted_digest") or ""
        if omitted_digest:
            full_human_prompt += (
                "\n\n## 未直接引用部分的要点（程序摘要，仅用于了解全貌）\n"
                "以下是本次未直接引用进提示词的资料要点。它只能用于理解项目全貌与发现线索，"
                "**不得当作证据编号引用，也不得把其中的数字直接写进评分理由**；"
                "需要引用具体事实时，只能使用上方「可追溯证据」中列出的原文与编号。\n"
                + omitted_digest
            )
        if step_number == 7:
            full_human_prompt += (
                "\n先输出 JSON 代码块，再写正文：第一段就是 ```json 块，字段 scores "
                "**必须是以指标 ID 为键的对象**"
                "（形如 {\"L3_01_01_01\": {\"actual_score\": 4, \"evidence_id\": \"P1-12-1\", "
                "\"reason\": \"……\"}}），不是数组。每个三级指标都要有一条；"
                "仅为有项目资料及评分依据的指标赋分，evidence_id 必须引用本步骤的项目资料编号。"
                "没有依据时按评分规则第 3 条「未取得佐证」计分并在理由中写明缺什么，"
                "只有评分规则第 4 条的两种情形才写「待核实」、不输出数值。"
                "JSON 之后的正文不超过 1,200 字，每个 reason 不超过 60 字。"
            )
        if step_number == 11:
            full_human_prompt += "\n程序评分结果：" + json.dumps(state.get("score_summary", {}), ensure_ascii=False)
        if step_number == 7:
            # 通用口径里的「不能核实就写待核实」与本步的举证责任规则直接冲突，单列覆盖
            full_human_prompt += (
                "\n引用关键项目事实、金额、时间和扣分依据时保留对应证据编号。"
                "知识库条目只用于评价规则，不能证明项目实际发生的事实。"
                "本步骤的「不能核实」按评分规则第 3、4 条处置：未取得佐证按标准计分，"
                "只有评分规则第 4 条的两种情形才写「待核实」。"
                "保持项目名称与输入一致；对相互冲突的金额、时间列出两处来源并标注待核实。"
            )
        else:
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
                    return self._invoke_llm(messages, step_number, state)
                except Exception as e:
                    logger.warning(f"LLM invoke失败: {e}，回退到合并消息模式")
                    try:
                        combined = system_prompt + "\n\n" + full_human_prompt
                        return self._invoke_llm(combined, step_number, state)
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
                return self._invoke_llm(messages, step_number, state)
            except Exception as exc:
                raise RuntimeError("模型调用失败") from exc

        raise RuntimeError("模型未初始化")

    def _record_token(self, response, step_number: int, state: PerformanceState):
        """从LLM响应中提取并记录token消耗"""
        try:
            from backend.services.token_service import extract_token_usage_from_response
            self._record_usage(extract_token_usage_from_response(response), step_number, state)
        except Exception as e:
            logger.debug(f"Token记录失败(非关键): {e}")

    def _record_usage(self, usage: Dict[str, int], step_number: int, state: PerformanceState,
                      estimated: bool = False):
        """把一次调用的用量落库；流转发模式没有 usage 时标注 estimated 并跳过写入"""
        try:
            from backend.services.token_service import record_token_usage
            from backend.database import SessionLocal as SL
            from backend.models.user_models import WorkflowSession

            total = int(usage.get("total_tokens", 0) or 0)
            if total <= 0:
                return
            if estimated:
                logger.info(f"[Token] step={step_number} 用量为估算值（流式响应未返回 usage）")
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

    @staticmethod
    def _chunk_text(chunk) -> str:
        """兼容 content 为 str 或内容块列表两种形态"""
        content = getattr(chunk, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "".join(parts)
        return ""

    def _llm_for_step(self, step_number: int):
        """按步骤取模型实例：配了输出预算的步骤，绑一份带预算的副本。

        DeepSeek 必须走 `extra_body.max_tokens`——langchain 1.x 把 `max_tokens`
        发成 `max_completion_tokens`，DeepSeek 把它当未知字段丢掉（详见
        STEP_OUTPUT_BUDGETS 的注释）。绑不上就退回默认实例，不能让调用直接失败。
        """
        llm = self.base_agent.llm
        budget = step_output_budget(step_number)
        if not budget or llm is None:
            return llm
        kwargs = {"max_tokens": budget}
        model_name = getattr(self, "model_name", "") or getattr(self.base_agent, "model_name", "")
        if get_model_provider(model_name) == "deepseek":
            kwargs["extra_body"] = {"max_tokens": budget}
        try:
            return llm.bind(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"步骤 {step_number} 的输出预算绑定失败，沿用模型默认上限：{exc}")
            return llm

    def _note_truncation(self, state: PerformanceState, step_number: int,
                         finish_reason: Optional[str], chars: int) -> bool:
        """输出被长度上限砍断时记账：状态、告警、日志三处都要留痕。

        为什么必须记：改前这条信息只存在于「JSON 解析失败」这个次要症状里，
        看上去像模型不会写 JSON，实际是它根本没写完。运维与复核要能一眼分开。
        """
        if finish_reason not in TRUNCATION_FINISH_REASONS:
            return False
        try:
            steps = state.setdefault("truncated_steps", [])
            if step_number not in steps:
                steps.append(step_number)
            budget = step_output_budget(step_number)
            limit = f"{budget} token 预算" if budget else "模型单次输出上限"
            warning = (
                f"步骤 {step_number} 的输出被长度上限截断（{limit}，已生成 {chars} 字），"
                "末尾内容可能缺失，该步骤结论需人工复核是否完整"
            )
            warnings = state.setdefault("warnings", [])
            if warning not in warnings:
                warnings.append(warning)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"截断记账失败(非关键): {exc}")
        logger.warning(
            f"[Step {step_number}] 输出被长度上限截断：finish_reason={finish_reason}, "
            f"{limit}, {chars} 字"
        )
        return True

    def _invoke_llm(self, messages, step_number: int, state: PerformanceState,
                    timeout: int = 300, stream: Optional[bool] = None) -> str:
        """调用模型；有 SSE 消费者时走流式，逐块推送 token_delta 后拼成完整结果。

        stream=False 用于「修复重试」这类内部补调：不希望把第二份输出混进前端的逐字流。
        """
        from backend.services import progress_bus

        llm = self._llm_for_step(step_number)
        sid = getattr(self, "session_id", "")
        allow_stream = hasattr(llm, "stream") if stream is None else bool(stream)
        stream_enabled = bool(sid) and allow_stream and hasattr(llm, "stream") and progress_bus.has_consumer(sid)

        if stream_enabled:
            try:
                parts: List[str] = []
                usage: Dict[str, int] = {}
                finish_reason: Optional[str] = None
                for chunk in llm.stream(messages, config={"timeout": timeout}):
                    text = self._chunk_text(chunk)
                    if text:
                        parts.append(text)
                        progress_bus.push_delta(sid, step_number, text)
                    meta = getattr(chunk, "usage_metadata", None)
                    if meta:
                        usage = {
                            "prompt_tokens": int(meta.get("input_tokens", 0) or 0),
                            "completion_tokens": int(meta.get("output_tokens", 0) or 0),
                            "total_tokens": int(meta.get("total_tokens", 0) or 0),
                        }
                    reason = (getattr(chunk, "response_metadata", None) or {}).get("finish_reason")
                    if reason:
                        finish_reason = reason
                content = "".join(parts)
                if content.strip():
                    if usage.get("total_tokens"):
                        self._record_usage(usage, step_number, state)
                    else:
                        self._record_usage(
                            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            step_number, state, estimated=True,
                        )
                    self._note_truncation(state, step_number, finish_reason, len(content))
                    return content
                logger.warning(f"流式返回为空，回退非流式: step={step_number}")
            except Exception as e:
                logger.warning(f"流式调用失败，回退非流式: step={step_number}, {e}")

        response = llm.invoke(messages, config={"timeout": timeout})
        self._record_token(response, step_number, state)
        text = response.content if hasattr(response, "content") else str(response)
        meta = getattr(response, "response_metadata", None) or {}
        self._note_truncation(state, step_number, meta.get("finish_reason"), len(text or ""))
        return text

    # ─── 长文档覆盖度 ────────────────────────────────────

    # 单步提示词直接引用的原文预算。与原实现的 8000/12000 对齐，避免 token 成本突增；
    # 超出预算的部分不再「静默消失」，而是变成覆盖度声明 + 要点摘要。
    DOC_PREVIEW_BUDGET = 8000
    DOC_FULL_BUDGET = 12000
    # 这些步骤的模板只用 raw_text_preview（节选），覆盖度声明要按节选字数说，不能报满预算
    PREVIEW_ONLY_STEPS = frozenset({1})

    # ─── 分类内容（Step2 内容索引）注入预算 ────────────────
    # effective_data_categorized 是「段落原文按类别重排」，体积≈语料本身
    # （实测 9,925 字语料 → 10,415 字符 JSON）。步骤 3/7/9/12 都用它，
    # 原样注入等于把整份语料往四个提示词里各灌一遍，且与同提示词里的原始资料重复。
    # 因此按类别封顶，并把「没放进来多少」明确写进提示词。
    CATEGORY_BUDGET = 6000
    CATEGORY_ITEM_BUDGET = 1500
    # 预算有限时的取用顺序：先给能决定「项目是什么、钱从哪来、要达成什么」的事实
    CATEGORY_ORDER = ("项目概况", "资金管理", "绩效目标", "实施过程", "组织管理", "其他")
    # 这些步骤的模板已经给了 raw_text_full，再注一遍分类内容是同一批字的重复，
    # 实测（9,925 字语料）可省 4,948 字符/次，且减少「同一段话出现两次」的干扰。
    CATEGORY_SKIP_STEPS = frozenset({12})

    # ─── 步骤 7 评分落地的处置口径见模块级 DISPOSITION_LABELS ──

    def _categorized_excerpt(self, state: PerformanceState,
                             total: Optional[int] = None,
                             per_item: Optional[int] = None,
                             step_number: Optional[int] = None) -> str:
        """把 Step2 的分类内容压到预算内，并声明省略了多少。"""
        if step_number in self.CATEGORY_SKIP_STEPS:
            return "（本步骤已直接提供原始资料全文，为避免同一批文字重复出现，此处不再重复注入分类内容。）"
        budget = self.CATEGORY_BUDGET if total is None else total
        item_budget = self.CATEGORY_ITEM_BUDGET if per_item is None else per_item
        categorized = state.get("effective_data_categorized") or {}
        if not isinstance(categorized, dict) or not categorized:
            return ("（步骤 2 未产出分类内容：未提供可解析的原始资料，或内容索引未建立。"
                    "本步骤不得据此推断任何项目事实，缺什么就写「资料不足」。）")

        order = list(self.CATEGORY_ORDER) + [
            name for name in categorized if name not in self.CATEGORY_ORDER
        ]
        blocks: List[str] = []
        used = 0
        dropped_categories = 0
        dropped_chars = 0
        for name in order:
            text = (categorized.get(name) or "").strip()
            if not text:
                continue
            room = min(item_budget, budget - used)
            if room <= 0:
                dropped_categories += 1
                dropped_chars += len(text)
                continue
            shown = text[:room]
            used += len(shown)
            if len(shown) < len(text):
                dropped_chars += len(text) - len(shown)
                blocks.append(
                    f"### {name}（共 {len(text)} 字，此处展示前 {len(shown)} 字，"
                    f"其余 {len(text) - len(shown)} 字未放入本步骤）\n{shown}"
                )
            else:
                blocks.append(f"### {name}（共 {len(text)} 字，全文）\n{shown}")

        if dropped_categories or dropped_chars:
            blocks.append(
                f"—— 另有 {dropped_categories} 个类别、共 {dropped_chars} 字未放入本步骤"
                f"（受本步骤分类资料预算 {budget} 字限制；完整分类内容见步骤 2 产出，"
                "未看到的部分不要猜测）"
            )
        return "\n\n".join(blocks)

    def _step7_material(self, state: PerformanceState) -> str:
        """步骤 7 提示词里**真实下发**的材料，供引用锚点校验使用。

        必须与 `default_prompts.STEP7_HUMAN_TEMPLATE` 的三项变量同源：
        evaluation_table_markdown / indicator_system_full_json / effective_data_categorized。

        为什么不能用 `step_evidence["7"]` 单独当校验依据：那个池子只有本步骤检索到的
        几个 chunk（实测 3 条），而提示词里同时外露着 evaluation_table 内的编号
        （P1-2-1 / P1-11-1 / P1-14-1 等）与原文小节标题。模型只看得见这整份材料，
        校验就必须对着这整份材料做，否则会把真实评分判成「证据无效」。
        """
        return "\n".join([
            state.get("evaluation_table") or "",
            json.dumps(state.get("indicator_system") or {}, ensure_ascii=False),
            self._categorized_excerpt(state, step_number=7),
        ])

    def _step7_source_text(self, state: PerformanceState) -> str:
        """只取「项目资料原文」部分，用于抽小节标题（`一、项目概况` 这一层）。

        为什么不能从 `_step7_material` 里抽标题：那是 evaluation_table +
        指标体系 JSON + 分类原文的拼接，里面混着**步骤 6 自己生成的** Markdown 标题
        （实测抽出过 `分值校验`、`资料缺口提示`、`结构化JSON数据供导出WordPDF`）。
        模型若引用 `资料「分值校验」`，会被判成合法锚点——等于「自己引自己上一步的产物」，
        与「引用项目资料」不是一回事。

        所以标题与序号只从分类原文抽；材料全文仍用于 `material_id` 校验（编号确实下发过）。
        """
        return self._categorized_excerpt(state, step_number=7)

    def _score_disposition_summary(self, items: Dict[str, Dict[str, Any]],
                                   unmatched: Optional[List[Dict[str, Any]]] = None
                                   ) -> Dict[str, Any]:
        """把逐指标的处置结果汇总成可展示的台账（实现见模块级 summarize_disposition）。"""
        return summarize_disposition(items, unmatched)

    def _document_coverage(self, state: PerformanceState,
                           step_number: int = 0) -> doc_coverage.CoveragePlan:
        """整份资料切块 → 按预算取头尾 → 记录未引用部分（同一会话只算一次）。"""
        cached = getattr(self, "_coverage_plan", None)
        if cached is not None:
            return cached

        plan = doc_coverage.build_plan(
            state.get("all_raw_texts") or {},
            state.get("raw_text") or "",
            budget_chars=self.DOC_FULL_BUDGET,
        )
        if plan.omitted_chars >= doc_coverage.DIGEST_MIN_CHARS:
            plan.digest = self._digest_omitted(state, plan, step_number)

        state["document_coverage"] = plan.to_dict()
        if plan.omitted_chunks:
            state.setdefault("warnings", []).append(
                f"资料共 {plan.total_chars} 字，提示词直接引用 {plan.used_chars} 字"
                f"（{plan.used_ratio * 100:.1f}%），未直接引用 {plan.omitted_chars} 字"
                + ("，已压缩为要点摘要" if plan.digest else "，未做摘要")
            )
        self._coverage_plan = plan
        return plan

    def _digest_omitted(self, state: PerformanceState, plan: doc_coverage.CoveragePlan,
                        step_number: int) -> str:
        """把未直接引用的资料分批压成要点（map-reduce），失败不影响主流程。"""
        if not getattr(self, "base_agent", None):
            return ""
        batches = doc_coverage.digest_batches(plan)
        parts: List[str] = []
        covered = 0
        for batch in batches:
            try:
                reply = self._invoke_llm(
                    doc_coverage.digest_messages(batch), step_number, state,
                    timeout=120, stream=False,
                )
            except Exception as exc:
                logger.warning(f"[覆盖度] 未引用资料压缩失败：{exc}")
                break
            if isinstance(reply, str) and reply.strip():
                parts.append(reply)
                covered += len(batch)
        plan.digest_chars_covered = covered
        digest = doc_coverage.reduce_digest(parts)
        if digest:
            logger.info(f"[覆盖度] 已把 {covered} 字未引用资料压缩为 {len(digest)} 字要点")
        return digest

    def _build_human_vars(self, state: PerformanceState, step_number: int) -> Dict[str, Any]:
        """为Human Prompt模板构建变量"""
        coverage = self._document_coverage(state, step_number)
        preview_chars = min(coverage.used_chars, self.DOC_PREVIEW_BUDGET)
        delivered = preview_chars if step_number in self.PREVIEW_ONLY_STEPS else None
        v = {
            "project_name": state.get("project_name", "未命名项目"),
            "project_type": state.get("project_type", "部门评价"),
            "raw_text_preview": coverage.text(self.DOC_PREVIEW_BUDGET),
            "raw_text_full": coverage.text(),
            "document_coverage": coverage.statement(delivered_chars=delivered),
            "omitted_digest": coverage.digest,
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
        v["effective_data_categorized"] = self._categorized_excerpt(state, step_number=step_number)
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

    def _record_step_result(self, state: PerformanceState, step_number: int, content: str,
                            structured: Any = None):
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
        # 结构化输出的判定结果（仅步骤 3/4/7 有），供状态接口与前端展示
        if structured is not None:
            status = getattr(structured, "status", None)
            if status:
                state["step_results"][step_number]["structured_status"] = status
            # 修复过就把首次正文单独留档：content 已被替换成修复版本，
            # 不留这一份就再也查不到「模型第一次到底写了什么、为什么被修掉」。
            original = getattr(structured, "original", "") or ""
            if original and original != content:
                state["step_results"][step_number]["raw_content"] = original
            if status == "manual_review":
                state["step_results"][step_number]["structured_errors"] = list(
                    getattr(structured, "errors", [])[:5]
                )

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
            from backend.services import progress_bus
            sid = state.get("session_id", "")
            p = progress_bus.get_progress(sid) if sid else None
            if p is not None:
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
                "document_coverage", "consistency",
                # 步骤 3/7/9 提示词的唯一资料入口；不落盘则重启续跑后这几步又看不到资料
                "effective_data_categorized",
                # 步骤 7 的逐指标处置台账：不含它，重启后「23 项里为什么只落地 7 项」就查不到了
                "score_disposition",
                # 步骤 7 的输出长度画像 + 被截断的步骤：不含它们，重启后只能看到
                # 「JSON 解析失败」这个症状，看不到「输出被砍断」这个原因（批次 20）
                "step7_length", "truncated_steps",
                # 弱定位的行号校验依据。只存行数（几十字节），不存全文；
                # 不含它则「重启/重放后同一份输入判出不同结果」，验收链条断裂
                "raw_line_counts",
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
            fd, temp_name = tempfile.mkstemp(dir=str(STATE_BACKUP_DIR), suffix=".tmp")
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as target:
                    json.dump(backup, target, ensure_ascii=False, indent=2)
                _publish_backup(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def delete_state_backup(session_id: str) -> bool:
        """删除某个session的磁盘备份，返回是否真的删掉了文件。

        与写入共用一把锁，避免「刚判断存在就被别人替换」的窗口。
        """
        with _STATE_BACKUP_LOCK:
            return _discard_backup(STATE_BACKUP_DIR / f"{session_id}.json")

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
                # 只存「文件名 → 非空行数」的轻量投影（几十字节），用于步骤 7 的弱定位
                # 行号校验。不存全文：all_raw_texts 可能有几十万字，进备份会把文件撑大。
                from backend.services import evidence_anchor as _anchor
                state["raw_line_counts"] = _anchor.doc_line_counts(extracted)
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
        content, outcome = self._execute_step_structured(state, 3)
        isys = outcome.payload if outcome.ok else None
        issue = indicator_tree_error(isys) if outcome.ok else outcome.summary()
        if issue:
            state.setdefault("warnings", []).append("步骤 3 待人工复核：" + issue)
            content = "资料不足或指标格式异常，待人工复核：" + issue + "\n\n" + content
        else:
            isys["indicator_type"] = itype
            state["indicator_system"] = isys
            # 只记账、不拦树：命中不代表这份体系错了，只代表标准字段里混进了结论。
            purity = audit_standard_purity(isys)
            state["standard_purity"] = purity
            if purity["with_verdict"]:
                logger.warning(
                    f"[Step 3] {purity['with_verdict']}/{purity['leaves']} 条叶子指标的"
                    f"scoring_standard 夹带了资料核对结论：{purity['ids'][:5]}"
                )
        self._record_step_result(state, 3, content, structured=outcome)
        return state

    # Step 4
    def _node_step4(self, state: PerformanceState) -> PerformanceState:
        logger.info("[Step 4] 生成指标分值")
        content, outcome = self._execute_step_structured(state, 4)
        import copy
        system = state.get("indicator_system") or {}
        scores = {}
        if outcome.ok and isinstance(outcome.payload, dict):
            parsed = outcome.payload
            scores = parsed.get("scores", parsed) if isinstance(parsed, dict) else {}
        elif not outcome.ok:
            state.setdefault("warnings", []).append(
                "步骤 4 模型输出不是可用分值 JSON：" + outcome.summary()
            )
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
        self._record_step_result(state, 4, content, structured=outcome)
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
        if state.get("indicator_system"):
            content, outcome = self._execute_step_structured(state, 7)
        else:
            content, outcome = "资料不足，指标体系待复核后方可进行评分。", None
        from backend.agents.tools.questionnaire_gen import indicator_leaves
        from backend.services import evidence_anchor

        ids = {item["id"]: item for item in indicator_leaves(
            state.get("indicator_system", {}).get("level1_indicators", [])
        )}
        # 编号与定位串必须同源：都只认本步骤真正下发给模型的 project 检索块。
        # 否则会出现「编号不认识、定位串却匹配到一个模型没看到的块」。
        hits = [hit for hit in (state.get("step_evidence", {}).get("7") or [])
                if isinstance(hit, dict) and hit.get("kind") == "project"]
        evidence_ids = {hit.get("id") for hit in hits if hit.get("id")}
        material = self._step7_material(state)
        # 标题/序号只从原文抽，避免把步骤 6 自己生成的表格标题当锚点（见 _step7_source_text）
        source_text = self._step7_source_text(state)
        headings = evidence_anchor.material_headings(source_text)
        ordinals = evidence_anchor.section_ordinals(source_text)
        chunks = hits
        # 弱定位所需的原文行数：线上用 all_raw_texts，重放用备份里的 raw_line_counts。
        # 两处走同一个判定函数，避免「线上能落地、重放判拒」的口径分裂。
        raw_texts = state.get("all_raw_texts") or {}
        line_counts = state.get("raw_line_counts") or {}
        scores = dict(state.get("indicator_score_map", {}))
        candidates = {}
        if outcome is not None:
            if outcome.ok and isinstance(outcome.payload, dict):
                candidates = outcome.payload.get("scores", {})
            elif not outcome.ok:
                state.setdefault("warnings", []).append(
                    "步骤 7 模型输出不是可用评分 JSON：" + outcome.summary()
                )
        if not isinstance(candidates, dict):
            candidates = {}

        # 模型给了分、但指标 id 不在体系内的条目：实测真实一轮里 6 条评分有 4 条是这种。
        # 不记下来就等于「模型编了 id」和「模型没提这个指标」在对外口径里长得一模一样。
        unmatched: List[Dict[str, Any]] = []
        for key, item in candidates.items():
            if key in ids:
                continue
            unmatched.append({
                "indicator_id": key,
                "actual_score": item.get("actual_score") if isinstance(item, dict) else None,
                "evidence": item.get("evidence_id") if isinstance(item, dict) else None,
            })

        items: Dict[str, Dict[str, Any]] = {}
        for key, indicator in ids.items():
            existing = scores.get(key)
            if key in scores and isinstance(existing, dict):
                items[key] = {"status": "accepted",
                              "actual_score": score_number(existing.get("actual_score")),
                              "evidence": existing.get("evidence_id"),
                              "evidence_anchor": existing.get("evidence_anchor"),
                              "note": "沿用已有评分"}
                continue
            if key not in candidates:
                items[key] = {"status": "not_mentioned"}
                continue
            item = candidates[key]
            if not isinstance(item, dict):
                # 提了这个指标但条目不是对象：与「没提」不同，不能混成同一个口径
                items[key] = {"status": "score_invalid", "note": "条目不是对象"}
                continue
            evidence = item.get("evidence_id")
            reason = item.get("reason")
            reason_text = reason if isinstance(reason, str) else ""
            anchor_from = "evidence_id"
            if not (isinstance(evidence, str) and evidence.strip()):
                # 实测：模型会把出处写进 reason（`（fe_wf_sample.txt「四、资金安排」）`）
                # 而把 evidence_id 留成 null。引用确实存在，只是放错字段——按字段形式
                # 判死等于又把真实评分丢掉一次。锚点仍在材料里校验，标准不放宽。
                evidence, anchor_from = reason_text, "reason"
            raw_value = item.get("actual_score")
            if "actual_score" not in item:
                # 列了这个指标却没写分值字段：结构缺字段，值得报，与「写 null 拒绝」不同
                items[key] = {"status": "score_invalid", "note": "缺少 actual_score 字段",
                              "evidence": evidence if isinstance(evidence, str) else None}
                continue
            if evidence_anchor.classify_score(raw_value) == "pending":
                # 模型按提示词要求明确拒绝赋分（写「待核实」或 JSON null），
                # 不是格式错误，也不能算「系统丢了评分」
                items[key] = {"status": "model_pending",
                              "actual_score": "待核实" if raw_value is None else str(raw_value).strip(),
                              "evidence": evidence if isinstance(evidence, str) and evidence.strip() else None}
                continue
            value = actual_indicator_score(indicator, {key: item})
            if value is None:
                items[key] = {"status": "score_invalid", "actual_score": raw_value,
                              "evidence": evidence if isinstance(evidence, str) else None}
                continue
            anchor = evidence_anchor.resolve(evidence, evidence_ids=evidence_ids,
                                             material=material, headings=headings,
                                             chunks=chunks, ordinals=ordinals,
                                             raw_texts=raw_texts, line_counts=line_counts)
            if not anchor.ok:
                items[key] = {"status": ANCHOR_STATUS_BY_REASON.get(anchor.reason, "anchor_unresolved"),
                              "evidence": evidence if isinstance(evidence, str) else None,
                              "anchor_from": anchor_from,
                              "anchor_reason": anchor.reason,
                              "cited_ids": list(anchor.ids)}
                continue
            if not reason_text.strip():
                items[key] = {"status": "reason_missing", "actual_score": value,
                              "evidence": evidence}
                continue
            if anchor.kind == "material_locator":
                # 引用落到了原文真实存在的行号，但那一行没进本步骤检索证据。
                # 只证明「这一行存在」，不证明「这一行支持该评分」——所以**不写进
                # indicator_score_map**，也就不进 total_score。单列出来让人工确认，
                # 否则「无法评价、暂按基础分」那几条会借这个口子把总分抬上去。
                items[key] = {"status": "anchor_weak", "actual_score": value,
                              "evidence": evidence, "anchor_kind": anchor.kind,
                              "anchor_from": anchor_from,
                              "evidence_anchor": {**anchor.as_dict(), "from": anchor_from},
                              "note": "行号在原文范围内，但该行未进入本步骤检索证据"}
                continue
            scores[key] = {"actual_score": value, "evidence_id": evidence,
                           "evidence_anchor": {**anchor.as_dict(), "from": anchor_from},
                           "score_reason": reason_text,
                           "full_score": score_number(indicator.get("score")),
                           "source": "project_evidence"}
            items[key] = {"status": "accepted", "actual_score": value,
                          "evidence": evidence, "anchor_kind": anchor.kind,
                          "anchor_from": anchor_from,
                          # 「按未取得佐证计分」和「实测表现得分」在分数上长得一样，
                          # 但对报告结论的含义完全相反：前者必须写进评价局限。
                          # 靠模型在 reason 里的固定前缀识别，不改任何既有字段语义。
                          "no_evidence": reason_text.startswith(NO_EVIDENCE_MARKER),
                          # 与 refresh_disposition 写入的字段同名同形：否则人工补分前后
                          # 台账字段集合不同，前端读 matched 会时有时无
                          "evidence_anchor": {**anchor.as_dict(), "from": anchor_from}}

        state["indicator_score_map"] = scores
        disposition = self._score_disposition_summary(items, unmatched)
        state["score_disposition"] = disposition
        line = disposition.get("summary_line") or ""
        incomplete = disposition.get("counts", {}).get("accepted", 0) < disposition.get("total", 0)
        if line and (incomplete or unmatched):
            content = line + "\n\n" + content
            warnings = state.setdefault("warnings", [])
            if line not in warnings:
                warnings.append(line)
        state["indicator_analysis"] = content
        # 输出长度画像：JSON 是否在最前、正文多长、理由有没有超字数。
        # 只统计不改写——评分能不能落地由上面的锚点校验决定，这里只回答「为什么」。
        length = structured_output.audit_step7_length(content)
        state["step7_length"] = length.as_dict()
        if not length.json_first and length.json_parsed:
            logger.info(f"[Step 7] 输出未按「先 JSON 后正文」：{length.summary()}")
        self._record_step_result(state, 7, content, structured=outcome)
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
        # 报告成型后立刻做一次纯程序的步骤间一致性检查（不调模型）
        self._run_consistency_check(state)
        logger.info("[Step 14] 报告生成完成")
        return state

    def _run_consistency_check(self, state: PerformanceState) -> dict:
        """跑一致性检查并写入 state["consistency"]；有结论则并入 warnings。

        检查只报「两处数字确实对不上」，不改写任何产出物——报告里原本写什么就还是什么，
        判断权交给人。
        """
        from backend.services import consistency

        report = consistency.check_consistency(state)
        state["consistency"] = report
        line = consistency.summary_line(report)
        if line:
            warnings = state.setdefault("warnings", [])
            if line not in warnings:
                warnings.append(line)
            logger.warning(f"[Step 14] {line}")
        else:
            logger.info("[Step 14] 步骤间一致性检查通过")
        return report

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
        return structured_output.extract_json(text)

    # ─── 结构化输出（形状校验 + 一次修复重试）─────────────

    def _execute_step_structured(self, state: PerformanceState, step_number: int):
        """执行需要 JSON 的步骤：拿文本 → 校验形状 → 必要时修复一次。

        返回 (content, outcome)。outcome.status 为 ok / repaired / manual_review；
        content 永远是模型的**原始文本**（修复后则替换为修复输出），不做静默改写。
        """
        content = self._execute_step(state, step_number)
        outcome = structured_output.parse(step_number, content)
        if outcome.ok:
            structured_output.log_outcome(outcome)
            self._note_structured_warnings(state, step_number, outcome)
            return content, outcome

        logger.warning(f"[Step {step_number}] 结构化输出不合规，尝试一次修复：{outcome.summary()}")
        repaired = self._repair_structured_output(state, step_number, content, outcome)
        if repaired:
            second = structured_output.parse(step_number, repaired)
            report = structured_output.shrink_report(step_number, content, second)
            if second.ok and report.shrunk:
                # 修复「通过」了，但少给了东西——这不是修好了，是修少了。
                # 实测：20 个叶子被修成 5 条，原始 6,000+ 字正文还被替换掉；
                # 另一种更隐蔽：条目一条没少，5 个真实分值全变成「待核实」。
                # 宁可交人工复核并保留原文，也不接受缩水版本。
                note = f"修复结果{report.reason}（修复缩水），不采用修复版本，保留原文待人工复核"
                outcome.errors = [note, *list(outcome.errors or [])]
                state.setdefault("warnings", []).append(f"步骤 {step_number} {note}")
                structured_output.log_outcome(outcome)
                return content, outcome
            if second.ok:
                if report.unverifiable:
                    # 判不了不等于没问题：照常采用，但必须留下可查的痕迹。
                    logger.warning(f"[Step {step_number}] {report.reason}")
                    state.setdefault("warnings", []).append(
                        f"步骤 {step_number} 已采用修复结果，但{report.reason}，请人工复核条目是否齐全"
                    )
                second.status = "repaired"
                second.original = content
                structured_output.log_outcome(second)
                self._note_structured_warnings(state, step_number, second)
                return repaired, second
            outcome.errors = second.errors or outcome.errors
        structured_output.log_outcome(outcome)
        return content, outcome

    def _repair_structured_output(self, state: PerformanceState, step_number: int,
                                 content: str, outcome) -> Optional[str]:
        if not getattr(self, "base_agent", None):
            return None
        try:
            # 修复是内部补调：不流式（避免两份输出混在逐字流里），但要让等待的用户知道在重做
            from backend.services import progress_bus

            sid = getattr(self, "session_id", "")
            if sid and progress_bus.has_consumer(sid):
                progress_bus.push_delta(sid, step_number, "\n\n（输出未通过格式校验，正在重新生成结构化结果…）\n")
            messages = structured_output.repair_messages(step_number, content, outcome.errors)
            repaired = self._invoke_llm(messages, step_number, state, timeout=180, stream=False)
            return repaired if isinstance(repaired, str) and repaired.strip() else None
        except Exception as exc:
            logger.warning(f"[Step {step_number}] 结构化修复调用失败：{exc}")
            return None

    def _note_structured_warnings(self, state: PerformanceState, step_number: int, outcome) -> None:
        """把被忽略的条目记进 warnings，让人知道模型输出里有多少条没进结构化结果。"""
        if not outcome.warnings:
            return
        preview = "；".join(outcome.warnings[:3])
        more = f" 等 {len(outcome.warnings)} 条" if len(outcome.warnings) > 3 else ""
        state.setdefault("warnings", []).append(
            f"步骤 {step_number} 有部分输出未进入结构化结果：{preview}{more}"
        )


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
