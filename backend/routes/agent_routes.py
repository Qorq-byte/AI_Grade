"""
Agent相关API路由
提供与前端交互的接口 — SSE流式 / 状态查询 / 人工确认 / 导出 / 问卷
"""
import hashlib
import re
import json
import asyncio
import contextvars
import os
import time
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from fastapi.responses import StreamingResponse, FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from loguru import logger
from langgraph.types import Command

from langchain_openai import ChatOpenAI

from backend.agents.base_agent import BaseAgent
from backend.agents import performance_agent
from backend.agents.performance_agent import PerformanceAgent
from backend.agents.tools.export_service import ExportService, ExportStyle, export_report
from backend.agents.tools.score_validator import actual_indicator_score, indicator_tree_error, score_number
from backend.agents.tools import structured_output
from backend.services.workflow_evidence import report_evidence_index
from backend.services import export_cache
from backend.agents.tools.questionnaire_gen import QuestionnaireGenerator, generate_default_questionnaire, indicator_leaves
from backend.agents.prompts.default_prompts import STEP_META
from backend.utils.llm_helper import get_supported_models
from backend.database import get_db
from backend.middleware.auth_middleware import get_current_user
from backend.models.memory_models import UserSession
from backend.models.user_models import User, UserRole, WorkflowSession
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.config import settings

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

# 上传文件存储目录
UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()

# 全局Agent实例字典（生产环境应使用redis等共享存储）
active_agents: Dict[str, BaseAgent] = {}
active_workflows: Dict[str, PerformanceAgent] = {}

# 存储每个session的初始化参数，用于approve后重建
active_workflow_configs: Dict[str, dict] = {}

def _validate_session_id(session_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        raise HTTPException(status_code=400, detail="无效的会话 ID")


def _owned_workflows(db: Session, user: User):
    query = db.query(WorkflowSession)
    if user.role != UserRole.ADMIN:
        query = query.filter(WorkflowSession.user_id == user.id)
    return query


def _require_workflow(session_id: str, user: User, db: Session) -> None:
    _validate_session_id(session_id)
    row = _owned_workflows(db, user).with_entities(WorkflowSession.session_id).filter(
        WorkflowSession.session_id == session_id
    ).first()
    if row is not None and row.session_id == session_id:
        return
    if user.role == UserRole.ADMIN and row is None and PerformanceAgent.load_state_backup(session_id):
        return
    raise HTTPException(status_code=404, detail="工作流不存在")


def _require_path_workflow(session_id: str, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    _require_workflow(session_id, user, db)


async def _require_body_workflow(request: Request, user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    # FastAPI validates malformed request bodies against the endpoint's model.
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return
    session_id = body.get("session_id") if isinstance(body, dict) else None
    if isinstance(session_id, str):
        _require_workflow(session_id, user, db)


def _require_chat_session(session_id: str, user: User, db: Session) -> None:
    _validate_session_id(session_id)
    row = db.query(UserSession).filter(UserSession.session_id == session_id).first()
    if row is None or row.session_id != session_id or (
        user.role != UserRole.ADMIN and row.user_id != str(user.id)
    ):
        raise HTTPException(status_code=404, detail="会话不存在")


# ─── 请求模型 ───

class AgentRequest(BaseModel):
    """Agent请求基础模型"""
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    use_memory: bool = True


class AgentResponse(BaseModel):
    """Agent响应基础模型"""
    success: bool
    data: Optional[Any] = None
    message: str = ""


class WorkflowRequest(BaseModel):
    """工作流请求模型"""
    session_id: Optional[str] = None
    project_name: str
    file_paths: List[str] = []
    model_name: Optional[str] = None  # None 时使用 config.DEFAULT_MODEL
    use_rag: bool = False
    require_approval: bool = False
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    comparison_mode: Optional[str] = "single"
    temperature_profile: Optional[str] = "normal"
    language_style: Optional[str] = "中立"
    indicator_type: Optional[str] = "项目支出指标体系"  # 指标体系类型（6种可选）


class ApproveRequest(BaseModel):
    """人工确认请求"""
    session_id: str
    approved: bool = True
    feedback: Optional[str] = None
    background: bool = False
    approval_step: int | None = None


class ExportRequest(BaseModel):
    """导出请求"""
    session_id: str
    format: str = "word"
    style_mode: str = "classic"
    project_name: Optional[str] = None
    step_number: Optional[int] = None
    step_content: Optional[str] = None
    #: 用户已知报告有缺章仍要导出时置 True（导出闸门放行，但记录一条日志）
    force: bool = False


class QuestionnaireRequest(BaseModel):
    """问卷生成请求"""
    session_id: str
    max_questions: int = Field(default=30, ge=1, le=100)
    custom_indicator_ids: Optional[List[str]] = None


@router.post("/chat", response_model=AgentResponse)
async def chat_with_agent(request: AgentRequest, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """
    与Agent对话接口
    支持长短期记忆、多模型切换
    """
    try:
        logger.info(f"收到对话请求: session_id={request.session_id}")
        
        # 获取或创建Agent实例
        session_id = request.session_id
        if session_id:
            _require_chat_session(session_id, user, db)
        if session_id and session_id in active_agents:
            agent = active_agents[session_id]
            # 如果需要切换模型
            if request.model and request.model != agent.model_name:
                agent.switch_model(
                    model_name=request.model,
                    temperature=request.temperature,
                    top_p=request.top_p
                )
        else:
            # 创建新Agent
            agent = BaseAgent(
                session_id=session_id,
                model_name=request.model,
                temperature=request.temperature,
                top_p=request.top_p,
                user_id=str(user.id),
            )
            session_id = agent.session_id
            active_agents[session_id] = agent
        
        # 调用Agent对话
        result = agent.chat(
            message=request.message,
            use_memory=request.use_memory
        )
        
        return AgentResponse(
            success=result.get("success", False),
            data=result,
            message="对话成功" if result.get("success") else "对话失败"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对话失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


class RefineStepRequest(BaseModel):
    session_id: str
    step_number: int
    message: str
    layout_mode: str = "classic"
    language_style: str = "neutral"
    indicator_type: str = "项目支出指标体系"


STEP_CONTENT_FIELDS = {
    1: "cost_list", 2: "effective_data", 5: "scoring_standards", 6: "evaluation_table",
    7: "indicator_analysis", 8: "experience_summary", 9: "problems_analysis", 10: "suggestions",
    11: "comprehensive_evaluation", 12: "basic_info", 13: "work_introduction",
}
STEP_DEPENDENTS = {
    1: range(2, 15), 2: range(3, 15), 3: range(4, 15), 4: range(5, 15),
    5: range(6, 15), 6: (7, 8, 9, 10, 11, 14), 7: (8, 9, 10, 11, 14),
    8: (14,), 9: (10, 11, 14), 10: (11, 14), 11: (14,), 12: (14,), 13: (14,),
}


def _review_after(values, step):
    completed = {int(key) for key in values.get("step_results", {})}
    return sorted((set(values.get("review_steps", [])) - {step})
                  | (completed & set(STEP_DEPENDENTS.get(step, ()))))


def _indicator_change(values, replacement):
    changes = {"indicator_system": replacement}
    previous = values.get("indicator_system") or {}
    scores = values.get("indicator_score_map") or {}
    if previous != replacement and scores:
        changes["indicator_score_history"] = [
            *values.get("indicator_score_history", []),
            {"indicator_system": previous, "scores": scores},
        ]
        changes["indicator_score_map"] = {}
        changes["score_table"] = ""
    return changes


def _apply_step_edit(session_id, step, content, source, expected_version=None):
    if session_id in _WORKFLOW_PROGRESS:
        raise HTTPException(status_code=409, detail="工作流执行中，暂不能修改步骤")
    values = _get_workflow_values(session_id)
    results = dict(values.get("step_results", {}))
    previous = results.get(step)
    if not previous or step not in {*STEP_CONTENT_FIELDS, 3, 4}:
        raise HTTPException(status_code=404, detail="找不到可修改的已完成步骤")
    version = previous.get("version", 1)
    if expected_version is not None and version != expected_version:
        raise HTTPException(status_code=409, detail="步骤已被修改，请刷新后重试")
    if not content.strip():
        raise HTTPException(status_code=422, detail="步骤内容不能为空")

    if step in (3, 4):
        try:
            parsed = json.loads(content)
        except ValueError:
            raise HTTPException(status_code=422, detail="结构化步骤需要有效的 JSON 对象")
        if step == 3:
            issue = indicator_tree_error(parsed)
            if issue:
                raise HTTPException(status_code=422, detail=issue)
            parsed["indicator_type"] = values.get("indicator_type") or parsed.get("indicator_type")
            updates = _indicator_change(values, parsed)
        else:
            raise HTTPException(status_code=422, detail="请修改步骤 3 的指标体系来调整指标分配，或使用逐指标评分")
    else:
        updates = {STEP_CONTENT_FIELDS[step]: {"raw": content} if step == 5 else content}
        if step == 7:
            # 报告第四章取的是 step7_prose：人工改完必须跟着重算，
            # 否则报告里还是修改前那段正文（批次 22）
            scores_json, prose = structured_output.split_step7_output(content)
            updates["step7_scores_json"] = scores_json
            updates["step7_prose"] = prose

    prior_revision = {"version": version, "content": previous.get("content", ""),
                      "edited_by": previous.get("edited_by", "workflow")}
    results[step] = {**previous, "content": content, "version": version + 1, "edited_by": source,
                     "updated_at": datetime.now(timezone.utc).isoformat(),
                     "revisions": [*previous.get("revisions", []), prior_revision]}
    review = _review_after(values, step)
    _persist_state(session_id, {**updates, "step_results": results, "review_steps": review})
    return {"session_id": session_id, "step_number": step, "refined_content": content,
            "version": version + 1, "review_steps": review}


class StepEditRequest(BaseModel):
    content: str
    version: int | None = None


@router.put("/workflow/{session_id}/steps/{step}", dependencies=[Depends(_require_path_workflow)])
async def edit_workflow_step(session_id: str, step: int, request: StepEditRequest):
    return {"success": True, "data": _apply_step_edit(session_id, step, request.content, "manual", request.version)}


@router.post("/workflow/{session_id}/steps/{step}/review", dependencies=[Depends(_require_path_workflow)])
async def review_workflow_step(session_id: str, step: int, user: User = Depends(get_current_user)):
    if session_id in _WORKFLOW_PROGRESS:
        raise HTTPException(status_code=409, detail="工作流执行中")
    values = _get_workflow_values(session_id)
    review = list(values.get("review_steps", []))
    if step not in review:
        raise HTTPException(status_code=409, detail="该步骤当前无需复核")
    review.remove(step)
    approvals = dict(values.get("approvals", {}))
    approvals[f"review_{step}"] = {"user_id": user.id, "version": values["step_results"][step].get("version", 1),
                                   "reviewed_at": datetime.now(timezone.utc).isoformat()}
    _persist_state(session_id, {"review_steps": review, "approvals": approvals})
    return {"success": True, "data": {"review_steps": review}}


@router.post("/workflow/refine", dependencies=[Depends(_require_body_workflow)])
async def refine_step_content(req: RefineStepRequest):
    session_id, step = req.session_id, req.step_number
    if session_id in _WORKFLOW_PROGRESS:
        raise HTTPException(status_code=409, detail="工作流执行中，暂不能修改步骤")
    if step == 4:
        raise HTTPException(status_code=422, detail="请修改步骤 3 的指标体系来调整指标分配，或使用逐指标评分")
    values = _get_workflow_values(session_id)
    current = values.get("step_results", {}).get(step)
    if not current or step == 14:
        raise HTTPException(status_code=404, detail="找不到可优化的已完成步骤")
    cfg = active_workflow_configs.get(session_id, {})
    model_name = cfg.get("model_name") or settings.DEFAULT_MODEL
    from backend.utils.llm_helper import get_model_credentials
    creds = get_model_credentials(model_name)
    model = ChatOpenAI(model=model_name, api_key=creds["api_key"], base_url=creds["base_url"], temperature=0.3)
    prompt = (
        f"依据证据及人工要求修订步骤 {step}，保留来源编号。无法证实的事实标注待核实。\n"
        f"{current.get('content', '')[:8000]}\n修订要求：{req.message}\n"
        + ("只输出结构化指标 JSON 对象。" if step == 3 else "输出修订后的完整内容。")
    )
    response = await run_in_threadpool(model.invoke, prompt)
    content = response.content if hasattr(response, "content") else str(response)
    if step == 3 and content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = _apply_step_edit(session_id, step, content, "model_refine", current.get("version", 1))
    return {"success": True, "data": data, "message": "内容已优化"}


@router.get("/status", response_model=AgentResponse, dependencies=[Depends(get_current_user)])
async def get_agent_status():
    """
    获取Agent状态
    """
    try:
        return AgentResponse(
            success=True,
            data={
                "status": "initialized",
                "langgraph": "ready",
                "rag": "ready",
                "langsmith": "connected",
                "database": "mysql",
                "memory_system": "long_short_term",
                "active_sessions": len(active_agents)
            },
            message="Agent状态正常"
        )
    except Exception as e:
        logger.error(f"获取状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/stats", response_model=AgentResponse)
async def get_session_stats(session_id: str, user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """
    获取会话记忆统计
    """
    _require_chat_session(session_id, user, db)
    try:
        if session_id not in active_agents:
            raise HTTPException(status_code=404, detail="会话不存在")
        
        agent = active_agents[session_id]
        stats = agent.get_memory_stats()
        
        return AgentResponse(
            success=True,
            data=stats,
            message="获取统计成功"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取统计失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}", response_model=AgentResponse)
async def close_session(session_id: str, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """
    关闭会话
    """
    if session_id in active_workflows or session_id in active_workflow_configs:
        _require_workflow(session_id, user, db)
        if session_id in _WORKFLOW_PROGRESS:
            raise HTTPException(status_code=409, detail="工作流正在执行")
    else:
        _require_chat_session(session_id, user, db)
    try:
        if session_id in active_agents:
            active_agents[session_id].close()
            del active_agents[session_id]
        if session_id in active_workflows:
            active_workflows[session_id].close()
            del active_workflows[session_id]
        if session_id in active_workflow_configs:
            del active_workflow_configs[session_id]
        logger.info(f"会话已关闭: {session_id}")
        return AgentResponse(
            success=True,
            data={"session_id": session_id},
            message="会话已关闭"
        )
    except Exception as e:
        logger.error(f"关闭会话失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models", response_model=AgentResponse)
async def get_available_models():
    """
    获取所有支持的模型列表
    """
    try:
        models = get_supported_models()
        
        return AgentResponse(
            success=True,
            data={"models": models},
            message="获取模型列表成功"
        )
    except Exception as e:
        logger.error(f"获取模型列表失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  文件上传
# ═══════════════════════════════════════════════════════

ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".jpg", ".jpeg", ".png", ".bmp", ".txt", ".csv",
}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


def _store_upload(file: UploadFile, path: Path) -> int:
    limit_message = f"文件过大，最大支持 {MAX_FILE_SIZE // 1024 // 1024}MB"
    if file.size is not None and file.size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=limit_message)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("xb") as target:
        try:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_FILE_SIZE:
                    raise HTTPException(status_code=400, detail=limit_message)
                target.write(chunk)
        except Exception:
            target.close()
            path.unlink(missing_ok=True)
            raise
    return written


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
):
    """
    上传单个文件
    支持 PDF/Word/Excel/图片/TXT/CSV
    """
    # 校验扩展名
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}。支持: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # 保存文件
    safe_name = f"u{user.id}_{uuid.uuid4().hex}_{Path(file.filename).name}"
    file_path = UPLOAD_DIR / safe_name

    try:
        file_size = await run_in_threadpool(_store_upload, file, file_path)

        # 异步处理文件内容
        try:
            from backend.services.file_processor import FileProcessor
            result = await run_in_threadpool(FileProcessor.process_file, str(file_path))
            extracted_preview = (result.get("content", "") or "")[:500]
        except Exception:
            extracted_preview = ""

        return {
            "success": True,
            "data": {
                "original_name": file.filename,
                "saved_name": safe_name,
                "file_path": str(file_path),
                "file_size": file_size,
                "file_type": ext,
                "extracted_preview": extracted_preview,
            },
            "message": f"文件 '{file.filename}' 上传成功",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")


@router.post("/upload/batch")
async def upload_files_batch(
    files: List[UploadFile] = File(...),
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
):
    """
    批量上传多个文件
    """
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="单次最多上传10个文件")

    results = []
    errors = []

    for file in files:
        try:
            ext = Path(file.filename).suffix.lower() if file.filename else ""
            if ext not in ALLOWED_EXTENSIONS:
                errors.append({"filename": file.filename, "error": f"不支持格式: {ext}"})
                continue

            safe_name = f"u{user.id}_{uuid.uuid4().hex}_{Path(file.filename).name}"
            file_path = UPLOAD_DIR / safe_name

            file_size = await run_in_threadpool(_store_upload, file, file_path)

            results.append({
                "original_name": file.filename,
                "saved_name": safe_name,
                "file_path": str(file_path),
                "file_size": file_size,
                "file_type": ext,
            })

        except HTTPException as e:
            errors.append({"filename": file.filename, "error": e.detail})
        except Exception as e:
            errors.append({"filename": file.filename, "error": str(e)})

    return {
        "success": True,
        "data": {
            "uploaded": results,
            "errors": errors,
            "total": len(files),
            "success_count": len(results),
            "failure_count": len(errors),
        },
        "message": f"上传完成: {len(results)}/{len(files)} 成功",
    }


# ═══════════════════════════════════════════════════════
#  工作流 - SSE 流式执行（核心）
# ═══════════════════════════════════════════════════════

# 全局进度跟踪（用于SSE轮询模式）
# 迁移到 progress_bus，agent 与 routes 共享同一字典，解除 agent → routes 反向依赖
from backend.services import progress_bus

_WORKFLOW_PROGRESS = progress_bus.PROXY
SSE_HEARTBEAT_SECONDS = 15
SSE_POLL_INTERVAL = 0.1

# 问卷缓存（用于 list 端点和跨请求访问）
_QUESTIONNAIRE_CACHE: Dict[str, Dict] = {}

_NODE_TO_STEP = {
    "init": 0, "step1_cost_list": 1, "step2_effective_data": 2,
    "step3_indicator_system": 3, "step4_indicator_scores": 4,
    "step5_scoring_standards": 5, "step6_evaluation_table": 6,
    "step7_indicator_analysis": 7, "step8_experience": 8,
    "step9_problems": 9, "step10_suggestions": 10,
    "step11_comprehensive": 11, "step12_basic_info": 12,
    "step13_work_intro": 13, "step14_final_report": 14,
}


def _make_sse_event(event_type: str, data: dict, seq: int = 0) -> str:
    """构建SSE事件字符串

    带 seq 时输出 `id: {seq}`，浏览器断线后可携带 Last-Event-ID 续传；
    不带 seq 时保持原格式（兼容旧客户端与单元测试）。
    """
    payload = {"event": event_type, **data, "timestamp": datetime.now().isoformat(), "seq": seq or 0}
    body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    return f"id: {seq}\n{body}" if seq else body


def _get_or_create_workflow(session_id: str, params: dict) -> tuple:
    """获取或创建 PerformanceAgent, 返回 (workflow, session_id)"""
    if session_id and session_id in active_workflows:
        return active_workflows[session_id], session_id
    workflow = PerformanceAgent(
        session_id=session_id,
        model_name=params.get("model_name") or settings.DEFAULT_MODEL,
        temperature=params.get("temperature", 0.7),
        top_p=params.get("top_p", 0.9),
        use_rag=params.get("use_rag", False),
        comparison_mode=params.get("comparison_mode", "single"),
        temperature_profile=params.get("temperature_profile", "normal"),
    )
    sid = workflow.session_id
    active_workflows[sid] = workflow
    active_workflow_configs[sid] = params
    return workflow, sid


def _register_workflow(request: WorkflowRequest, user: User, db: Session) -> str:
    upload_root = UPLOAD_DIR.resolve()
    for supplied_path in request.file_paths:
        path = Path(supplied_path).resolve()
        if path.parent != upload_root or not path.name.startswith(f"u{user.id}_") or not path.is_file():
            raise HTTPException(status_code=400, detail="工作流资料必须是本人上传的文件")

    session_id = request.session_id or str(uuid.uuid4())
    _validate_session_id(session_id)
    if db.query(WorkflowSession.id).filter(WorkflowSession.session_id == session_id).first():
        raise HTTPException(status_code=409, detail="会话已启动")
    if (session_id in active_workflows or session_id in active_agents
            or PerformanceAgent.load_state_backup(session_id)
            or db.query(UserSession.id).filter(UserSession.session_id == session_id).first()):
        raise HTTPException(status_code=409, detail="会话已存在")
    db.add(WorkflowSession(
        session_id=session_id, user_id=user.id, project_name=request.project_name,
        status="running", current_step=0, model_name=request.model_name or settings.DEFAULT_MODEL,
        use_rag=request.use_rag, comparison_mode=request.comparison_mode or "single",
        temperature_profile=request.temperature_profile or "normal",
    ))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="会话已启动") from exc
    return session_id


def _record_workflow_outcome(session_id: str, status: str, current_step: int,
                             final_report: str = "", token_used: int = 0) -> None:
    from backend.database import SessionLocal

    with SessionLocal.begin() as db:
        updates = {
            WorkflowSession.status: status,
            WorkflowSession.current_step: current_step,
            WorkflowSession.updated_at: datetime.now(timezone.utc),
        }
        if token_used:
            updates[WorkflowSession.token_used] = case(
                (WorkflowSession.token_used > 0, WorkflowSession.token_used), else_=token_used,
            )
        if status in {"completed", "failed"}:
            updates[WorkflowSession.completed_at] = datetime.now(timezone.utc)
        if final_report:
            updates[WorkflowSession.final_report] = final_report
        db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).update(updates)


def _build_registered_workflow(session_id: str, params: dict):
    try:
        workflow, _ = _get_or_create_workflow(session_id, params)
        return workflow
    except Exception:
        _record_workflow_outcome(session_id, "failed", 0)
        raise


def _launch_workflow(session_id, workflow, initial_state, retry_step=None):
    progress = {"status": "retrying" if retry_step else "running", "last_step": retry_step or 0,
                "current_step": retry_step or 0, "step_results": {}, "started_steps": []}
    config = {"configurable": {"thread_id": session_id}}
    _WORKFLOW_PROGRESS[session_id] = progress

    def _run_workflow():
        status = "failed"
        final_report = ""
        try:
            result = workflow.app.invoke(initial_state, config)
            final_report = result.get("final_report", "") or ""
            progress["has_error"] = bool(result.get("errors"))
            state = result.get("step_control", {})
            if state.get("requires_approval") or result.get("__interrupt__"):
                status = "paused"
                interrupts = result.get("__interrupt__", ())
                value = interrupts[0].value if interrupts else None
                gate = value if isinstance(value, dict) else {}
                progress["approval_step"] = gate.get("step_number") or state.get("approval_step") or progress["last_step"]
            elif 14 in result.get("completed_steps", []):
                status = "completed"
            else:
                progress["error_message"] = "工作流未完成"
        except Exception:
            logger.exception("后台工作流执行失败: {}", session_id)
            progress["error_message"] = "工作流执行失败"
        finally:
            try:
                token_used = sum(max(len(step["content"] or "") // 3, 1)
                                 for step in progress["step_results"].values())
                step = progress.get("approval_step") or progress["current_step"] or progress["last_step"]
                _record_workflow_outcome(session_id, status, step, final_report, token_used)
            except Exception:
                logger.exception("工作流结果持久化失败: {}", session_id)
                status = "failed"
                progress["error_message"] = "工作流结果保存失败"
            progress["final_report"] = final_report
            progress["status"] = status
            _WORKFLOW_PROGRESS.pop(session_id, None)

    # run_in_executor 不会复制 contextvars，手动带过去，否则工作流线程里的日志丢掉 request_id
    _ctx = contextvars.copy_context()
    asyncio.get_running_loop().run_in_executor(None, lambda: _ctx.run(_run_workflow))

    return progress


@router.post("/workflow/start-stream")
async def start_workflow_stream(request: WorkflowRequest, user: User = Depends(get_current_user),
                                db: Session = Depends(get_db)):
    """
    SSE 流式启动绩效评价工作流
    前端通过 EventSource / fetch + ReadableStream 实时接收14步进度

    事件类型:
      - step_start       : 步骤开始
      - step_complete    : 步骤完成（含 content_preview）
      - approval_required: 需要人工确认（Step 3/6/11）
      - warning          : 校验警告
      - error            : 步骤错误
      - complete         : 全部完成（含 final_report 预览）
    """
    params = {
        "model_name": request.model_name, "temperature": request.temperature,
        "top_p": request.top_p, "use_rag": request.use_rag,
        "comparison_mode": request.comparison_mode or "single",
        "temperature_profile": request.temperature_profile or "normal",
    }
    session_id = _register_workflow(request, user, db)
    workflow = _build_registered_workflow(session_id, params)
    initial_state = {
        "session_id": session_id,
        "project_name": request.project_name,
        "file_paths": request.file_paths,
        "model_name": request.model_name,
        "use_rag": request.use_rag,
        "require_approval": request.require_approval,
        "uploaded_files": [],
        "indicator_type": request.indicator_type or "项目支出指标体系",
        "step_control": {"language_style": request.language_style or "中立"},
    }

    progress_bus.begin(session_id)
    progress = _launch_workflow(session_id, workflow, initial_state)

    async def _event_generator():
        total_estimated_tokens = 0
        # 轮询进度并发射SSE事件
        emitted_steps = set()
        emitted_starts = set()
        loop = asyncio.get_running_loop()
        heartbeat_at = loop.time()
        questionnaire_emitted = False
        seq = 0

        def emit(event_type: str, data: dict) -> str:
            """统一出口：向总线申请序号并写入补发缓冲，保证与续传端点共用同一序列"""
            nonlocal seq
            seq = progress_bus.next_seq(session_id)
            frame = _make_sse_event(event_type, data, seq)
            progress_bus.record_frame(session_id, seq, frame)
            return frame

        try:
            yield ": connected\n\n"
            while True:
                await asyncio.sleep(SSE_POLL_INTERVAL)
                p = progress
                status = p["status"]
                emitted_any = False

                # 转发 LLM token 增量（真正的逐字流）
                for step_no, delta in progress_bus.drain_deltas(session_id):
                    emitted_any = True
                    yield emit("token_delta", {
                        "session_id": session_id,
                        "step_number": step_no,
                        "delta": delta,
                    })

                # 发射新完成的步骤
                for sn in range(1, 15):
                    if sn in emitted_steps:
                        continue
                    sr = p["step_results"].get(str(sn))
                    step_name = STEP_META.get(sn, {}).get("name", f"步骤{sn}")
                    if sn not in emitted_starts and (sn in p["started_steps"] or sr):
                        emitted_starts.add(sn)
                        emitted_any = True
                        yield emit("step_start", {
                            "step_number": sn, "step_name": step_name,
                            "session_id": session_id, "status": "running",
                        })
                    if sr:
                        step_content = sr.get("content", "") or ""
                        preview = step_content[:300]
                        emitted_any = True
                        yield emit("step_complete", {
                            "step_number": sn, "step_name": step_name,
                            "session_id": session_id, "content_preview": preview,
                            "content": step_content, "evidence": sr.get("evidence", []),
                        })
                        emitted_steps.add(sn)

                        total_estimated_tokens += max(len(step_content) // 3, 1)
                        emitted_any = True
                        yield emit("token_update", {
                            "tokens_used": total_estimated_tokens,
                            "step_number": sn,
                            "session_id": session_id,
                        })

                        # 步骤6完成后，发送问卷可生成事件
                        if sn == 6 and not questionnaire_emitted:
                            emitted_any = True
                            yield emit("questionnaire_ready", {
                                "session_id": session_id,
                                "message": "评价指标体系表已生成，可以创建调查问卷了",
                            })
                            questionnaire_emitted = True

                # 完成或错误
                if status == "completed":
                    final = p.get("final_report", "")
                    yield emit("complete", {
                        "session_id": session_id,
                        "completed_steps": sorted(emitted_steps),
                        "has_error": p.get("has_error", False),
                        "final_report_preview": final[:2000],
                        "tokens_used": total_estimated_tokens,
                    })
                    break
                elif status == "paused":
                    step = p["approval_step"]
                    yield emit("waiting_approval", {
                        "session_id": session_id, "approval_step": step,
                        "step_name": STEP_META.get(step, {}).get("name", ""),
                        "content_preview": p["step_results"].get(str(step), {}).get("content", "")[:500],
                    })
                    break
                elif status == "failed":
                    yield emit("error", {
                        "step_number": p["current_step"] or -1,
                        "step_name": STEP_META.get(p["current_step"], {}).get("name", "系统错误"),
                        "message": p.get("error_message", "工作流执行失败"),
                        "session_id": session_id,
                    })
                    break

                # 仅在本次循环没有产生任何事件时补心跳，避免与真实事件抢帧；
                # 首个步骤启动前给 30s 宽限期，避免刚建连就涌入心跳。
                started_any = bool(p.get("started_steps"))
                idle_for = loop.time() - heartbeat_at
                if not emitted_any and idle_for >= SSE_HEARTBEAT_SECONDS + (0 if started_any else 30):
                    heartbeat_at = loop.time()
                    yield ": ping " + str(int(time.time() * 1000)) + "\n\n"

        except Exception as e:
            err_msg = str(e)[:500]
            logger.error(f"SSE流式执行失败: {err_msg}")
            if "KeyError" in str(type(e).__name__):
                friendly = f"配置错误：缺少必要的参数，请联系管理员检查系统配置"
            elif "ConnectionError" in str(type(e).__name__) or "timeout" in err_msg.lower():
                friendly = "网络超时，请检查网络连接后重试"
            elif "APIError" in str(type(e).__name__) or "RateLimitError" in str(type(e).__name__):
                friendly = "AI服务繁忙，请稍后重试"
            elif "ValueError" in str(type(e).__name__):
                friendly = f"参数错误：{err_msg}"
            else:
                friendly = f"系统异常：{err_msg}"
            yield emit("error", {
                "step_number": -1, "step_name": "系统错误",
                "message": friendly, "session_id": session_id,
            })
        finally:
            progress_bus.detach_consumer(session_id)

    # 在返回响应体之前登记消费者，确保工作流线程一开始就能判断「有人收听」并走流式
    progress_bus.attach_consumer(session_id)
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        }
    )


@router.get("/workflow/{session_id}/stream")
async def resume_workflow_stream(session_id: str, request: Request,
                                 user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    """断线续传：按 Last-Event-ID 补发最近未收到的帧，并继续观测执行状态。

    仅补发内存中的最近 SSE_Replay 窗口（默认 400 帧），更早的历史以状态接口为准。
    说明：续传连接不接收 token_delta（增量只推给首个消费者），步骤正文在
    step_complete 事件与状态接口中都有完整内容。
    """
    _require_workflow(session_id, user, db)
    last_event_id = 0
    raw_header = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    if raw_header:
        try:
            last_event_id = int(raw_header)
        except (TypeError, ValueError):
            last_event_id = 0

    async def _replay_generator():
        seq_cursor = last_event_id
        sent_steps = set()
        terminal_seen = False

        def _parse(frame: str):
            try:
                body = frame.split("data:", 1)[1].strip()
                return json.loads(body)
            except Exception:
                return None

        # 1) 补发内存缓冲中漏掉的帧
        for seq, frame in progress_bus.frames_since(session_id, last_event_id):
            seq_cursor = max(seq_cursor, seq)
            event = _parse(frame)
            if event:
                if event.get("event") == "step_complete":
                    sent_steps.add(event.get("step_number"))
                if event.get("event") in {"complete", "error", "waiting_approval"}:
                    terminal_seen = True
            yield frame
        if terminal_seen:
            return

        def emit(event_type: str, data: dict) -> str:
            nonlocal seq_cursor
            seq = progress_bus.next_seq(session_id)
            seq_cursor = max(seq_cursor, seq)
            frame = _make_sse_event(event_type, data, seq)
            progress_bus.record_frame(session_id, seq, frame)
            return frame

        def emit_terminal(progress: dict):
            status = progress.get("status")
            if status == "completed":
                return emit("complete", {
                    "session_id": session_id,
                    "completed_steps": sorted(int(s) for s in progress.get("step_results", {})),
                    "has_error": progress.get("has_error", False),
                    "final_report_preview": (progress.get("final_report") or "")[:2000],
                    "resumed": True,
                })
            if status == "paused":
                step = progress.get("approval_step")
                return emit("waiting_approval", {
                    "session_id": session_id, "approval_step": step,
                    "step_name": STEP_META.get(step, {}).get("name", ""),
                    "content_preview": progress.get("step_results", {}).get(str(step), {}).get("content", "")[:500],
                    "resumed": True,
                })
            return emit("error", {
                "step_number": -1, "step_name": "系统错误",
                "message": progress.get("error_message", "工作流执行失败"),
                "session_id": session_id, "resumed": True,
            })

        # 2) 从当前进度续接：补发断线期间完成的步骤
        while True:
            progress = progress_bus.get_progress(session_id)
            if progress is None:
                yield f": ping {int(time.time() * 1000)}\n\n"
                return
            for sn_key in sorted(progress.get("step_results", {}), key=lambda k: int(k)):
                sn = int(sn_key)
                if sn in sent_steps:
                    continue
                sent_steps.add(sn)
                sr = progress["step_results"][sn_key]
                content = sr.get("content", "") or ""
                yield emit("step_complete", {
                    "step_number": sn,
                    "step_name": STEP_META.get(sn, {}).get("name", f"步骤{sn}"),
                    "session_id": session_id,
                    "content_preview": content[:300],
                    "content": content,
                    "evidence": sr.get("evidence", []),
                    "resumed": True,
                })
            if progress.get("status") in {"completed", "failed", "paused"}:
                yield emit_terminal(progress)
                return
            yield f": ping {int(time.time() * 1000)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        _replay_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        }
    )


# ═══════════════════════════════════════════════════════
#  工作流 - 同步执行（兼容旧调用 / 后端任务）
# ═══════════════════════════════════════════════════════

@router.post("/workflow/start", response_model=AgentResponse)
async def start_workflow(request: WorkflowRequest, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """同步启动工作流（一次性返回全部结果，耗时较长，推荐使用 start-stream）"""
    try:
        params = {
            "model_name": request.model_name, "temperature": request.temperature,
            "top_p": request.top_p, "use_rag": request.use_rag,
            "comparison_mode": request.comparison_mode or "single",
            "temperature_profile": request.temperature_profile or "normal",
        }
        session_id = _register_workflow(request, user, db)
        workflow = _build_registered_workflow(session_id, params)
        initial_state = {
            "session_id": session_id, "project_name": request.project_name,
            "file_paths": request.file_paths, "model_name": request.model_name,
            "use_rag": request.use_rag,
            "require_approval": request.require_approval,
            "indicator_type": request.indicator_type or "项目支出指标体系",
            "step_control": {"language_style": request.language_style or "中立"},
        }
        _WORKFLOW_PROGRESS[session_id] = {"status": "running", "last_step": 0, "step_results": {}}
        try:
            result = await run_in_threadpool(workflow.run_workflow, initial_state)
            status = ("paused" if result.get("interrupted") else
                      "completed" if 14 in result.get("completed_steps", []) else "failed")
            _record_workflow_outcome(session_id, status, result.get("current_step", 0),
                                     result.get("final_report", ""))
        finally:
            _WORKFLOW_PROGRESS.pop(session_id, None)
        return AgentResponse(success=True, data={
            "session_id": session_id,
            "completed_steps": result.get("completed_steps", []),
            "current_step": result.get("current_step", 0),
            "final_report": result.get("final_report", ""),
            "errors": result.get("errors", []),
            "warnings": result.get("warnings", []),
        }, message="工作流执行完成")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动工作流失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 状态查询（从 checkpoint 读取真实进度）
# ═══════════════════════════════════════════════════════

@router.get("/workflow/{session_id}/status", response_model=AgentResponse, dependencies=[Depends(_require_path_workflow)])
async def get_workflow_status(session_id: str, db: Session = Depends(get_db), after_step: int | None = None):
    record = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    workflow = active_workflows.get(session_id)
    config = {"configurable": {"thread_id": session_id}}
    checkpoint = workflow.get_state(config) if workflow else None
    values = getattr(checkpoint, "values", {}) or PerformanceAgent.load_state_backup(session_id)
    progress = _WORKFLOW_PROGRESS.get(session_id)
    results = {int(key): value if isinstance(value, dict) else {"content": str(value or "")}
               for key, value in values.get("step_results", {}).items()}
    if progress:
        results.update({int(key): value for key, value in progress["step_results"].copy().items()})

    tasks = getattr(checkpoint, "tasks", ())
    interrupts = [item for task in tasks for item in task.interrupts]
    gate = interrupts[0].value if interrupts and isinstance(interrupts[0].value, dict) else {}
    approval_step = gate.get("step_number") or values.get("step_control", {}).get("approval_step")
    completed = sorted(results or values.get("completed_steps", []))
    if not completed and record and record.status == "completed":
        completed = list(range(1, 15))
    status = record.status if record else ("completed" if 14 in completed else "not_found")
    current = (record.current_step or 0) if record else max(completed, default=0)
    if progress:
        status = progress["status"]
        current = progress.get("current_step") or progress["last_step"]
    elif interrupts:
        status = "awaiting_approval"
        current = approval_step or current

    summaries = []
    for step, result in sorted(results.items()):
        content = result.get("content") or ""
        summary = {
            "step_number": step, "step_name": result.get("step_name") or STEP_META.get(step, {}).get("name", ""),
            "status": "completed", "content_preview": content[:200], "version": result.get("version", 1),
        }
        # 结构化输出的判定（仅步骤 3/4/7 有）：manual_review 表示模型输出未通过 JSON 校验，需人工复核
        if result.get("structured_status"):
            summary["structured_status"] = result["structured_status"]
        if result.get("structured_errors"):
            summary["structured_errors"] = result["structured_errors"]
        if after_step is not None and step > after_step:
            summary["content"] = content
            summary["evidence"] = values.get("step_evidence", {}).get(str(step), [])
        summaries.append(summary)
    return AgentResponse(success=True, data={
        "session_id": session_id, "project_name": values.get("project_name") or (record.project_name if record else ""),
        "status": status, "current_step": current, "current_step_name": STEP_META.get(current, {}).get("name", "初始化"),
        "completed_steps": completed, "pending_steps": [step for step in range(1, 15) if step not in completed],
        "requires_approval": status == "awaiting_approval", "approval_step": approval_step,
        "approval_step_name": STEP_META.get(approval_step or 0, {}).get("name", ""),
        "retryable": status == "failed" and any(task.error for task in tasks),
        "review_steps": values.get("review_steps", []), "step_summaries": summaries,
        "errors": values.get("errors", []), "warnings": values.get("warnings", []),
        # 提示词用到了多少资料：前端据此展示「资料覆盖度」徽标
        "document_coverage": values.get("document_coverage", {}) or {},
        # 步骤间一致性检查结果（纯程序，不调模型）：前端据此展示结论徽标与问题清单
        "consistency": _consistency_report(values),
        # 步骤 7 逐指标评分处置台账：区分「已落地 / 模型判定资料不足 / 引用无法定位 / 未提及」
        # 老会话（本字段上线前跑的）没有这项，返回空对象，前端不展示徽标
        "score_disposition": values.get("score_disposition", {}) or {},
    })


def _raw_workflow_values(session_id: str) -> dict:
    """与 status 接口同源的原始状态。

    刻意**不**复用 `_get_workflow_values`：那个函数会给缺失 id 的指标补上「按名称拼」的
    id，而 `indicator_score_map` 是按真实 id 存的；两套 id 混用会让一致性检查误报
    「落库得分已过期」。
    """
    workflow = active_workflows.get(session_id)
    snapshot = workflow.get_state({"configurable": {"thread_id": session_id}}) if workflow else None
    values = dict(getattr(snapshot, "values", {}) or PerformanceAgent.load_state_backup(session_id))
    if not values:
        raise HTTPException(status_code=404, detail="工作流状态不存在")
    return values


def _consistency_report(values: dict) -> dict:
    """报告已生成时按当前状态现算一致性（0.4ms 级，纯程序）。

    不直接返回落库的 `consistency`：用户改过分值或重存过报告后，旧结论会与眼前这份报告
    对不上——那正是这个功能要防的事。报告还没生成时没有可检查的对象，原样透传。
    """
    from backend.services import consistency

    if not (values.get("final_report") or "").strip():
        return values.get("consistency", {}) or {}
    return consistency.check_consistency(values)


@router.post("/workflow/{session_id}/consistency-check", dependencies=[Depends(_require_path_workflow)])
async def rerun_consistency_check(session_id: str):
    """按当前状态重跑步骤间一致性检查。

    用在两类场景：工作流早于本功能上线（没有 consistency 字段）；或用户手改过
    分值/章节后想确认报告里的数字还对不对。纯程序计算，不调用模型、不花 token，
    结果会写回状态与备份，但不修改任何产出物内容。
    """
    from backend.services import consistency

    try:
        values = _raw_workflow_values(session_id)
        report = consistency.check_consistency(values)
        _persist_state(session_id, {"consistency": report})
        return {"success": True, "data": {
            "consistency": report,
            "summary": consistency.summary_line(report),
        }}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"一致性检查失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class RetryWorkflowRequest(BaseModel):
    session_id: str


@router.post("/workflow/retry", dependencies=[Depends(_require_body_workflow)])
async def retry_workflow(request: RetryWorkflowRequest):
    session_id = request.session_id
    workflow = active_workflows.get(session_id)
    config = {"configurable": {"thread_id": session_id}}
    checkpoint = workflow.get_state(config) if workflow else None
    if session_id in _WORKFLOW_PROGRESS or not checkpoint or not any(task.error for task in checkpoint.tasks):
        raise HTTPException(status_code=409, detail="没有可重试的失败步骤")
    step = max(checkpoint.values.get("completed_steps", []), default=0) + 1
    _launch_workflow(session_id, workflow, None, retry_step=step)
    return AgentResponse(success=True, data={"session_id": session_id, "current_step": step, "status": "retrying"})


@router.post("/workflow/approve", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def approve_workflow(request: ApproveRequest, user: User = Depends(get_current_user)):
    """
    人工确认指定步骤
    前端在收到 approval_required 事件后调用此接口
    调用 resume_from_checkpoint() 继续执行，并自动穿越后续所有中断点
    一次性完成全部14步，避免前端需要多次审批交互
    """
    try:
        session_id = request.session_id
        if session_id in _WORKFLOW_PROGRESS:
            raise HTTPException(status_code=409, detail="工作流正在执行")
        workflow = active_workflows.get(session_id)
        if workflow is None:
            raise HTTPException(status_code=409, detail="没有可恢复的审批检查点")
        config = {"configurable": {"thread_id": session_id}}
        checkpoint = workflow.get_state(config)
        waiting = [item.value for task in checkpoint.tasks for item in task.interrupts] if checkpoint else []
        if not waiting:
            raise HTTPException(status_code=409, detail="没有可恢复的审批检查点")
        gate = waiting[0] if isinstance(waiting[0], dict) else {}
        if request.approval_step is not None and request.approval_step != gate.get("step_number"):
            raise HTTPException(status_code=409, detail="审批点已变更，请刷新后确认当前步骤")

        resume_value = {"approved": request.approved, "feedback": request.feedback or "", "user_id": user.id}
        if request.background and checkpoint.values.get("require_approval"):
            _launch_workflow(session_id, workflow, Command(resume=resume_value))
            return AgentResponse(success=True, data={"session_id": session_id, "status": "running"})
        _WORKFLOW_PROGRESS[session_id] = {"status": "running", "last_step": 0, "step_results": {}}
        try:
            resume = (workflow.resume_from_checkpoint if checkpoint.values.get("require_approval")
                      else workflow.resume_through_all)
            result = await run_in_threadpool(resume, resume_value, config)
            if result.get("data", {}).get("__interrupt__"):
                result["interrupted"] = True
            status = ("paused" if result.get("interrupted") else
                      "completed" if 14 in result.get("completed_steps", []) else "failed")
            _record_workflow_outcome(session_id, status, result.get("current_step", 0),
                                     result.get("final_report", ""))
            if not result.get("success"):
                raise HTTPException(status_code=500, detail=result.get("error", "工作流执行失败"))
        finally:
            _WORKFLOW_PROGRESS.pop(session_id, None)

        # 提取各步骤结果供前端更新
        step_results = {}
        data_section = result.get("data", {})
        sr = data_section.get("step_results", {})
        for sn, sr_data in sr.items():
            if isinstance(sr_data, dict):
                step_results[str(sn)] = {
                    "step_name": STEP_META.get(int(sn), {}).get("name", f"步骤{sn}"),
                    "content": sr_data.get("content", "")[:3000],
                }

        return AgentResponse(success=True, data={
            "session_id": session_id,
            "approved": request.approved,
            "completed_steps": result.get("completed_steps", []),
            "current_step": result.get("current_step", 0),
            "final_report": result.get("final_report", ""),
            "step_results": step_results,
            "status": status,
        }, message="审批已处理")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"审批失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 报告查询 & 编辑存储
# ═══════════════════════════════════════════════════════

def _report_review_steps(session_id):
    return PerformanceAgent.load_state_backup(session_id).get("review_steps", [])


def _read_report(session_id: str, db: Session) -> tuple[str, str]:
    project_name = "绩效评价报告"
    record = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if record and record.final_report is not None:
        return record.final_report, record.project_name or project_name

    workflow = active_workflows.get(session_id)
    if workflow:
        config = {"configurable": {"thread_id": session_id}}
        snapshot = workflow.get_state(config)
        if snapshot and snapshot.values:
            values = snapshot.values
            content = values.get("final_report") or _assemble_report_from_steps(values)
            if content:
                return content, values.get("project_name") or project_name

    backup = PerformanceAgent.load_state_backup(session_id)
    if backup:
        project_name = backup.get("project_name") or project_name
        for key in ("final_report", "report_content", "report"):
            if isinstance(backup.get(key), str):
                return backup[key], project_name
        steps = backup.get("step_results", {})
        step14 = steps.get("14", {})
        content = (step14.get("content") if isinstance(step14, dict) else "")
        content = content or _assemble_report_from_backup_steps(backup)
        if content:
            return content, project_name

    raise HTTPException(status_code=404, detail="报告不存在，请先完成工作流")


@router.get("/workflow/{session_id}/report", dependencies=[Depends(_require_path_workflow)])
async def get_report_content(session_id: str, db: Session = Depends(get_db)):
    content, project_name = _read_report(session_id, db)
    review = _report_review_steps(session_id)
    return {
        "success": True,
        "data": {
            "session_id": session_id, "project_name": project_name,
            "content": content, "word_count": len(content), "review_steps": review,
        },
    }


class SaveReportRequest(BaseModel):
    session_id: str
    content: str


def _save_report(session_id: str, content: str, db: Session) -> None:
    if session_id in _WORKFLOW_PROGRESS:
        raise HTTPException(status_code=409, detail="工作流正在执行")
    record = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if record:
        record.final_report = content
        record.updated_at = datetime.now(timezone.utc)
        db.commit()
    elif PerformanceAgent.load_state_backup(session_id):
        PerformanceAgent.update_state_backup(session_id, {"final_report": content})
    else:
        raise HTTPException(status_code=404, detail="工作流不存在，无法保存")

    workflow = active_workflows.get(session_id)
    if workflow:
        try:
            workflow.app.update_state({"configurable": {"thread_id": session_id}}, {"final_report": content})
        except Exception:
            logger.exception("同步报告检查点失败: {}", session_id)


@router.post("/workflow/save-report", dependencies=[Depends(_require_body_workflow)])
async def save_report_content(req: SaveReportRequest, db: Session = Depends(get_db)):
    _save_report(req.session_id, req.content, db)
    return {
        "success": True, "message": "报告已保存",
        "data": {"session_id": req.session_id, "word_count": len(req.content)},
    }


def _chapter_from_values(values: dict, key: str):
    """取某一章的正文。

    第四章（`indicator_analysis`）优先取步骤 7 的正文 `step7_prose`：整段输出是
    「评分 JSON + 正文」，直接拼进报告就是一段裸 JSON（批次 22）。老会话没有这个字段，
    回退原文，宁可在报告里留着也不清空章节。
    """
    if key == "indicator_analysis":
        prose = (values.get("step7_prose") or "").strip()
        if prose:
            return prose
    return values.get(key)


def _assemble_report_from_steps(values: dict) -> str:
    """从各步骤State值拼装完整报告Markdown"""
    sections = []
    project_name = values.get("project_name", "绩效评价报告")
    sections.append(f"# {project_name}\n")

    chapter_map = [
        ("一、绩效评价工作开展情况", values.get("work_introduction")),
        ("二、项目基本情况", values.get("basic_info")),
        ("三、评价指标体系", values.get("evaluation_table")),
        ("四、指标得分及分析", _chapter_from_values(values, "indicator_analysis")),
        ("五、综合评价分析及结论", values.get("comprehensive_evaluation")),
        ("六、主要经验做法", values.get("experience_summary")),
        ("七、存在问题及原因分析", values.get("problems_analysis")),
        ("八、改进建议", values.get("suggestions")),
    ]

    for title, content_section in chapter_map:
        clean = (content_section or "").strip()
        if "[调用失败" in clean or "[模型" in clean:
            clean = ""
        if not clean:
            clean = "（本部分内容尚未生成）"
        if not clean.startswith("#"):
            sections.append(f"## {title}\n\n{clean}\n")
        else:
            sections.append(f"{clean}\n")

    sections.append("\n---\n")
    sections.append("*报告由云海睿评AI生成系统自动生成*")
    return "\n".join(sections)


def _assemble_report_from_backup_steps(backup: dict) -> str:
    """从磁盘备份的 step_results 拼装报告（step name → chapter mapping）"""
    step_results = backup.get("step_results", {})
    project_name = backup.get("project_name", "绩效评价报告")

    # 步骤号 → 章节标题 映射
    step_chapter_map = {
        "13": "一、绩效评价工作开展情况",
        "12": "二、项目基本情况",
        "6": "三、评价指标体系",
        "7": "四、指标得分及分析",
        "11": "五、综合评价分析及结论",
        "8": "六、主要经验做法",
        "9": "七、存在问题及原因分析",
        "10": "八、改进建议",
    }

    sections = [f"# {project_name}\n"]
    for step_num, chapter_title in sorted(step_chapter_map.items()):
        step = step_results.get(step_num, {})
        content = ""
        if isinstance(step, dict):
            content = step.get("content", "")
        if step_num == "7":
            # 第四章只拼正文：不带这一步，报告里就会出现整段评分 JSON（批次 22）
            content = (backup.get("step7_prose") or "").strip() or content
        if not content:
            content = "（本部分内容尚未生成）"
        if not content.startswith("#"):
            sections.append(f"## {chapter_title}\n\n{content}\n")
        else:
            sections.append(f"{content}\n")

    sections.append("\n---\n")
    sections.append("*报告由云海睿评AI生成系统自动生成*")
    return "\n".join(sections)


# ═══════════════════════════════════════════════════════
#  工作流 - 报告导出
# ═══════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════
#  文件下载
# ═══════════════════════════════════════════════════════

def _export_base_dir(user: User) -> str:
    return os.path.join("backend", ExportService.EXPORT_DIR, str(user.id))


def _export_with_cache(*, user: User, session_id: str, target: str, content: str,
                       file_label: str, fmt: str, style: ExportStyle,
                       title: str) -> tuple[str, bool]:
    """带幂等的导出：同一内容 + 同一排版 + 同一格式在 10 分钟内只渲染一次。

    返回 (文件路径, 是否复用已有文件)。幂等键不含时间戳，所以「连点三次导出」
    只会真正渲染一次，后两次直接返回第一次的文件路径。
    """
    key = export_cache.cache_key(
        user.id, session_id, target, fmt, export_cache.style_fingerprint(style)
    )
    content_hash = export_cache.content_fingerprint(content)
    cached_path = export_cache.lookup(key, content_hash)
    if cached_path:
        logger.info(f"导出命中幂等缓存，复用文件: {os.path.basename(cached_path)}")
        return cached_path, True

    if fmt == "pdf":
        file_path = ExportService.export_to_pdf(
            content, file_label, style, base_dir=_export_base_dir(user), title=title
        )
    else:
        file_path = ExportService.export_to_word(
            content, file_label, style, base_dir=_export_base_dir(user), title=title
        )
    export_cache.store(key, content_hash, file_path)
    return file_path, False


@router.get("/download/{file_name}")
async def download_export_file(file_name: str, user: User = Depends(get_current_user)):
    """
    下载导出的Word/PDF文件
    前端导出成功后通过此接口下载文件
    """
    # 安全检查：防止路径穿越
    safe_name = os.path.basename(file_name)
    if safe_name != file_name or Path(safe_name).suffix.lower() not in {".pdf", ".docx"}:
        raise HTTPException(status_code=404, detail="文件不存在")
    export_dir = os.path.join(_export_base_dir(user), ExportService.EXPORT_DIR)
    file_path = os.path.join(export_dir, safe_name)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {safe_name}")

    # 根据扩展名设置content-type
    media_type = "application/octet-stream"
    if safe_name.endswith(".pdf"):
        media_type = "application/pdf"
    elif safe_name.endswith(".docx"):
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=safe_name,
    )


def _export_blockers(session_id: str) -> list:
    """导出前必须处理的问题（缺章、步骤没跑完、报告过短……）。

    读不到工作流状态时返回空 —— 闸门只对「能看见状态」的会话生效，
    不能因为状态丢了就谁都不许导出（那是比缺章更糟的故障）。
    """
    from backend.services import consistency

    try:
        values = _get_workflow_values(session_id)
    except HTTPException:
        return []
    except Exception:
        logger.exception(f"导出前一致性检查失败，放行: {session_id}")
        return []
    try:
        report = consistency.check_consistency(values)
    except Exception:
        logger.exception(f"一致性检查异常，放行: {session_id}")
        return []
    return [item for item in report.get("issues", []) if item.get("blocking")]


@router.post("/workflow/export", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def export_workflow_report(request: ExportRequest, user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    """
    导出绩效评价报告（Word / PDF）
    支持：
      - 完整报告导出（需要工作流已完成）
      - 单步骤导出（step_number + step_content 直接导出）
    """
    try:
        session_id = request.session_id
        fmt = request.format or "word"
        style = ExportStyle.classic() if request.style_mode == "classic" else ExportStyle()
        project_name = request.project_name or "绩效评价报告"

        # ── 单步骤导出：使用前端传来的 step_content ──
        if request.step_number and request.step_content:
            step_name = STEP_META.get(request.step_number, {}).get("name", f"步骤{request.step_number}")
            file_label = f"{project_name}_步骤{request.step_number}_{step_name}"
            content = request.step_content

            file_path, reused = _export_with_cache(
                user=user, session_id=session_id, target=f"step:{request.step_number}",
                content=content, file_label=file_label, fmt=fmt, style=style,
                title=f"{project_name} - {step_name}",
            )

            file_name = os.path.basename(file_path)
            logger.info(f"步骤{request.step_number}导出成功: {file_name}（reused={reused}）")
            return AgentResponse(success=True, data={
                "session_id": session_id, "file_path": file_path,
                "file_name": file_name, "format": fmt,
                "step_number": request.step_number, "reused": reused,
            }, message=f"步骤{request.step_number}导出成功")

        if _report_review_steps(session_id):
            raise HTTPException(status_code=409, detail="报告关联的步骤尚待复核，请复核后重新组装报告")
        report_content, stored_project_name = _read_report(session_id, db)
        project_name = request.project_name or stored_project_name
        if not report_content:
            raise HTTPException(status_code=400, detail="报告内容为空，请确认工作流已完成")

        # 导出闸门：结构性缺陷（缺章/步骤没跑完/报告过短）先拦一次。
        # force 是明确的「我知道，仍然导出」—— 没有它，缺一章的会话就彻底导不出去了。
        if not request.force:
            blockers = _export_blockers(session_id)
            if blockers:
                detail = "报告还不能导出：" + "；".join(
                    f"{item['id']} {item['message']}" for item in blockers[:3]
                ) + "。确认要导出请选择「仍要导出」。"
                raise HTTPException(status_code=409, detail=detail,
                                    headers={"X-Export-Blocked": "1"})
        elif _export_blockers(session_id):
            logger.warning(f"报告存在结构性缺陷，用户强制导出: {session_id}")

        file_path, reused = _export_with_cache(
            user=user, session_id=session_id, target="report",
            content=report_content, file_label=project_name, fmt=fmt, style=style,
            title=project_name,
        )

        file_name = os.path.basename(file_path)
        logger.info(f"报告导出成功: {file_name}（reused={reused}）")
        return AgentResponse(success=True, data={
            "session_id": session_id, "file_path": file_path,
            "file_name": file_name, "format": fmt, "reused": reused,
        }, message="报告导出成功" if not reused else "已复用上次导出的文件")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"导出失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 单步骤导出
# ═══════════════════════════════════════════════════════

class ExportStepRequest(BaseModel):
    session_id: str
    step_number: int
    format: str = "word"


@router.post("/workflow/export-step", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def export_step_content(request: ExportStepRequest, user: User = Depends(get_current_user)):
    """
    导出单个步骤的内容为Word或PDF
    从工作流State中获取步骤内容并导出
    """
    try:
        session_id = request.session_id
        step_number = request.step_number
        fmt = request.format or "word"
        style = ExportStyle.classic()

        # 从工作流state获取步骤内容
        step_content = None
        if session_id in active_workflows:
            workflow = active_workflows[session_id]
        elif session_id in active_workflow_configs:
            cfg = active_workflow_configs[session_id]
            workflow = PerformanceAgent(
                session_id=session_id, model_name=cfg.get("model_name") or settings.DEFAULT_MODEL,
                temperature=cfg.get("temperature", 0.7),
                top_p=cfg.get("top_p", 0.9),
                use_rag=cfg.get("use_rag", False),
                comparison_mode=cfg.get("comparison_mode", "single"),
                temperature_profile=cfg.get("temperature_profile", "normal"),
            )
            active_workflows[session_id] = workflow
        else:
            raise HTTPException(status_code=404, detail="工作流不存在")

        config = {"configurable": {"thread_id": session_id}}
        state_obj = workflow.get_state(config)

        if state_obj and getattr(state_obj, 'values', None):
            values = state_obj.values
            project_name = values.get("project_name", "绩效评价报告")
            step_results = values.get("step_results", {})
            step_data = step_results.get(step_number, {})
            if isinstance(step_data, dict):
                step_content = step_data.get("content", "")
            else:
                step_content = str(step_data) if step_data else ""

        if not step_content:
            raise HTTPException(status_code=400, detail=f"步骤{step_number}内容为空，请确认该步骤已执行完成")

        step_name = STEP_META.get(step_number, {}).get("name", f"步骤{step_number}")
        file_label = f"{project_name}_步骤{step_number}_{step_name}"

        file_path, reused = _export_with_cache(
            user=user, session_id=session_id, target=f"step:{step_number}",
            content=step_content, file_label=file_label, fmt=fmt, style=style,
            title=f"{project_name} - {step_name}",
        )

        file_name = os.path.basename(file_path)
        logger.info(f"步骤{step_number}导出成功: {file_name}（reused={reused}）")
        return AgentResponse(success=True, data={
            "session_id": session_id,
            "file_path": file_path,
            "file_name": file_name,
            "format": fmt,
            "step_number": step_number,
            "reused": reused,
        }, message=f"步骤{step_number} {step_name} 导出成功")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"步骤导出失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 问卷答案提交
# ═══════════════════════════════════════════════════════

class QuestionnaireAnswerItem(BaseModel):
    question_id: str
    question_text: Optional[str] = None
    question_type: str = "likert"
    related_indicator_id: Optional[str] = None
    related_indicator_name: Optional[str] = None
    selected_level_index: Optional[int] = None
    text_answer: Optional[str] = None


class SubmitQuestionnaireRequest(BaseModel):
    session_id: str
    answers: List[QuestionnaireAnswerItem]
    questionnaire_version: str | None = None


def _published_questionnaire(session_id: str) -> dict:
    _validate_session_id(session_id)
    data = _QUESTIONNAIRE_CACHE.get(session_id) or (
        PerformanceAgent.load_state_backup(session_id).get("questionnaire") or {}
    )
    if not data.get("questions"):
        raise HTTPException(status_code=404, detail="问卷不存在或尚未生成")
    return data


def _questionnaire_response(session_id, data):
    return AgentResponse(success=True, data={
        "session_id": session_id, "questionnaire": data["questions"],
        "display_markdown": data.get("display_md", ""),
        "total_questions": len(data["questions"]), "questionnaire_version": data.get("version"),
    })


@router.get("/workflow/{session_id}/questionnaire", response_model=AgentResponse)
async def get_public_questionnaire(session_id: str):
    return _questionnaire_response(session_id, _published_questionnaire(session_id))


@router.post("/workflow/questionnaire/submit")
async def submit_questionnaire_answers(req: SubmitQuestionnaireRequest, db: Session = Depends(get_db)):
    from backend.models.user_models import QuestionnaireAnswer

    current = _published_questionnaire(req.session_id)
    history = PerformanceAgent.load_state_backup(req.session_id).get("questionnaire_history", [])
    versions = [current, *reversed(history)]
    if req.questionnaire_version:
        versions = [item for item in versions if item.get("version") == req.questionnaire_version]
    questions = {}
    for version in versions:
        for question in version.get("questions", []):
            questions.setdefault(question.get("question_id") or question.get("id"), question)

    seen = set()
    rows = []
    for answer in req.answers:
        question = questions.get(answer.question_id)
        if not question or answer.question_id in seen:
            raise HTTPException(status_code=422, detail="答卷包含未知或重复题目，请重新加载问卷")
        seen.add(answer.question_id)
        kind = question.get("question_type") or answer.question_type
        index = answer.selected_level_index
        if kind == "likert":
            if index is None:
                continue
            if not 0 <= index < len(question.get("scale_levels") or range(5)):
                raise HTTPException(status_code=422, detail="量表选项超出范围")
        elif not (answer.text_answer or "").strip():
            continue
        rows.append(QuestionnaireAnswer(
            session_id=req.session_id, question_id=answer.question_id,
            question_text=question.get("question_text") or question.get("question") or answer.question_text,
            question_type=kind, related_indicator_id=question.get("related_indicator_id"),
            related_indicator_name=question.get("related_indicator_name"),
            selected_level_index=index if kind == "likert" else None,
            text_answer=answer.text_answer if kind != "likert" else None,
            submitted_at=datetime.now(timezone.utc),
        ))
    if not rows:
        raise HTTPException(status_code=422, detail="请至少回答一道题")
    db.add_all(rows)
    db.commit()
    return {"success": True, "data": {"saved": len(rows), "errors": []}, "message": "答卷已保存"}


@router.get("/workflow/{session_id}/questionnaire/answers", dependencies=[Depends(_require_path_workflow)])
async def get_questionnaire_answers(session_id: str):
    """获取指定 session 的问卷答案（用于报告生成引用）"""
    from backend.database import SessionLocal
    from backend.models.user_models import QuestionnaireAnswer

    db = SessionLocal()
    try:
        rows = db.query(QuestionnaireAnswer).filter(
            QuestionnaireAnswer.session_id == session_id
        ).order_by(QuestionnaireAnswer.submitted_at).all()

        return {
            "success": True,
            "data": [{
                "id": r.id,
                "question_id": r.question_id,
                "question_text": r.question_text,
                "question_type": r.question_type,
                "related_indicator_name": r.related_indicator_name,
                "selected_level_index": r.selected_level_index,
                "text_answer": r.text_answer,
                "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            } for r in rows],
            "total": len(rows),
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════
#  工作流 - 调查问卷生成
# ═══════════════════════════════════════════════════════

@router.post("/workflow/questionnaire", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def generate_questionnaire(request: QuestionnaireRequest):
    session_id = request.session_id
    values = _get_workflow_values(session_id)
    indicators = values.get("indicator_system") or {}
    if not indicators.get("level1_indicators"):
        raise HTTPException(status_code=400, detail="指标体系尚未生成，请先完成步骤 3")
    signature = hashlib.sha256(json.dumps(
        [indicators, sorted(set(request.custom_indicator_ids)) if request.custom_indicator_ids is not None else None,
         request.max_questions], ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    backup = PerformanceAgent.load_state_backup(session_id)
    previous = _QUESTIONNAIRE_CACHE.get(session_id) or backup.get("questionnaire", {})
    if previous.get("indicator_version") == signature and previous.get("questions"):
        return _questionnaire_response(session_id, previous)

    generator = QuestionnaireGenerator()
    questions = generator.generate_questions(
        indicators, custom_indicators=request.custom_indicator_ids, max_questions=request.max_questions,
    )
    if not questions:
        return AgentResponse(success=True, data={
            "session_id": session_id, "questionnaire": [], "display_markdown": "", "total_questions": 0,
        }, message="资料不足，当前指标中没有符合条件的问卷题目")
    for item in previous.get("questions", []):
        if item.get("created_by") == "user" and item.get("question_type") == "open_ended":
            questions.append({**item, "index": len(questions) + 1})

    published = {
        "project_name": values.get("project_name") or "未命名项目", "questions": questions,
        "display_md": generator.format_questionnaire_for_display(questions),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "indicator_version": signature, "version": uuid.uuid4().hex,
    }
    history = backup.get("questionnaire_history", [])
    if previous.get("questions"):
        history = [*history, previous]
    PerformanceAgent.update_state_backup(session_id, {"questionnaire": published, "questionnaire_history": history})
    _QUESTIONNAIRE_CACHE[session_id] = published
    return _questionnaire_response(session_id, published)


@router.get("/workflow/questionnaire/list", response_model=AgentResponse)
async def list_questionnaires(user: User = Depends(get_current_user),
                              db: Session = Depends(get_db)):
    """
    列出所有已有问卷的 WorkflowSession 记录
    前端「问卷填写」Tab 调用此接口获取问卷列表
    """
    try:
        allowed_ids = None
        if user.role != UserRole.ADMIN:
            allowed_ids = {sid for (sid,) in _owned_workflows(db, user).with_entities(
                WorkflowSession.session_id
            )}
        results = []
        for sid, data in _QUESTIONNAIRE_CACHE.items():
            if data.get("deleted") or (allowed_ids is not None and sid not in allowed_ids):
                continue
            results.append({
                "session_id": sid,
                "project_name": data.get("project_name", "未命名项目"),
                "question_count": len(data.get("questions", [])),
                "updated_at": data.get("updated_at", ""),
            })

        # 也检查 active_workflows 中有 indicator_system 的session（可生成问卷但尚未生成）
        for sid, wf in active_workflows.items():
            if (allowed_ids is not None and sid not in allowed_ids) or sid in _QUESTIONNAIRE_CACHE:
                continue
            try:
                config = {"configurable": {"thread_id": sid}}
                state_obj = wf.get_state(config)
                if state_obj and getattr(state_obj, 'values', None):
                    indicator_system = state_obj.values.get("indicator_system", {})
                    if indicator_system and indicator_system.get("level1_indicators"):
                        results.append({
                            "session_id": sid,
                            "project_name": state_obj.values.get("project_name", "未命名项目"),
                            "question_count": 0,
                            "updated_at": str(datetime.now(timezone.utc)),
                            "needs_generation": True,
                        })
            except Exception:
                pass

        # 也检查 active_workflow_configs 中的session（内存evict后从配置重建）
        for sid in active_workflow_configs:
            if allowed_ids is not None and sid not in allowed_ids:
                continue
            if sid in _QUESTIONNAIRE_CACHE or any(r.get("session_id") == sid for r in results):
                continue
            try:
                results.append({
                    "session_id": sid,
                    "project_name": active_workflow_configs[sid].get("project_name", "未命名项目"),
                    "question_count": 0,
                    "updated_at": str(datetime.now(timezone.utc)),
                    "needs_generation": True,
                })
            except Exception:
                pass

        # 始终尝试从文件备份恢复问卷列表（服务器重启后内存数据丢失，磁盘备份是唯一数据源）
        try:
            backup_dir = performance_agent.STATE_BACKUP_DIR
            if os.path.exists(backup_dir):
                for fname in os.listdir(backup_dir):
                    if not fname.endswith(".json"):
                        continue
                    sid = fname[:-5]
                    if allowed_ids is not None and sid not in allowed_ids:
                        continue
                    # 跳过已在内存中的 session
                    if any(r.get("session_id") == sid for r in results):
                        continue
                    try:
                        # 必须走持锁的读入口：本循环可能与 update_state_backup 的
                        # 原子替换并发，直接 open() 会让对方拿到 WinError 5
                        backup = PerformanceAgent.load_state_backup(sid)
                        if not backup:
                            continue
                        qdata = backup.get("questionnaire", {})
                        if qdata.get("deleted"):
                            continue
                        indicator_system = backup.get("indicator_system", {})
                        has_indicator = bool(indicator_system and indicator_system.get("level1_indicators"))
                        results.append({
                            "session_id": sid,
                            "project_name": qdata.get("project_name", "") or backup.get("project_name", "未命名项目"),
                            "question_count": len(qdata.get("questions", [])),
                            "updated_at": qdata.get("updated_at", str(datetime.now(timezone.utc))),
                            "needs_generation": not bool(qdata.get("questions")) and has_indicator,
                        })
                    except Exception:
                        pass
        except Exception:
            pass

        return AgentResponse(success=True, data=results,
                             message=f"找到 {len(results)} 条问卷记录")
    except Exception as e:
        logger.error(f"获取问卷列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _delete_questionnaire_data(session_id: str, db: Session) -> None:
    from backend.models.user_models import QuestionnaireAnswer

    deleted = {"questions": [], "deleted": True}
    PerformanceAgent.update_state_backup(session_id, {"questionnaire": deleted, "questionnaire_history": []})
    _QUESTIONNAIRE_CACHE[session_id] = deleted
    db.query(QuestionnaireAnswer).filter(QuestionnaireAnswer.session_id == session_id).delete()


@router.delete("/workflow/questionnaire/{session_id}", response_model=AgentResponse,
               dependencies=[Depends(_require_path_workflow)])
async def delete_questionnaire(session_id: str, db: Session = Depends(get_db)):
    _delete_questionnaire_data(session_id, db)
    db.commit()
    return AgentResponse(success=True, data={"session_id": session_id}, message="问卷已删除")


class BatchDeleteQRequest(BaseModel):
    session_ids: list[str]


@router.post("/workflow/questionnaire/batch-delete", response_model=AgentResponse)
async def batch_delete_questionnaires(req: BatchDeleteQRequest, user: User = Depends(get_current_user),
                                      db: Session = Depends(get_db)):
    session_ids = list(dict.fromkeys(req.session_ids))
    for session_id in session_ids:
        _require_workflow(session_id, user, db)
    for session_id in session_ids:
        _delete_questionnaire_data(session_id, db)
    db.commit()
    return AgentResponse(success=True, data={"deleted": len(session_ids), "session_ids": session_ids},
                         message=f"成功删除 {len(session_ids)} 条问卷")


@router.post("/workflow/{session_id}/perspective", response_model=AgentResponse, dependencies=[Depends(_require_path_workflow)])
async def analyze_perspective(session_id: str, request: Dict[str, Any]):
    """
    步骤9多视角分析 — 前端切换视角时调用
    根据已有的问题分析结果，从指定视角重新生成分析内容
    perspectives: finance(财政) / congress(人大) / audit(审计)
    """
    try:
        perspective = request.get("perspective", "finance")
        perspective_names = {
            "finance": "财政部门视角",
            "congress": "人大常委会视角",
            "audit": "审计部门视角",
            "management": "管理方建议视角",
            "third_party": "第三方建议视角",
        }
        perspective_map = {
            "finance": "请从财政部门角度分析资金管理合规性、预算执行率、专款专用情况等方面的问题",
            "congress": "请从人大常委会角度分析预算监督执行、人大审查意见落实等方面的问题",
            "audit": "请从审计部门角度分析资金安全、内控制度有效性、财务核算规范性等方面的问题",
            "management": "请从项目管理方角度分析问题，并给出管理层面的改进建议，聚焦组织协调、资源配置、风险管控等方面",
            "third_party": "请从独立第三方评估机构角度分析问题，给出客观中立的改进建议，聚焦绩效目标设定合理性、评价方法科学性、结果应用有效性等方面",
        }

        if session_id not in active_workflows:
            if session_id not in active_workflow_configs:
                raise HTTPException(status_code=404, detail="工作流不存在")
            cfg = active_workflow_configs[session_id]
            workflow = PerformanceAgent(
                session_id=session_id, model_name=cfg.get("model_name") or settings.DEFAULT_MODEL,
                temperature=cfg.get("temperature", 0.7),
                top_p=cfg.get("top_p", 0.9),
                use_rag=cfg.get("use_rag", False),
                comparison_mode=cfg.get("comparison_mode", "single"),
                temperature_profile=cfg.get("temperature_profile", "normal"),
            )
            active_workflows[session_id] = workflow
        else:
            workflow = active_workflows[session_id]
        config = {"configurable": {"thread_id": session_id}}
        state_obj = workflow.get_state(config)
        if not state_obj or not getattr(state_obj, 'values', None):
            raise HTTPException(status_code=400, detail="工作流尚未启动")

        values = state_obj.values

        # 获取已有的扣分分析数据
        indicator_analysis = values.get("indicator_analysis", "")
        problems_analysis = values.get("problems_analysis", "")

        if not indicator_analysis and not problems_analysis:
            raise HTTPException(status_code=400, detail="步骤7/9尚未完成，无法进行视角分析")

        # 调用LLM从指定视角分析
        if workflow.base_agent:
            prompt = f"""## 项目名称
{values.get('project_name', '未命名')}

## 指标得分及分析
{indicator_analysis[:3000]}

## 已有问题分析
{problems_analysis[:3000]}

## 分析指令
{perspective_map.get(perspective, perspective_map['finance'])}
请深入分析并给出该视角下的具体问题、扣分指标映射和深层原因。"""

            from backend.agents.prompts.default_prompts import _UNIVERSAL_ROLE, _STYLE_INSTRUCTIONS, _QUALITY_RULES
            from langchain_core.messages import SystemMessage, HumanMessage
            system_prompt = _UNIVERSAL_ROLE.format(
                style_instruction="保持专业、客观、平衡的语调。分析要有深度，拒绝泛泛而谈。",
                quality_rules="所有分析必须基于提供的指标得分数据，不得编造。",
            )
            messages = [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
            response = workflow.base_agent.llm.invoke(messages, config={"timeout": 300})
            analysis = response.content if hasattr(response, 'content') else str(response)
        else:
            analysis = "模型未初始化，无法进行视角分析"

        perspective_name = perspective_names.get(perspective, "未知视角")

        return AgentResponse(success=True, data={
            "session_id": session_id,
            "perspective": perspective,
            "perspective_name": perspective_name,
            "analysis": analysis,
        }, message=f"已完成{perspective_name}分析")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"视角分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤7 - 逐指标评分分析接口
# ═══════════════════════════════════════════════════════

class IndicatorScoreInput(BaseModel):
    """单指标评分输入"""
    indicator_id: str
    indicator_name: str
    level1_name: str = ""
    level2_name: str = ""
    level3_name: str = ""
    full_score: float = 0
    actual_score: float = 0
    score_reason: str = ""


class BatchIndicatorScoreRequest(BaseModel):
    session_id: str
    scores: List[IndicatorScoreInput]


class IndicatorScoreItem(BaseModel):
    """单指标分析项"""
    indicator_id: str
    evaluation_point: str
    score: Optional[float] = None
    reason: Optional[str] = None


class GenerateIndicatorAnalysisRequest(BaseModel):
    session_id: str
    indicator_id: str
    level1_name: str = ""
    level2_name: str = ""
    level3_name: str = ""
    indicator_explanation: str = ""
    scoring_standard: str = ""
    score_inputs: List[IndicatorScoreItem] = []


@router.get("/workflow/{session_id}/indicators-for-scoring", dependencies=[Depends(_require_path_workflow)])
async def get_indicators_for_scoring(session_id: str):
    """
    步骤7：获取需要进行评分的指标体系结构（用于前端生成评分输入框）
    返回所有三级指标及其评分标准、指标解释，供逐指标填写得分和原因
    """
    try:
        values = _get_workflow_values(session_id)
        indicator_system = values.get("indicator_system", {})
        if not indicator_system or not indicator_system.get("level1_indicators"):
            raise HTTPException(status_code=400, detail="指标体系尚未生成，请先完成步骤3")

        items = []
        for l1 in indicator_system.get("level1_indicators", []):
            l1_name = l1.get("name", "")
            l1_score = l1.get("score", 0)
            for l2 in l1.get("children", []):
                l2_name = l2.get("name", "")
                l2_score = l2.get("score", 0)
                for l3 in l2.get("children", []):
                    l3_name = l3.get("name", "")
                    l3_score = l3.get("score", 0)
                    l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                    explanation = l3.get("explanation", "")
                    scoring_standard = l3.get("scoring_standard", "")

                    # 解析评分标准中的评价要点
                    eval_points = _parse_evaluation_points(scoring_standard)

                    items.append({
                        "indicator_id": l3_id,
                        "level1_name": l1_name,
                        "level1_score": l1_score,
                        "level2_name": l2_name,
                        "level2_score": l2_score,
                        "level3_name": l3_name,
                        "full_score": l3_score,
                        "indicator_explanation": explanation,
                        "scoring_standard": scoring_standard,
                        "evaluation_points": eval_points,
                    })

        return {"success": True, "data": {"indicators": items, "total_count": len(items)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取评分指标失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/generate-indicator-analysis", dependencies=[Depends(_require_body_workflow)])
async def generate_indicator_analysis(req: GenerateIndicatorAnalysisRequest):
    """
    步骤7：为单个三级指标生成分析内容
    根据操作者输入的评分要点的得分和原因，由大模型撰写指标分析
    """
    try:
        values = _get_workflow_values(req.session_id)
        cfg = active_workflow_configs.get(req.session_id, {})
        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL

        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.7)

        project_core = values.get("effective_data", "")[:3000]
        score_inputs_text = "\n".join([
            f"- 评价要点「{s.evaluation_point}」: 得分={s.score}, 原因={s.reason or '无'}"
            for s in req.score_inputs
        ]) if req.score_inputs else "（无评分输入）"

        prompt = f"""## 项目核心内容
{project_core}

## 当前分析指标
- 一级指标：{req.level1_name}
- 二级指标：{req.level2_name}
- 三级指标：{req.level3_name}
- 指标解释：{req.indicator_explanation}
- 评分标准：{req.scoring_standard}
- 满分分值：{sum(s.score or 0 for s in req.score_inputs) if req.score_inputs else 0}

## 操作者评分输入
{score_inputs_text}

## 任务
请根据上述信息，撰写该三级指标的详细分析内容。分析应：
1. 逐条对照评分标准中的评价要点
2. 说明得分/扣分原因
3. 引用项目核心内容中的具体事实和数据
4. 语言专业、客观

请直接输出分析内容。"""
        resp = model.invoke(prompt)
        analysis = resp.content if hasattr(resp, 'content') else str(resp)
        return {"success": True, "data": {"analysis": analysis, "indicator_id": req.indicator_id}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"生成指标分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/save-indicator-scores", dependencies=[Depends(_require_body_workflow)])
async def save_indicator_scores(req: BatchIndicatorScoreRequest):
    """
    步骤7：保存逐指标评分结果并生成评分表
    """
    try:
        values = _get_workflow_values(req.session_id)
        if req.session_id in _WORKFLOW_PROGRESS:
            raise HTTPException(status_code=409, detail="工作流执行中，暂不能修改评分")
        indicators = {item.get("id"): item for item in indicator_leaves(
            values.get("indicator_system", {}).get("level1_indicators", [])
        )}
        score_map = dict(values.get("indicator_score_map", {}))
        seen = set()
        for entry in req.scores:
            indicator = indicators.get(entry.indicator_id)
            full = score_number(indicator.get("score")) if indicator else None
            actual = score_number(entry.actual_score)
            if (entry.indicator_id in seen or full is None or actual is None
                    or actual > full or score_number(entry.full_score) != full):
                raise HTTPException(status_code=422, detail=f"指标 {entry.indicator_id} 的分值、满分或 ID 无效")
            seen.add(entry.indicator_id)
            score_map[entry.indicator_id] = {
                **entry.model_dump(), "indicator_name": indicator["name"],
                "full_score": full, "actual_score": actual,
            }

        # 生成评分表
        score_table = _generate_score_table(values.get("indicator_system", {}), score_map)
        _persist_state(req.session_id, {
            "indicator_score_map": score_map,
            "score_table": score_table,
            # 人工改过分值后台账必须跟着变，否则界面继续显示旧的「7/23 已落地」
            "score_disposition": performance_agent.refresh_disposition(
                values.get("score_disposition") or {}, score_map, list(indicators)
            ),
            "review_steps": _review_after(values, 7),
        })

        return {"success": True, "data": {"score_table": score_table, "saved_count": len(seen)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存评分结果失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤9 - 扣分点一键提取
# ═══════════════════════════════════════════════════════

@router.get("/workflow/{session_id}/extract-deductions", dependencies=[Depends(_require_path_workflow)])
async def extract_deduction_points(session_id: str):
    """
    步骤9：从步骤7指标分析中提取扣分点
    返回扣分指标列表，供操作者选择哪些作为问题提出
    """
    try:
        values = _get_workflow_values(session_id)
        indicator_analysis = values.get("indicator_analysis", "")
        score_map = values.get("indicator_score_map", {})

        if not indicator_analysis and not score_map:
            raise HTTPException(status_code=400, detail="步骤7尚未完成，请先生成指标分析")

        deduction_points = []
        if score_map:
            for ind_id, info in score_map.items():
                actual = score_number(info.get("actual_score"))
                full = score_number(info.get("full_score"))
                if actual is not None and full is not None and actual < full:
                    deduction_points.append({
                        "indicator_id": ind_id,
                        "indicator_name": info.get("indicator_name", ""),
                        "level1_name": info.get("level1_name", ""),
                        "level2_name": info.get("level2_name", ""),
                        "full_score": full,
                        "actual_score": actual,
                        "deduction": round(full - actual, 2),
                        "score_reason": info.get("score_reason", ""),
                        "selected": True,
                        "source": "score_map",
                    })

        return {
            "success": True,
            "data": {
                "deduction_points": deduction_points,
                "total_count": len(deduction_points),
                "total_deduction": sum(p.get("deduction", 0) for p in deduction_points),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提取扣分点失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeductionSelectionRequest(BaseModel):
    session_id: str
    selected_points: List[Dict[str, Any]]
    manual_problems: Optional[List[Dict[str, str]]] = None


@router.post("/workflow/confirm-deductions", dependencies=[Depends(_require_body_workflow)])
async def confirm_deduction_points(req: DeductionSelectionRequest):
    """
    步骤9：确认扣分点选择（含手动添加的问题）
    操作者选择哪些扣分点作为问题提出，也可以手动添加问题
    """
    try:
        values = _get_workflow_values(req.session_id)
        _persist_state(req.session_id, {
            "confirmed_deduction_points": req.selected_points,
            "manual_problems": req.manual_problems or [],
        })
        return {
            "success": True,
            "data": {
                "confirmed_count": len(req.selected_points),
                "manual_count": len(req.manual_problems or []),
            },
            "message": "扣分点已确认"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"确认扣分点失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤10后 - 逻辑校对
# ═══════════════════════════════════════════════════════

@router.post("/workflow/{session_id}/verify-logic", dependencies=[Depends(_require_path_workflow)])
async def verify_logic_consistency(session_id: str):
    """
    步骤10后：对指标体系、指标分析、问题、建议进行逻辑校对
    确保通篇逻辑合理、数据一致
    """
    try:
        values = _get_workflow_values(session_id)
        cfg = active_workflow_configs.get(session_id, {})

        indicator_system = values.get("indicator_system", {})
        indicator_analysis = values.get("indicator_analysis", "")
        problems_analysis = values.get("problems_analysis", "")
        suggestions = values.get("suggestions", "")
        score_table = values.get("score_table", "")

        if not all([indicator_analysis, problems_analysis, suggestions]):
            raise HTTPException(status_code=400, detail="请先完成步骤7-10后再进行逻辑校对")

        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL
        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.3)

        prompt = f"""你是绩效评价报告逻辑校对专家。请对以下内容进行逻辑一致性校对：

## 一、指标体系（Step6）
{json.dumps(indicator_system, ensure_ascii=False, indent=2)[:2000]}

## 二、指标分析（Step7）
{indicator_analysis[:2000]}

## 三、问题及原因分析（Step9）
{problems_analysis[:2000]}

## 四、改进建议（Step10）
{suggestions[:2000]}

## 校对要求
请检查以下维度：
1. **总分一致性**：各部分得分汇总是否等于100分
2. **问题-指标对应**：问题分析中提到的扣分项是否与指标分析中的扣分一致
3. **建议-问题对应**：每条建议是否都有对应的问题
4. **数据一致性**：全文提及的金额、比例、数字是否前后矛盾
5. **逻辑连贯性**：从指标体系到分析到问题到建议的逻辑链条是否完整

请按以下格式输出：
### 校对通过项
（列出通过校对的维度）

### 发现的问题
| 序号 | 类型 | 问题描述 | 涉及内容 | 修改建议 |
|------|------|----------|----------|----------|

### 总结
（是否需要修改，关键问题概述）"""
        resp = model.invoke(prompt)
        result_text = resp.content if hasattr(resp, 'content') else str(resp)

        # 保存到state（写回checkpoint）
        _persist_state(session_id, {"logic_verification": result_text})

        return {"success": True, "data": {"verification_result": result_text}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"逻辑校对失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤11 - 自动评分汇总与等级判定
# ═══════════════════════════════════════════════════════

class GradeThresholdsRequest(BaseModel):
    excellent: float = 90
    good: float = 80
    medium: float = 60
    poor: float = 0


@router.get("/workflow/{session_id}/score-summary", dependencies=[Depends(_require_path_workflow)])
async def get_score_summary(
    session_id: str,
    excellent: float = Query(90, description="优秀线"),
    good: float = Query(80, description="良好线"),
    medium: float = Query(60, description="合格线"),
):
    """
    步骤11：自动评分汇总与等级判定
    从指标体系+评分结果计算汇总得分、判定等级、生成图表数据
    """
    try:
        values = _get_workflow_values(session_id)
        indicator_system = values.get("indicator_system", {})
        score_map = values.get("indicator_score_map", {})

        if not indicator_system:
            raise HTTPException(status_code=400, detail="指标体系尚未生成")

        # 计算各一级指标得分
        level1_scores = []
        total_score = 0
        total_full = 0
        missing = 0
        assessed = 0

        for l1 in indicator_system.get("level1_indicators", []):
            l1_name = l1.get("name", "")
            l1_score = 0
            l1_full = 0
            level2_data = []

            for l2 in l1.get("children", []):
                l2_name = l2.get("name", "")
                l2_score = 0
                l2_full = 0
                level3_data = []

                for l3 in l2.get("children", []):
                    l3_name = l3.get("name", "")
                    l3_full_score = score_number(l3.get("score")) or 0
                    l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                    l3_score = actual_indicator_score(l3, score_map, l3_id)
                    missing += l3_score is None
                    assessed += l3_score is not None
                    l2_score += l3_score if l3_score is not None else 0
                    l2_full += l3_full_score
                    level3_data.append({
                        "name": l3_name,
                        "full_score": l3_full_score,
                        "actual_score": l3_score,
                        "score_rate": round(l3_score / l3_full_score * 100, 1) if l3_score is not None and l3_full_score > 0 else None,
                    })

                l1_score += l2_score
                l1_full += l2_full
                level2_data.append({
                    "name": l2_name,
                    "full_score": l2_full,
                    "actual_score": l2_score,
                    "score_rate": round(l2_score / l2_full * 100, 1) if l2_full > 0 else 0,
                    "items": level3_data,
                })

            total_score += l1_score
            total_full += l1_full
            level1_scores.append({
                "name": l1_name,
                "full_score": l1_full,
                "actual_score": l1_score,
                "score_rate": round(l1_score / l1_full * 100, 1) if l1_full > 0 else 0,
                "items": level2_data,
            })

        # 等级判定
        score_100 = round(total_score / total_full * 100, 2) if total_full > 0 else 0
        if missing or not assessed or abs(total_full - 100) > 0.01:
            grade = "待核实"
            score_100 = None
        elif score_100 >= excellent:
            grade = "优秀"
        elif score_100 >= good:
            grade = "良好"
        elif score_100 >= medium:
            grade = "合格"
        else:
            grade = "不合格"

        # 图表数据（用于前端柱状图）
        chart_data = {
            "categories": [item["name"] for item in level1_scores],
            "full_scores": [item["full_score"] for item in level1_scores],
            "actual_scores": [item["actual_score"] for item in level1_scores],
            "score_rates": [item["score_rate"] for item in level1_scores],
        }

        result = {
            "total_score": round(total_score, 2),
            "total_full": total_full,
            "score_100": score_100,
            "grade": grade,
            "missing_count": missing,
            # 缺失原因的分项口径（新增字段，向后兼容）：只有 missing_count 无法回答
            # 「是模型看过资料说没法评，还是系统把评分丢了」——实测这两种差 16 倍
            "disposition": values.get("score_disposition", {}) or {},
            "thresholds": {"excellent": excellent, "good": good, "medium": medium},
            "level1_scores": level1_scores,
            "chart_data": chart_data,
        }

        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"评分汇总失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤12 - 模块化内容生成
# ═══════════════════════════════════════════════════════

STEP12_MODULES = {
    "project_background": "项目背景",
    "project_content": "项目内容",
    "project_management": "项目组织管理情况",
    "project_funding": "项目资金投入情况",
    "project_targets": "项目绩效目标",
}


class ModuleGenerateRequest(BaseModel):
    session_id: str
    module_key: str
    module_name: Optional[str] = None


@router.post("/workflow/generate-module", dependencies=[Depends(_require_body_workflow)])
async def generate_module_content(req: ModuleGenerateRequest):
    """
    步骤12：单独生成某一个模块的内容
    支持：project_background, project_content, project_management, project_funding, project_targets
    """
    try:
        if req.module_key not in STEP12_MODULES and not req.module_name:
            raise HTTPException(status_code=400, detail=f"无效的模块: {req.module_key}，支持: {list(STEP12_MODULES.keys())}")

        module_name = req.module_name or STEP12_MODULES.get(req.module_key, req.module_key)

        values = _get_workflow_values(req.session_id)
        cfg = active_workflow_configs.get(req.session_id, {})
        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL

        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.7)

        project_name = values.get("project_name", "未命名项目")
        effective_data = values.get("effective_data", "") or values.get("raw_text", "") or ""

        module_prompts = {
            "project_background": "撰写项目背景部分，包括项目提出的背景、立项依据、项目必要性等。",
            "project_content": "撰写项目主要内容，包括建设内容/服务内容、实施周期、建设规模等。",
            "project_management": "撰写项目组织管理情况，包括组织机构设置、管理流程、制度建设等。",
            "project_funding": "撰写项目资金投入与使用情况，包括资金来源、到位情况、支出结构等。",
            "project_targets": "撰写项目绩效目标，包括原定的产出目标、效益目标、时效目标等。",
        }

        instruction = module_prompts.get(req.module_key, f"撰写「{module_name}」部分的详细内容。")

        prompt = f"""## 项目名称
{project_name}

## 项目核心资料
{effective_data[:5000]}

## 任务
{instruction}

请直接输出该模块的完整内容，要求客观、数据准确、语言规范。"""

        resp = model.invoke(prompt)
        content = resp.content if hasattr(resp, 'content') else str(resp)

        # 保存到模块字典（写回checkpoint）
        modules = values.get("basic_info_modules", {}) or {}
        modules[req.module_key] = content
        _persist_state(req.session_id, {"basic_info_modules": modules})

        return {
            "success": True,
            "data": {
                "module_key": req.module_key,
                "module_name": module_name,
                "content": content,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"生成模块内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow/{session_id}/modules", dependencies=[Depends(_require_path_workflow)])
async def get_module_contents(session_id: str):
    """获取步骤12各模块内容"""
    try:
        values = _get_workflow_values(session_id)
        modules = values.get("basic_info_modules", {}) or {}
        return {
            "success": True,
            "data": {
                "modules": [
                    {"key": k, "name": STEP12_MODULES.get(k, k), "content": v}
                    for k, v in modules.items()
                ]
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取模块内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  问卷 - 开放性问题添加
# ═══════════════════════════════════════════════════════

class AddOpenQuestionRequest(BaseModel):
    session_id: str
    question_text: str
    related_indicator_id: Optional[str] = None
    related_indicator_name: Optional[str] = None


@router.post("/workflow/questionnaire/add-open-question", dependencies=[Depends(_require_body_workflow)])
async def add_open_ended_question(req: AddOpenQuestionRequest):
    """
    向已有问卷手动添加开放性问题（填空题）
    操作者可以根据需要在里克特量表问卷基础上添加自定义开放性问题
    """
    try:
        session_id = req.session_id

        qcache = _published_questionnaire(session_id)
        questions = list(qcache["questions"])
        new_question = QuestionnaireGenerator().add_open_ended_question(questions, req.question_text)
        new_question["related_indicator_id"] = req.related_indicator_id or ""
        new_question["related_indicator_name"] = req.related_indicator_name or ""
        updated = {**qcache, "questions": questions,
                   "display_md": QuestionnaireGenerator().format_questionnaire_for_display(questions),
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        PerformanceAgent.update_state_backup(session_id, {"questionnaire": updated})
        _QUESTIONNAIRE_CACHE[session_id] = updated

        return {
            "success": True,
            "data": {"question": new_question, "total_questions": len(questions)},
            "message": "开放性问题已添加"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加开放性问题失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  语言风格更新
# ═══════════════════════════════════════════════════════

class LanguageStyleRequest(BaseModel):
    session_id: str
    language_style: str = "中立"


@router.post("/workflow/set-language-style", dependencies=[Depends(_require_body_workflow)])
async def set_language_style(req: LanguageStyleRequest):
    """
    设置步骤9、10的语言风格（中立/尖锐/委婉）
    """
    valid_styles = ["中立", "尖锐", "委婉"]
    if req.language_style not in valid_styles:
        raise HTTPException(status_code=400, detail=f"无效的语言风格，支持: {valid_styles}")

    try:
        values = _get_workflow_values(req.session_id)
        sc = values.get("step_control", {})
        sc["language_style"] = req.language_style
        _persist_state(req.session_id, {"step_control": sc})
        return {"success": True, "data": {"language_style": req.language_style}, "message": f"语言风格已设置为「{req.language_style}」"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"设置语言风格失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════

def _get_workflow_values(session_id: str) -> dict:
    """Read the checkpoint or disk projection, using legacy name-based indicator IDs."""
    workflow = active_workflows.get(session_id)
    snapshot = workflow.get_state({"configurable": {"thread_id": session_id}}) if workflow else None
    values = dict(getattr(snapshot, "values", {}) or PerformanceAgent.load_state_backup(session_id))
    if not values:
        raise HTTPException(status_code=404, detail="工作流状态不存在")
    if "step_results" in values:
        values["step_results"] = {int(key): value for key, value in values["step_results"].items()}

    def with_ids(items, parents=()):
        nodes = []
        for item in items:
            names = (*parents, item.get("name") or "")
            node = {**item, "id": item.get("id") or "_".join(names)}
            if item.get("children"):
                node["children"] = with_ids(item["children"], names)
            nodes.append(node)
        return nodes

    system = values.get("indicator_system")
    if system and system.get("level1_indicators"):
        values["indicator_system"] = {**system, "level1_indicators": with_ids(system["level1_indicators"])}
    return values


def _persist_state(session_id: str, updates: dict) -> None:
    if session_id in _WORKFLOW_PROGRESS and updates.keys() & {"indicator_system", "indicator_score_map", "step_results"}:
        raise HTTPException(status_code=409, detail="工作流执行中，暂不能修改指标或步骤")
    workflow = active_workflows.get(session_id)
    if workflow:
        config = {"configurable": {"thread_id": session_id}}
        workflow.app.update_state(config, updates)
    persisted = dict(updates)
    if "step_results" in updates:
        persisted["step_results"] = {
            key: {**item, "created_at": str(item.get("created_at", "")),
                  "updated_at": str(item.get("updated_at", ""))}
            for key, item in updates["step_results"].items()
        }
    PerformanceAgent.update_state_backup(session_id, persisted)


def _parse_evaluation_points(scoring_standard: str) -> list:
    """从评分标准文本中解析评价要点"""
    points = []
    if not scoring_standard:
        return points
    # 按常见序号分割
    lines = re.split(r'[\n;；]', scoring_standard)
    for line in lines:
        line = re.sub(r'^\s*[\d一二三四五六七八九十]+[、.)）]\s*', '', line).strip()
        if line and len(line) > 2:
            points.append(line)
    if not points:
        points = [scoring_standard]
    return points


def _generate_score_table(indicator_system: dict, score_map: dict) -> str:
    """从指标体系和评分映射生成评分表Markdown"""
    rows = []
    rows.append("| 一级指标 | 二级指标 | 三级指标 | 分值 | 得分 | 得分率 |")
    rows.append("|----------|----------|----------|------|------|--------|")

    for l1 in indicator_system.get("level1_indicators", []):
        l1_name = l1.get("name", "")
        for l2 in l1.get("children", []):
            l2_name = l2.get("name", "")
            l2_full, l2_score = 0, 0
            sub_rows = []
            for l3 in l2.get("children", []):
                l3_name = l3.get("name", "")
                l3_full = score_number(l3.get("score")) or 0
                l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                actual = actual_indicator_score(l3, score_map, l3_id)
                rate = f"{actual / l3_full * 100:.1f}%" if actual is not None and l3_full else "—"
                l2_full += l3_full
                l2_score += actual if actual is not None else 0
                score_text = actual if actual is not None else "待核实"
                sub_rows.append(f"| {l1_name} | {l2_name} | {l3_name} | {l3_full} | {score_text} | {rate} |")
            for sr in sub_rows:
                rows.append(sr)
            rows.append(f"| | **{l2_name} 已核实小计** | | **{l2_full}** | **{l2_score}** | — |")

    return "\n".join(rows)


# ═══════════════════════════════════════════════════════
#  步骤6 - 指标拖拽重排序
# ═══════════════════════════════════════════════════════

class ReorderRequest(BaseModel):
    session_id: str
    level: str  # "level1" / "level2" / "level3"
    from_index: int
    to_index: int
    parent_l1_idx: Optional[int] = None  # level2时需指定属于哪个一级指标
    parent_l2_idx: Optional[int] = None  # level3时需指定属于哪个二级指标


@router.post("/workflow/reorder-indicators", dependencies=[Depends(_require_body_workflow)])
async def reorder_indicators(req: ReorderRequest):
    """
    步骤6：拖拽重排序指标体系
    level1: 一级指标顺序拖动（其下所有二级、三级指标跟随移动）
    level2: 二级指标在某一级指标内拖动（其下三级指标跟随）
    level3: 三级指标在某二级指标内拖动（不能跨二级指标）
    """
    try:
        values = _get_workflow_values(req.session_id)
        indicator_system = values.get("indicator_system", {})
        if not indicator_system or not indicator_system.get("level1_indicators"):
            raise HTTPException(status_code=400, detail="指标体系尚未生成")

        l1_list = indicator_system.get("level1_indicators", [])

        if req.level == "level1":
            if 0 <= req.from_index < len(l1_list) and 0 <= req.to_index < len(l1_list):
                item = l1_list.pop(req.from_index)
                l1_list.insert(req.to_index, item)
                indicator_system["level1_indicators"] = l1_list

        elif req.level == "level2":
            if req.parent_l1_idx is None:
                raise HTTPException(status_code=400, detail="level2重排需要指定parent_l1_idx")
            if req.parent_l1_idx >= len(l1_list):
                raise HTTPException(status_code=400, detail="一级指标索引越界")
            l2_list = l1_list[req.parent_l1_idx].get("children", [])
            if 0 <= req.from_index < len(l2_list) and 0 <= req.to_index < len(l2_list):
                item = l2_list.pop(req.from_index)
                l2_list.insert(req.to_index, item)
                l1_list[req.parent_l1_idx]["children"] = l2_list

        elif req.level == "level3":
            if req.parent_l1_idx is None or req.parent_l2_idx is None:
                raise HTTPException(status_code=400, detail="level3重排需要指定parent_l1_idx和parent_l2_idx")
            if req.parent_l1_idx >= len(l1_list):
                raise HTTPException(status_code=400, detail="一级指标索引越界")
            l2_list = l1_list[req.parent_l1_idx].get("children", [])
            if req.parent_l2_idx >= len(l2_list):
                raise HTTPException(status_code=400, detail="二级指标索引越界")
            l3_list = l2_list[req.parent_l2_idx].get("children", [])
            if 0 <= req.from_index < len(l3_list) and 0 <= req.to_index < len(l3_list):
                item = l3_list.pop(req.from_index)
                l3_list.insert(req.to_index, item)
                l2_list[req.parent_l2_idx]["children"] = l3_list
                l1_list[req.parent_l1_idx]["children"] = l2_list
        else:
            raise HTTPException(status_code=400, detail="level参数无效，支持: level1/level2/level3")

        # 写回checkpoint（workflow.app 是编译后的图，才有update_state方法）
        _persist_state(req.session_id, {"indicator_system": indicator_system,
                                       "review_steps": _review_after(values, 3)})

        return {"success": True, "message": f"{req.level} 指标重排序完成"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重排序失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤3 - 指标体系模板库
# ═══════════════════════════════════════════════════════

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "templates")

# 预置经典模板
_DEFAULT_TEMPLATES = {
    "project_expenditure": {
        "name": "项目支出指标体系",
        "type": "项目支出",
        "description": "适用于项目支出绩效评价的通用指标体系模板",
        "level1_indicators": [
            {"name": "决策", "children": [
                {"name": "项目立项", "children": [
                    {"name": "立项依据充分性", "score": 3, "explanation": "项目立项是否符合法律法规、政策要求，立项程序是否规范"},
                    {"name": "立项程序规范性", "score": 3, "explanation": "项目申请、设立过程是否符合相关要求"},
                ]},
                {"name": "绩效目标", "children": [
                    {"name": "绩效目标合理性", "score": 3, "explanation": "项目绩效目标是否与实际工作内容相关，预期产出和效果是否符合正常业绩水平"},
                    {"name": "绩效指标明确性", "score": 3, "explanation": "绩效目标是否细化分解为具体的绩效指标，是否清晰、可衡量"},
                ]},
                {"name": "资金投入", "children": [
                    {"name": "预算编制科学性", "score": 3, "explanation": "预算编制是否经过科学论证，额度测算是否依据充分"},
                    {"name": "资金分配合理性", "score": 3, "explanation": "资金分配是否与项目实际需求相匹配"},
                ]},
            ]},
            {"name": "过程", "children": [
                {"name": "资金管理", "children": [
                    {"name": "资金到位率", "score": 4, "explanation": "实际到位资金与计划投入资金的比率"},
                    {"name": "资金使用合规性", "score": 4, "explanation": "资金使用是否符合财务管理制度规定"},
                ]},
                {"name": "组织实施", "children": [
                    {"name": "管理制度健全性", "score": 4, "explanation": "项目管理制度是否健全，执行是否有效"},
                    {"name": "制度执行有效性", "score": 4, "explanation": "项目实施是否符合管理规定"},
                ]},
            ]},
            {"name": "产出", "children": [
                {"name": "产出数量", "children": [
                    {"name": "实际完成率", "score": 10, "explanation": "实际产出数量与计划产出数量的比率"},
                ]},
                {"name": "产出质量", "children": [
                    {"name": "质量达标率", "score": 10, "explanation": "质量达标产出数与实际产出数的比率"},
                ]},
                {"name": "产出时效", "children": [
                    {"name": "完成及时性", "score": 5, "explanation": "实际完成时间与计划完成时间的比较"},
                ]},
                {"name": "产出成本", "children": [
                    {"name": "成本节约率", "score": 5, "explanation": "实际成本与计划成本的比较"},
                ]},
            ]},
            {"name": "效益", "children": [
                {"name": "项目效益", "children": [
                    {"name": "社会效益", "score": 12, "explanation": "项目实施对社会发展带来的影响"},
                    {"name": "经济效益", "score": 8, "explanation": "项目实施带来的直接或间接经济效益"},
                    {"name": "生态效益", "score": 5, "explanation": "项目实施对生态环境的影响"},
                    {"name": "可持续影响", "score": 6, "explanation": "项目实施后的可持续运行及发挥成效的情况"},
                ]},
                {"name": "满意度", "children": [
                    {"name": "服务对象满意度", "score": 5, "explanation": "项目服务对象或受益对象的满意程度"},
                ]},
            ]},
        ],
    },
    "department_overall": {
        "name": "部门整体指标体系",
        "type": "部门整体",
        "description": "适用于部门整体支出绩效评价的通用指标体系模板",
        "level1_indicators": [
            {"name": "部门决策", "children": [
                {"name": "目标设定", "children": [
                    {"name": "绩效目标合理性", "score": 3, "explanation": "部门设定的绩效目标是否合理"},
                    {"name": "绩效指标明确性", "score": 3, "explanation": "绩效指标是否明确、可衡量"},
                ]},
                {"name": "预算配置", "children": [
                    {"name": "人员经费合理性", "score": 3, "explanation": "人员经费占比是否合理"},
                    {"name": "公用经费合理性", "score": 3, "explanation": "公用经费安排的科学性"},
                    {"name": "项目支出合理性", "score": 4, "explanation": "重点项目支出保障程度"},
                ]},
            ]},
            {"name": "部门管理", "children": [
                {"name": "预算管理", "children": [
                    {"name": "预算执行率", "score": 4, "explanation": "部门整体预算执行情况"},
                    {"name": "结转结余率", "score": 3, "explanation": "部门资金结转结余控制情况"},
                ]},
                {"name": "资产管理", "children": [
                    {"name": "资产管理规范性", "score": 4, "explanation": "资产管理制度及执行情况"},
                ]},
                {"name": "业务管理", "children": [
                    {"name": "管理制度健全性", "score": 3, "explanation": "业务管理制度的完善程度"},
                ]},
            ]},
            {"name": "部门产出", "children": [
                {"name": "数量产出", "children": [
                    {"name": "重点工作完成率", "score": 15, "explanation": "部门年度重点工作实际完成情况"},
                ]},
                {"name": "质量产出", "children": [
                    {"name": "工作质量达标率", "score": 10, "explanation": "各项工作质量达标情况"},
                ]},
                {"name": "时效产出", "children": [
                    {"name": "工作完成及时性", "score": 5, "explanation": "各项工作是否按计划及时完成"},
                ]},
            ]},
            {"name": "部门效益", "children": [
                {"name": "履职效益", "children": [
                    {"name": "社会效益", "score": 15, "explanation": "部门履职对社会发展产生的综合效益"},
                    {"name": "服务对象满意度", "score": 10, "explanation": "服务对象的总体满意程度"},
                    {"name": "可持续影响", "score": 5, "explanation": "部门工作成效的长期可持续性"},
                ]},
            ]},
        ],
    },
}


def _get_templates_file() -> str:
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    return os.path.join(TEMPLATES_DIR, "indicator_templates.json")


def _load_templates() -> dict:
    """加载所有模板（内置+用户自定义）"""
    tp_file = _get_templates_file()
    if os.path.exists(tp_file):
        try:
            with open(tp_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 首次：写入默认模板到文件
    defaults = dict(_DEFAULT_TEMPLATES)
    with open(tp_file, "w", encoding="utf-8") as f:
        json.dump(defaults, f, ensure_ascii=False, indent=2)
    return defaults


@router.get("/workflow/templates", dependencies=[Depends(get_current_user)])
async def list_templates():
    """获取所有可用的指标体系模板"""
    try:
        templates = _load_templates()
        return {
            "success": True,
            "data": [
                {"id": k, "name": v["name"], "type": v["type"], "description": v.get("description", ""),
                 "indicator_count": sum(len(l2.get("children", [])) for l1 in v.get("level1_indicators", []) for l2 in l1.get("children", []))}
                for k, v in templates.items()
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow/templates/{template_id}", dependencies=[Depends(get_current_user)])
async def get_template(template_id: str):
    """获取单个模板详情"""
    try:
        templates = _load_templates()
        if template_id not in templates:
            raise HTTPException(status_code=404, detail="模板不存在")
        return {"success": True, "data": templates[template_id]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ImportTemplateRequest(BaseModel):
    session_id: str
    template_id: str


@router.post("/workflow/templates/import", dependencies=[Depends(_require_body_workflow)])
async def import_template(req: ImportTemplateRequest):
    """
    步骤3：一键导入模板指标体系到当前工作流
    """
    try:
        templates = _load_templates()
        if req.template_id not in templates:
            raise HTTPException(status_code=404, detail="模板不存在")

        values = _get_workflow_values(req.session_id)
        template = templates[req.template_id]

        # 导入模板的指标体系
        indicator_system = {
            "indicator_type": template["type"],
            "level1_indicators": template["level1_indicators"],
            "source": f"template:{req.template_id}",
        }
        _persist_state(req.session_id, {**_indicator_change(values, indicator_system),
                                       "review_steps": _review_after(values, 3)})

        return {"success": True, "data": indicator_system, "message": f"已导入模板「{template['name']}」"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"导入模板失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class SaveTemplateRequest(BaseModel):
    session_id: str
    template_name: str
    template_type: str = "自定义"


@router.post("/workflow/templates/save", dependencies=[Depends(_require_body_workflow)])
async def save_as_template(req: SaveTemplateRequest):
    """将当前工作流的指标体系保存为新模板"""
    try:
        values = _get_workflow_values(req.session_id)
        indicator_system = values.get("indicator_system", {})
        if not indicator_system or not indicator_system.get("level1_indicators"):
            raise HTTPException(status_code=400, detail="指标体系为空")

        templates = _load_templates()
        tid = str(uuid.uuid4())[:8]
        templates[tid] = {
            "name": req.template_name,
            "type": req.template_type,
            "description": f"用户自定义模板（{datetime.now(timezone.utc).strftime('%Y-%m-%d')}）",
            "level1_indicators": indicator_system.get("level1_indicators", []),
        }
        with open(_get_templates_file(), "w", encoding="utf-8") as f:
            json.dump(templates, f, ensure_ascii=False, indent=2)

        return {"success": True, "data": {"id": tid}, "message": "模板保存成功"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存模板失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤14 - 最终报告自定义顺序组合 & 格式校验
# ═══════════════════════════════════════════════════════

# 报告的默认章节顺序
REPORT_SECTION_ORDER = {
    "step12_basic_info": "一、基本情况",
    "step13_work_intro": "二、绩效评价工作开展情况",
    "step6_indicator_system": "三、绩效评价指标体系",
    "step7_indicator_analysis": "四、指标体系分析情况",
    "step8_experience": "五、经验做法",
    "step9_problems": "六、问题及原因分析",
    "step10_suggestions": "七、建议",
    "step11_comprehensive": "八、综合评价分析情况及评价结论",
}

# 章节key → 工作流状态中的内容字段名
_SECTION_KEY_TO_STATE_FIELD = {
    "step12_basic_info": "basic_info",
    "step13_work_intro": "work_introduction",
    "step6_indicator_system": "evaluation_table",
    "step7_indicator_analysis": "indicator_analysis",
    "step8_experience": "experience_summary",
    "step9_problems": "problems_analysis",
    "step10_suggestions": "suggestions",
    "step11_comprehensive": "comprehensive_evaluation",
}


class ReportOrderRequest(BaseModel):
    session_id: str
    sections: List[Dict[str, Any]]


@router.get("/workflow/{session_id}/report-order", dependencies=[Depends(_require_path_workflow)])
async def get_report_order(session_id: str):
    """获取最终报告的章节顺序"""
    try:
        values = _get_workflow_values(session_id)
        saved_order = values.get("report_section_order")
        if saved_order:
            return {"success": True, "data": saved_order, "is_custom": True}
        # 返回默认顺序
        default = []
        for k, v in REPORT_SECTION_ORDER.items():
            state_field = _SECTION_KEY_TO_STATE_FIELD.get(k, "")
            has_content = bool(values.get(state_field) if state_field else values.get(k))
            default.append({"key": k, "name": v, "enabled": True, "has_content": has_content})
        return {"success": True, "data": default, "is_custom": False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/report-order", dependencies=[Depends(_require_body_workflow)])
async def set_report_order(req: ReportOrderRequest):
    """设置最终报告的自定义章节顺序"""
    try:
        values = _get_workflow_values(req.session_id)
        _persist_state(req.session_id, {"report_section_order": req.sections})
        return {"success": True, "message": "报告章节顺序已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AssembleReportRequest(BaseModel):
    session_id: str
    show_title: bool = True
    include_sections: Optional[List[str]] = None


@router.post("/workflow/assemble-report", dependencies=[Depends(_require_body_workflow)])
async def assemble_final_report(req: AssembleReportRequest, db: Session = Depends(get_db)):
    """
    步骤14：按自定义顺序组装最终报告
    支持选择性地包含某些章节
    """
    try:
        values = _get_workflow_values(req.session_id)
        project_name = values.get("project_name", "未命名项目")

        # 使用自定义顺序或默认顺序
        custom_order = values.get("report_section_order")
        if custom_order:
            section_map = {s["key"]: s for s in custom_order}
        else:
            section_map = {k: {"key": k, "name": v, "enabled": True} for k, v in REPORT_SECTION_ORDER.items()}

        # 按顺序组装
        review = set(values.get("review_steps", []))
        stale_fields = {STEP_CONTENT_FIELDS.get(step) for step in review}
        report_sections = []
        ordered_keys = custom_order and [s["key"] for s in custom_order] or list(REPORT_SECTION_ORDER.keys())
        if req.include_sections:
            ordered_keys = [k for k in ordered_keys if k in req.include_sections]

        for key in ordered_keys:
            sec = section_map.get(key, {"enabled": True})
            if not sec.get("enabled", True):
                continue
            title = sec.get("name", REPORT_SECTION_ORDER.get(key, key))
            # 通过映射获取正确的状态字段（basic_info / work_introduction 等）
            state_field = _SECTION_KEY_TO_STATE_FIELD.get(key, "")
            if state_field in stale_fields:
                report_sections.append(f"## {title}\n\n待复核：上游结果已修改。")
                continue
            content = values.get(state_field, "") if state_field else ""
            if not content:
                content = values.get(key, "") or ""
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            if content:
                report_sections.append(f"## {title}\n\n{content}")

        full_report = "\n\n".join(report_sections)
        if req.show_title:
            full_report = f"# {project_name}绩效评价报告\n\n{full_report}"

        index = report_evidence_index(values.get("step_evidence", {}))
        if index:
            full_report += "\n\n" + index
        if review:
            return {"success": True, "data": {"report": full_report, "section_count": len(report_sections),
                                             "review_steps": sorted(review), "draft": True}}
        _save_report(req.session_id, full_report, db)
        return {"success": True, "data": {"report": full_report, "section_count": len(report_sections)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"组装报告失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/{session_id}/format-check", dependencies=[Depends(_require_path_workflow)])
async def check_report_format(session_id: str):
    """
    步骤14：依据396号文要求进行格式校对
    检查最终报告的格式规范性和内容完整性
    """
    try:
        values = _get_workflow_values(session_id)
        final_report = values.get("final_report", "")
        if not final_report:
            raise HTTPException(status_code=400, detail="请先组装最终报告")

        cfg = active_workflow_configs.get(session_id, {})
        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL
        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.3)

        prompt = f"""你是绩效评价报告格式审查专家。请对以下报告进行格式和内容校对：

## 格式要求（参照396号文）：
1. 报告结构是否完整（包含基本情况、工作开展、指标体系、分析、经验、问题、建议、结论）
2. 各级标题编号是否规范（"一、" → "(一)" → "1." 层级）
3. 表格格式是否规范（表头、边框、数据对齐）
4. 措辞是否正式、客观
5. 数据前后是否一致

## 待校对报告：
{final_report[:8000]}

请按以下格式输出校对结果：
### 通过的检查项
（列出符合要求的项目）

### 需要修改的问题
| 序号 | 问题类型 | 位置 | 问题描述 | 修改建议 |
|------|----------|------|----------|----------|

### 总结评价
是否建议通过/需修改后提交"""
        resp = model.invoke(prompt)
        result = resp.content if hasattr(resp, 'content') else str(resp)
        _persist_state(session_id, {"format_check_result": result})

        return {"success": True, "data": {"check_result": result}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"格式校对失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤12 - 动态模块添加/删除
# ═══════════════════════════════════════════════════════

class AddCustomModuleRequest(BaseModel):
    session_id: str
    module_key: str
    module_name: str
    content: Optional[str] = None


@router.post("/workflow/add-custom-module", dependencies=[Depends(_require_body_workflow)])
async def add_custom_module(req: AddCustomModuleRequest):
    """
    步骤12：添加自定义模块（除默认5个模块外）
    操作者可根据项目需要新增模块内容
    """
    try:
        values = _get_workflow_values(req.session_id)
        modules = values.get("basic_info_modules", {}) or {}

        if req.module_key in modules:
            raise HTTPException(status_code=400, detail="该模块key已存在")

        modules[req.module_key] = req.content or ""
        # 也更新模块名映射
        module_names = values.get("module_names", {}) or {}
        module_names[req.module_key] = req.module_name
        _persist_state(req.session_id, {
            "basic_info_modules": modules,
            "module_names": module_names,
        })

        return {"success": True, "data": {"key": req.module_key, "name": req.module_name},
                "message": f"模块「{req.module_name}」已添加"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加模块失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeleteModuleRequest(BaseModel):
    session_id: str
    module_key: str


@router.post("/workflow/delete-module", dependencies=[Depends(_require_body_workflow)])
async def delete_module(req: DeleteModuleRequest):
    """步骤12：删除自定义模块"""
    try:
        values = _get_workflow_values(req.session_id)
        modules = values.get("basic_info_modules", {}) or {}

        if req.module_key not in modules:
            raise HTTPException(status_code=404, detail="模块不存在")

        modules.pop(req.module_key, "")
        _persist_state(req.session_id, {"basic_info_modules": modules})

        return {"success": True, "message": f"模块已删除"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/update-module-content", dependencies=[Depends(_require_body_workflow)])
async def update_module_content(req: AddCustomModuleRequest):
    """
    步骤12：更新已有模块内容（支持默认5模块+自定义模块）
    """
    try:
        values = _get_workflow_values(req.session_id)
        modules = values.get("basic_info_modules", {}) or {}
        modules[req.module_key] = req.content or ""
        _persist_state(req.session_id, {"basic_info_modules": modules})
        return {"success": True, "message": f"模块「{req.module_name or req.module_key}」内容已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  导出样式自定义
# ═══════════════════════════════════════════════════════

class ExportStyleRequest(BaseModel):
    session_id: str
    font_family: str = "SimSun"
    font_size: int = 12
    line_spacing: float = 1.5
    page_margin: float = 2.5


@router.post("/workflow/export-style", dependencies=[Depends(_require_body_workflow)])
async def set_export_style(req: ExportStyleRequest):
    """
    设置导出PDF/Word文档的自定义样式
    支持的字体：SimSun(宋体), SimHei(黑体), KaiTi(楷体), FangSong(仿宋)
    """
    try:
        values = _get_workflow_values(req.session_id)
        style = {
            "font_family": req.font_family,
            "font_size": req.font_size,
            "line_spacing": req.line_spacing,
            "page_margin": req.page_margin,
        }
        _persist_state(req.session_id, {"export_style": style})
        return {"success": True, "data": style, "message": "导出样式已保存"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow/{session_id}/export-style", dependencies=[Depends(_require_path_workflow)])
async def get_export_style(session_id: str):
    """获取当前的导出样式设置"""
    try:
        values = _get_workflow_values(session_id)
        style = values.get("export_style", {
            "font_family": "SimSun", "font_size": 12,
            "line_spacing": 1.5, "page_margin": 2.5,
        })
        return {"success": True, "data": style}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
