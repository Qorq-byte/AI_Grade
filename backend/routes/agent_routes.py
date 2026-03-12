"""
Agent相关API路由
提供与前端交互的接口 — SSE流式 / 状态查询 / 人工确认 / 导出 / 问卷
"""
import re
import json
import asyncio
import os
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Request
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from loguru import logger

from langchain_openai import ChatOpenAI

from backend.agents.base_agent import BaseAgent
from backend.agents.performance_agent import PerformanceAgent
from backend.agents.tools.export_service import ExportService, ExportStyle, export_report
from backend.agents.tools.questionnaire_gen import QuestionnaireGenerator, generate_default_questionnaire
from backend.agents.prompts.default_prompts import STEP_META
from backend.utils.llm_helper import get_supported_models
from backend.middleware.auth_middleware import decode_token
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


class ExportRequest(BaseModel):
    """导出请求"""
    session_id: str
    format: str = "word"
    style_mode: str = "classic"
    project_name: Optional[str] = None
    step_number: Optional[int] = None
    step_content: Optional[str] = None


class QuestionnaireRequest(BaseModel):
    """问卷生成请求"""
    session_id: str
    max_questions: int = 30
    custom_indicator_ids: Optional[List[str]] = None


@router.post("/chat", response_model=AgentResponse)
async def chat_with_agent(request: AgentRequest):
    """
    与Agent对话接口
    支持长短期记忆、多模型切换
    """
    try:
        logger.info(f"收到对话请求: session_id={request.session_id}")
        
        # 获取或创建Agent实例
        session_id = request.session_id
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
                top_p=request.top_p
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


@router.post("/workflow/refine")
async def refine_step_content(req: RefineStepRequest):
    try:
        session_id = req.session_id
        # 获取或重建 config
        cfg = {}
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
            cfg = active_workflow_configs.get(session_id, {})

        workflow = active_workflows[session_id]
        config = {"configurable": {"thread_id": session_id}}
        state_obj = workflow.get_state(config)

        step_key = int(req.step_number)
        if state_obj and getattr(state_obj, 'values', None):
            step_results = state_obj.values.get("step_results", {})
            current = step_results.get(step_key, {})
            current_content = current.get("content", "") if isinstance(current, dict) else str(current)
        else:
            current_content = ""

        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL
        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.7)
        prompt = (
            f"你是绩效评价报告编辑助手。用户对步骤 {step_key} ({STEP_META.get(step_key, {}).get('name', '')})"
            f"的内容提出了优化要求。请根据用户指令对以下内容进行调整。\n"
            f"排版模式: {'经典排版（正式公文风格）' if req.layout_mode == 'classic' else '自动排版（现代商务风格）'}\n\n"
            f"=== 原始内容 ===\n{current_content[:3000]}\n\n"
            f"=== 用户指令 ===\n{req.message}\n\n"
            f"请直接输出优化后的完整内容，格式与原文保持一致。"
        )
        resp = model.invoke(prompt)
        refined = resp.content if hasattr(resp, 'content') else str(resp)

        if state_obj and getattr(state_obj, 'values', None):
            step_results = state_obj.values.get("step_results", {})
            if step_key in step_results and isinstance(step_results[step_key], dict):
                step_results[step_key]["content"] = refined
            # 写回checkpoint（直接修改values快照不会持久化）
            _persist_state(session_id, {"step_results": step_results})

        return {
            "success": True,
            "data": {
                "session_id": session_id,
                "step_number": step_key,
                "refined_content": refined,
            },
            "message": "内容已优化"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"优化步骤内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status", response_model=AgentResponse)
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
async def get_session_stats(session_id: str):
    """
    获取会话记忆统计
    """
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
async def close_session(session_id: str):
    """
    关闭会话
    """
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


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Query(None),
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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = f"{timestamp}_{file.filename}"
    file_path = UPLOAD_DIR / safe_name

    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail=f"文件过大，最大支持 {MAX_FILE_SIZE // 1024 // 1024}MB")
            f.write(content)

        file_size = len(content)

        # 异步处理文件内容
        try:
            from backend.services.file_processor import FileProcessor
            result = FileProcessor.process_file(str(file_path))
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

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = f"{timestamp}_{file.filename}"
            file_path = UPLOAD_DIR / safe_name

            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                errors.append({
                    "filename": file.filename,
                    "error": f"文件过大，最大 {MAX_FILE_SIZE // 1024 // 1024}MB"
                })
                continue

            with open(file_path, "wb") as f:
                f.write(content)

            results.append({
                "original_name": file.filename,
                "saved_name": safe_name,
                "file_path": str(file_path),
                "file_size": len(content),
                "file_type": ext,
            })

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
_WORKFLOW_PROGRESS: Dict[str, Dict] = {}

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


def _make_sse_event(event_type: str, data: dict) -> str:
    """构建SSE事件字符串"""
    payload = {"event": event_type, **data, "timestamp": datetime.now().isoformat()}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


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


@router.post("/workflow/start-stream")
async def start_workflow_stream(request: WorkflowRequest, http_request: Request):
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
    workflow, session_id = _get_or_create_workflow(request.session_id, params)
    config = {"configurable": {"thread_id": session_id}}

    # ── 从Bearer token解析用户（可选），用于工作流归属 ──
    user_id = None
    auth_header = http_request.headers.get("authorization", "") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        payload = decode_token(token)
        if payload and payload.get("type") == "access":
            user_id = payload.get("user_id")

    # ── 持久化工作流会话（供管理端监控、报告列表、重启后恢复使用）──
    try:
        from backend.database import SessionLocal
        from backend.models.user_models import WorkflowSession as WSModel
        dbs = SessionLocal()
        try:
            wf_record = dbs.query(WSModel).filter(WSModel.session_id == session_id).first()
            if wf_record is None:
                wf_record = WSModel(
                    session_id=session_id,
                    user_id=user_id,
                    project_name=request.project_name,
                    status="running",
                    current_step=0,
                    model_name=request.model_name or settings.DEFAULT_MODEL,
                    use_rag=request.use_rag,
                    comparison_mode=request.comparison_mode or "single",
                    temperature_profile=request.temperature_profile or "normal",
                )
                dbs.add(wf_record)
            else:
                wf_record.status = "running"
                wf_record.project_name = request.project_name
            dbs.commit()
            logger.info(f"工作流会话已持久化: {session_id} user={user_id}")
        except Exception as e:
            logger.warning(f"工作流会话持久化失败（非致命）: {e}")
            dbs.rollback()
        finally:
            dbs.close()
    except Exception as e:
        logger.warning(f"工作流会话持久化异常（非致命）: {e}")

    initial_state = {
        "session_id": session_id,
        "project_name": request.project_name,
        "file_paths": request.file_paths,
        "model_name": request.model_name,
        "use_rag": request.use_rag,
        "uploaded_files": [],
        "indicator_type": request.indicator_type or "项目支出指标体系",
        "step_control": {"language_style": request.language_style or "中立"},
    }

    async def _event_generator():
        has_error = False
        total_estimated_tokens = 0

        # 初始化进度跟踪
        _WORKFLOW_PROGRESS[session_id] = {
            "status": "running",
            "last_step": 0,
            "step_results": {},
            "warnings": [],
            "errors": [],
        }

        # 在后台线程运行 invoke，工作流不受SSE连接状态影响
        def _run_workflow():
            try:
                result = workflow.app.invoke(initial_state, config)
                final_report = result.get("final_report", "") or ""
                p = _WORKFLOW_PROGRESS.get(session_id)
                if p:
                    p["status"] = "done"
                    p["final_report"] = final_report
                    p["last_step"] = 14

                # ── 持久化完成状态（在后台线程执行，不依赖SSE客户端行为）──
                try:
                    from backend.database import SessionLocal
                    from backend.models.user_models import WorkflowSession as WSModel
                    dbsave = SessionLocal()
                    try:
                        wf_record = dbsave.query(WSModel).filter(
                            WSModel.session_id == session_id
                        ).first()
                        if wf_record:
                            wf_record.status = "completed"
                            wf_record.current_step = 14
                            wf_record.completed_at = datetime.now(timezone.utc)
                            if final_report:
                                wf_record.final_report = final_report
                            dbsave.commit()
                            logger.info(
                                f"工作流{session_id}已完成并保存到数据库 "
                                f"report_len={len(final_report)}"
                            )
                    finally:
                        dbsave.close()
                except Exception as e:
                    logger.warning(f"工作流完成状态持久化失败（非致命）: {e}")
            except Exception as e:
                logger.error(f"后台工作流执行失败: {e}")
                p = _WORKFLOW_PROGRESS.get(session_id)
                if p:
                    p["status"] = "error"
                    p["error_message"] = str(e)[:500]

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _run_workflow)

        # 轮询进度并发射SSE事件
        emitted_steps = set()
        questionnaire_emitted = False
        try:
            while True:
                await asyncio.sleep(0.5)
                p = _WORKFLOW_PROGRESS.get(session_id)
                if not p:
                    break
                status = p.get("status", "")

                # 发射新完成的步骤
                for sn in range(1, 15):
                    if sn in emitted_steps:
                        continue
                    sr = p.get("step_results", {}).get(str(sn))
                    if sr:
                        step_name = STEP_META.get(sn, {}).get("name", f"步骤{sn}")
                        step_content = sr.get("content", "") or ""
                        yield _make_sse_event("step_start", {
                            "step_number": sn, "step_name": step_name,
                            "session_id": session_id,
                        })
                        preview = step_content[:300]
                        yield _make_sse_event("step_complete", {
                            "step_number": sn, "step_name": step_name,
                            "session_id": session_id, "content_preview": preview,
                            "content": step_content,
                        })
                        emitted_steps.add(sn)

                        # 同步当前步骤到数据库（管理端监控实时进度）
                        try:
                            from backend.database import SessionLocal
                            from backend.models.user_models import WorkflowSession as WSModel
                            dbsync = SessionLocal()
                            try:
                                ws_row = dbsync.query(WSModel).filter(
                                    WSModel.session_id == session_id
                                ).first()
                                if ws_row and (ws_row.current_step or 0) < sn:
                                    ws_row.current_step = sn
                                    ws_row.updated_at = datetime.now(timezone.utc)
                                    dbsync.commit()
                            finally:
                                dbsync.close()
                        except Exception:
                            pass

                        total_estimated_tokens += max(len(step_content) // 3, 1)
                        yield _make_sse_event("token_update", {
                            "tokens_used": total_estimated_tokens,
                            "step_number": sn,
                            "session_id": session_id,
                        })

                        # 步骤6完成后，发送问卷可生成事件
                        if sn == 6 and not questionnaire_emitted:
                            yield _make_sse_event("questionnaire_ready", {
                                "session_id": session_id,
                                "message": "评价指标体系表已生成，可以创建调查问卷了",
                            })
                            questionnaire_emitted = True

                # 完成或错误
                if status == "done":
                    final = p.get("final_report", "")
                    yield _make_sse_event("complete", {
                        "session_id": session_id,
                        "completed_steps": sorted(emitted_steps),
                        "has_error": has_error,
                        "final_report_preview": final[:2000],
                        "tokens_used": total_estimated_tokens,
                    })
                    break
                elif status == "error":
                    yield _make_sse_event("error", {
                        "step_number": -1, "step_name": "系统错误",
                        "message": p.get("error_message", "工作流执行失败"),
                        "session_id": session_id,
                    })
                    break

            # ── 保存最终报告到数据库（兜底：补齐状态与token统计）──
            try:
                final_report = p.get("final_report", "") if p else ""
                if not final_report and session_id in active_workflows:
                    st = active_workflows[session_id].get_state(config)
                    if st and getattr(st, 'values', None):
                        final_report = st.values.get("final_report", "") or ""
                if final_report or total_estimated_tokens:
                    from backend.database import SessionLocal
                    from backend.models.user_models import WorkflowSession
                    dbsave = SessionLocal()
                    try:
                        wf_record = dbsave.query(WorkflowSession).filter(
                            WorkflowSession.session_id == session_id
                        ).first()
                        if wf_record:
                            wf_record.status = "completed"
                            wf_record.current_step = 14
                            wf_record.completed_at = wf_record.completed_at or datetime.now(timezone.utc)
                            if final_report:
                                wf_record.final_report = final_report
                            if total_estimated_tokens:
                                wf_record.token_used = (wf_record.token_used or 0) + total_estimated_tokens
                            dbsave.commit()
                            logger.info(f"工作流{session_id}报告已保存到数据库（SSE兜底）")
                        dbsave.close()
                    except Exception:
                        pass
            except Exception:
                pass

            # 清理
            _WORKFLOW_PROGRESS.pop(session_id, None)

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
            yield _make_sse_event("error", {
                "step_number": -1, "step_name": "系统错误",
                "message": friendly, "session_id": session_id,
            })

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


# ═══════════════════════════════════════════════════════
#  工作流 - 同步执行（兼容旧调用 / 后端任务）
# ═══════════════════════════════════════════════════════

@router.post("/workflow/start", response_model=AgentResponse)
async def start_workflow(request: WorkflowRequest):
    """同步启动工作流（一次性返回全部结果，耗时较长，推荐使用 start-stream）"""
    try:
        params = {
            "model_name": request.model_name, "temperature": request.temperature,
            "top_p": request.top_p, "use_rag": request.use_rag,
            "comparison_mode": request.comparison_mode or "single",
            "temperature_profile": request.temperature_profile or "normal",
        }
        workflow, session_id = _get_or_create_workflow(request.session_id, params)
        initial_state = {
            "session_id": session_id, "project_name": request.project_name,
            "file_paths": request.file_paths, "model_name": request.model_name,
            "use_rag": request.use_rag,
            "indicator_type": request.indicator_type or "项目支出指标体系",
            "step_control": {"language_style": request.language_style or "中立"},
        }
        result = workflow.run_workflow(initial_state)
        return AgentResponse(success=True, data={
            "session_id": session_id,
            "completed_steps": result.get("completed_steps", []),
            "current_step": result.get("current_step", 0),
            "final_report": result.get("final_report", ""),
            "errors": result.get("errors", []),
            "warnings": result.get("warnings", []),
        }, message="工作流执行完成")
    except Exception as e:
        logger.error(f"启动工作流失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 状态查询（从 checkpoint 读取真实进度）
# ═══════════════════════════════════════════════════════

@router.get("/workflow/{session_id}/status", response_model=AgentResponse)
async def get_workflow_status(session_id: str):
    """
    获取工作流实时状态 — 从 LangGraph checkpoint 读取真实进度
    支持：步骤摘要 / 审批状态 / 待处理步骤 / 警告 & 错误
    """
    try:
        if session_id not in active_workflows:
            if session_id not in active_workflow_configs:
                # 服务器重启后内存为空 → 尝试从磁盘备份恢复步骤摘要
                backup = PerformanceAgent.load_state_backup(session_id)
                step_results = backup.get("step_results", {}) if backup else {}
                if backup and step_results:
                    step_summaries = []
                    for sn_str in sorted(step_results.keys(), key=lambda x: int(x)):
                        sn = int(sn_str)
                        srn = step_results[sn_str] or {}
                        content = srn.get("content", "") if isinstance(srn, dict) else str(srn)
                        step_summaries.append({
                            "step_number": sn,
                            "step_name": STEP_META.get(sn, {}).get("name", f"Step {sn}"),
                            "status": "completed",
                            "content_preview": (content or "")[:200],
                        })
                    completed = [int(x) for x in step_results.keys()]
                    return AgentResponse(success=True, data={
                        "session_id": session_id, "status": "completed",
                        "current_step": 14, "completed_steps": completed,
                        "pending_steps": [], "requires_approval": False,
                        "step_summaries": step_summaries,
                        "errors": [], "warnings": [],
                    }, message="已从备份恢复（服务器重启后）")

                # 数据库兜底
                try:
                    from backend.database import SessionLocal
                    from backend.models.user_models import WorkflowSession
                    dbs = SessionLocal()
                    try:
                        wf_record = dbs.query(WorkflowSession).filter(
                            WorkflowSession.session_id == session_id
                        ).first()
                        if wf_record and wf_record.final_report:
                            return AgentResponse(success=True, data={
                                "session_id": session_id,
                                "status": "completed",
                                "current_step": 14,
                                "completed_steps": list(range(1, 15)),
                                "pending_steps": [],
                                "requires_approval": False,
                                "step_summaries": [],
                                "errors": [], "warnings": [],
                            }, message="已从数据库恢复")
                    finally:
                        dbs.close()
                except Exception:
                    pass

                return AgentResponse(success=True, data={
                    "session_id": session_id, "status": "not_found",
                    "current_step": 0, "completed_steps": [],
                }, message="工作流未找到（可能已过期）")
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

        workflow = active_workflows[session_id]
        config = {"configurable": {"thread_id": session_id}}
        state_obj = workflow.get_state(config)

        if state_obj is None:
            return AgentResponse(success=True, data={
                "session_id": session_id, "status": "not_started",
                "current_step": 0, "completed_steps": [],
            }, message="工作流尚未启动")

        values = getattr(state_obj, 'values', {}) or {}
        sc = values.get("step_control", {})
        requires_approval = sc.get("requires_approval", False)
        approval_step = sc.get("approval_step")
        current_step = sc.get("current_step", 0)
        step_statuses = sc.get("step_statuses", {})

        completed_steps = sorted([sn for sn, st in step_statuses.items() if st == "completed"])
        pending_steps = [sn for sn in range(1, 15) if sn not in completed_steps]

        sr = values.get("step_results", {})
        step_summaries = []
        for sn in completed_steps:
            srn = sr.get(sn, {})
            step_summaries.append({
                "step_number": int(sn),
                "step_name": STEP_META.get(int(sn), {}).get("name", f"Step {sn}"),
                "status": srn.get("status", "completed"),
                "content_preview": (srn.get("content", "") or "")[:200],
            })

        return AgentResponse(success=True, data={
            "session_id": session_id,
            "status": "awaiting_approval" if requires_approval else ("running" if pending_steps else "completed"),
            "current_step": current_step,
            "completed_steps": completed_steps,
            "pending_steps": pending_steps,
            "requires_approval": requires_approval,
            "approval_step": approval_step,
            "approval_step_name": STEP_META.get(approval_step or 0, {}).get("name", ""),
            "step_summaries": step_summaries,
            "errors": values.get("errors", []),
            "warnings": values.get("warnings", []),
        }, message="获取状态成功")
    except Exception as e:
        logger.error(f"获取工作流状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 人工确认（审批 Step 3/6/11）
# ═══════════════════════════════════════════════════════

@router.post("/workflow/approve", response_model=AgentResponse)
async def approve_workflow(request: ApproveRequest):
    """
    人工确认指定步骤
    前端在收到 approval_required 事件后调用此接口
    调用 resume_from_checkpoint() 继续执行，并自动穿越后续所有中断点
    一次性完成全部14步，避免前端需要多次审批交互
    """
    try:
        session_id = request.session_id
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

        workflow = active_workflows[session_id]
        config = {"configurable": {"thread_id": session_id}}

        resume_value = {"approved": request.approved, "feedback": request.feedback or ""}
        # 使用 resume_through_all 自动穿越步骤6和步骤11的审批点
        # 用户只需在前端审批一次（步骤3），后续中断点系统自动通过
        result = workflow.resume_through_all(resume_value=resume_value, config=config)

        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "工作流执行失败"))

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
        }, message="已确认，工作流继续执行至完成")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"审批失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 报告查询 & 编辑存储
# ═══════════════════════════════════════════════════════

@router.get("/workflow/{session_id}/report")
async def get_report_content(session_id: str):
    """获取完整报告Markdown内容（供编辑页面使用）"""
    try:
        content = ""
        project_name = "绩效评价报告"

        # 1. 内存中的工作流（服务器运行期间）
        if session_id in active_workflows:
            workflow = active_workflows[session_id]
            config = {"configurable": {"thread_id": session_id}}
            state_obj = workflow.get_state(config)
            if state_obj is not None and getattr(state_obj, 'values', None):
                values = state_obj.values
                content = values.get("final_report", "") or ""
                if not content or len(content.strip()) < 50:
                    content = _assemble_report_from_steps(values)
                project_name = values.get("project_name", project_name)

        # 2. 数据库中的报告（服务器重启后）
        if not content:
            try:
                from backend.database import SessionLocal
                from backend.models.user_models import WorkflowSession
                dbs = SessionLocal()
                try:
                    wf_record = dbs.query(WorkflowSession).filter(
                        WorkflowSession.session_id == session_id
                    ).first()
                    if wf_record and wf_record.final_report:
                        content = wf_record.final_report
                        project_name = wf_record.project_name or project_name
                finally:
                    dbs.close()
            except Exception:
                pass

        # 3. 磁盘状态备份（服务器重启后）
        if not content:
            backup = PerformanceAgent.load_state_backup(session_id)
            if backup:
                project_name = backup.get("project_name", project_name)
                # 优先返回显式保存的报告内容（save-report 写入）
                for key in ("final_report", "report_content", "report"):
                    val = backup.get(key, "")
                    if val and str(val).strip():
                        content = str(val)
                        break
                # 否则用步骤14或各步骤内容拼装
                if not content:
                    step_results = backup.get("step_results", {})
                    step_14 = step_results.get("14", {})
                    if isinstance(step_14, dict) and step_14.get("content"):
                        content = step_14["content"]
                    elif step_results:
                        content = _assemble_report_from_backup_steps(backup)

        if not content:
            raise HTTPException(status_code=404, detail="报告不存在，请先完成工作流")

        return {
            "success": True,
            "data": {
                "session_id": session_id,
                "project_name": project_name,
                "content": content,
                "word_count": len(content),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取报告内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class SaveReportRequest(BaseModel):
    session_id: str
    content: str


@router.post("/workflow/save-report")
async def save_report_content(req: SaveReportRequest):
    """保存编辑后的报告内容"""
    try:
        session_id = req.session_id
        saved_to_state = False

        # 1. 更新内存中工作流的 LangGraph state（通过update_state写回checkpoint）
        if session_id in active_workflows:
            workflow = active_workflows[session_id]
            config = {"configurable": {"thread_id": session_id}}
            state_obj = workflow.get_state(config)
            if state_obj is not None and getattr(state_obj, 'values', None):
                saved_to_state = _persist_state(session_id, {"final_report": req.content})

        # 2. 持久化到 DB
        saved_to_db = False
        try:
            from backend.database import SessionLocal
            from backend.models.user_models import WorkflowSession
            db = SessionLocal()
            try:
                wf = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
                if wf:
                    wf.final_report = req.content
                    wf.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    saved_to_db = True
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"持久化到DB失败（非致命）: {e}")

        # 3. 工作流不在内存且DB无记录 → 尝试写入磁盘备份，避免编辑内容丢失
        if not saved_to_state and not saved_to_db:
            try:
                backup = PerformanceAgent.load_state_backup(session_id)
                if backup:
                    backup["final_report"] = req.content
                    backup["updated_at"] = datetime.now(timezone.utc).isoformat()
                    backup_dir = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "state_backups",
                    )
                    os.makedirs(backup_dir, exist_ok=True)
                    with open(os.path.join(backup_dir, f"{session_id}.json"), "w", encoding="utf-8") as f:
                        json.dump(backup, f, ensure_ascii=False, indent=2)
                    saved_to_state = True
            except Exception as e:
                logger.warning(f"写入磁盘备份失败（非致命）: {e}")

        if not saved_to_state and not saved_to_db:
            raise HTTPException(status_code=404, detail="工作流不存在，无法保存")

        return {
            "success": True,
            "message": "报告已保存",
            "data": {"session_id": session_id, "word_count": len(req.content)}
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存报告失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════

def _assemble_report_from_steps(values: dict) -> str:
    """从各步骤State值拼装完整报告Markdown"""
    sections = []
    project_name = values.get("project_name", "绩效评价报告")
    sections.append(f"# {project_name}\n")

    chapter_map = [
        ("一、绩效评价工作开展情况", values.get("work_introduction")),
        ("二、项目基本情况", values.get("basic_info")),
        ("三、评价指标体系", values.get("evaluation_table")),
        ("四、指标得分及分析", values.get("indicator_analysis")),
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

@router.get("/download/{file_name}")
async def download_export_file(file_name: str):
    """
    下载导出的Word/PDF文件
    前端导出成功后通过此接口下载文件
    """
    # 安全检查：防止路径穿越
    safe_name = os.path.basename(file_name)
    export_dir = os.path.join("backend", ExportService.EXPORT_DIR)
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


@router.post("/workflow/export", response_model=AgentResponse)
async def export_workflow_report(request: ExportRequest):
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

            if fmt == "pdf":
                file_path = ExportService.export_to_pdf(
                    content, file_label, style, base_dir="backend", title=f"{project_name} - {step_name}"
                )
            else:
                file_path = ExportService.export_to_word(
                    content, file_label, style, base_dir="backend", title=f"{project_name} - {step_name}"
                )

            file_name = os.path.basename(file_path)
            logger.info(f"步骤{request.step_number}导出成功: {file_name}")
            return AgentResponse(success=True, data={
                "session_id": session_id, "file_path": file_path,
                "file_name": file_name, "format": fmt,
                "step_number": request.step_number,
            }, message=f"步骤{request.step_number}导出成功")

        # ── 完整报告导出 ──
        # 尝试从工作流state获取报告内容
        report_content = None

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
            # 工作流不在内存中，尝试从数据库获取报告内容
            try:
                from backend.database import SessionLocal
                from backend.models.user_models import WorkflowSession
                dbsession = SessionLocal()
                try:
                    wf_record = dbsession.query(WorkflowSession).filter(
                        WorkflowSession.session_id == session_id
                    ).first()
                    if wf_record and wf_record.final_report:
                        report_content = wf_record.final_report
                        project_name = request.project_name or wf_record.project_name or project_name
                    dbsession.close()
                except Exception:
                    pass
            except Exception:
                pass

            # 如果数据库也没有，尝试从磁盘备份恢复
            if not report_content:
                backup = PerformanceAgent.load_state_backup(session_id)
                if backup:
                    step_results = backup.get("step_results", {})
                    project_name = request.project_name or backup.get("project_name", project_name)
                    # 优先使用显式保存的报告（save-report 写入）
                    for key in ("final_report", "report_content", "report"):
                        val = backup.get(key, "")
                        if val and len(str(val).strip()) > 100:
                            report_content = str(val)
                            break
                    # 其次使用步骤14（组装最终报告）的内容
                    if not report_content:
                        step_14 = step_results.get("14", {})
                        if isinstance(step_14, dict) and step_14.get("content"):
                            report_content = step_14["content"]
                            logger.info(f"从磁盘备份步骤14恢复了报告: {session_id}")
                    # 最后从各步骤内容拼装
                    if not report_content and step_results:
                        report_content = _assemble_report_from_backup_steps(backup)

            if not report_content:
                raise HTTPException(status_code=404, detail="工作流不存在或已过期，请重新执行")

        # 从LangGraph state获取报告
        if not report_content and session_id in active_workflows:
            config = {"configurable": {"thread_id": session_id}}
            state_obj = active_workflows[session_id].get_state(config)

            if state_obj is not None and getattr(state_obj, 'values', None):
                values = state_obj.values
                final_report = values.get("final_report", "")
                if not final_report or len(final_report.strip()) < 100:
                    # 从各步骤结果拼装完整报告
                    final_report = _assemble_report_from_steps(values)
                report_content = final_report
                project_name = request.project_name or values.get("project_name", project_name)

        if not report_content:
            raise HTTPException(status_code=400, detail="报告内容为空，请确认工作流已完成")

        if fmt == "pdf":
            file_path = ExportService.export_to_pdf(
                report_content, project_name, style, base_dir="backend", title=project_name
            )
        else:
            file_path = ExportService.export_to_word(
                report_content, project_name, style, base_dir="backend", title=project_name
            )

        file_name = os.path.basename(file_path)
        logger.info(f"报告导出成功: {file_name}")
        return AgentResponse(success=True, data={
            "session_id": session_id, "file_path": file_path,
            "file_name": file_name, "format": fmt,
        }, message="报告导出成功")
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


@router.post("/workflow/export-step", response_model=AgentResponse)
async def export_step_content(request: ExportStepRequest):
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

        if fmt == "pdf":
            file_path = ExportService.export_to_pdf(
                step_content, file_label, style, base_dir="backend",
                title=f"{project_name} - {step_name}"
            )
        else:
            file_path = ExportService.export_to_word(
                step_content, file_label, style, base_dir="backend",
                title=f"{project_name} - {step_name}"
            )

        file_name = os.path.basename(file_path)
        logger.info(f"步骤{step_number}导出成功: {file_name}")
        return AgentResponse(success=True, data={
            "session_id": session_id,
            "file_path": file_path,
            "file_name": file_name,
            "format": fmt,
            "step_number": step_number,
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


@router.post("/workflow/questionnaire/submit")
async def submit_questionnaire_answers(req: SubmitQuestionnaireRequest):
    """
    提交调查问卷答案并持久化到数据库
    每个答案独立存储为一条 QuestionnaireAnswer 记录
    """
    from backend.database import SessionLocal
    from backend.models.user_models import QuestionnaireAnswer

    db = SessionLocal()
    saved = 0
    errors = []

    for a in req.answers:
        try:
            row = QuestionnaireAnswer(
                session_id=req.session_id,
                question_id=a.question_id,
                question_text=a.question_text,
                question_type=a.question_type,
                related_indicator_id=a.related_indicator_id,
                related_indicator_name=a.related_indicator_name,
                selected_level_index=a.selected_level_index,
                text_answer=a.text_answer,
                submitted_at=datetime.now(timezone.utc),
            )
            db.add(row)
            saved += 1
        except Exception as e:
            errors.append({"question_id": a.question_id, "error": str(e)})

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"提交失败: {str(e)}")
    finally:
        db.close()

    logger.info(f"问卷答案已保存: session={req.session_id}, 成功={saved}, 失败={len(errors)}")
    return {
        "success": True,
        "data": {"saved": saved, "errors": errors},
        "message": f"已保存 {saved} 条答案",
    }


@router.get("/workflow/{session_id}/questionnaire/answers")
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

@router.post("/workflow/questionnaire", response_model=AgentResponse)
async def generate_questionnaire(request: QuestionnaireRequest):
    """
    生成调查问卷（里克特5级量表）
    基于工作流中最新的指标体系
    """
    try:
        session_id = request.session_id

        # 优先从内存缓存返回已有问卷
        if session_id in _QUESTIONNAIRE_CACHE:
            qcache = _QUESTIONNAIRE_CACHE[session_id]
            return AgentResponse(success=True, data={
                "session_id": session_id,
                "questionnaire": qcache["questions"],
                "display_markdown": qcache.get("display_md", ""),
                "total_questions": len(qcache["questions"]),
            }, message=f"已返回 {len(qcache['questions'])} 道问卷题目（缓存）")

        # 尝试恢复工作流（内存 → 配置 → 磁盘备份）
        workflow = None
        indicator_system = {}
        project_core_content = ""

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

        if workflow:
            config = {"configurable": {"thread_id": session_id}}
            state_obj = workflow.get_state(config)
            if state_obj and getattr(state_obj, 'values', None):
                indicator_system = state_obj.values.get("indicator_system", {})
                project_core_content = state_obj.values.get("effective_data", "") or ""

        # 始终尝试从文件备份恢复（服务器重启后唯一数据源）
        backup = PerformanceAgent.load_state_backup(session_id)
        if backup:
            # 优先检查是否已有缓存的问卷
            cached_q = backup.get("questionnaire", {})
            if cached_q.get("questions"):
                return AgentResponse(success=True, data={
                    "session_id": session_id,
                    "questionnaire": cached_q["questions"],
                    "display_markdown": cached_q.get("display_md", ""),
                    "total_questions": len(cached_q["questions"]),
                }, message=f"已返回 {len(cached_q['questions'])} 道问卷题目（备份）")

            if not indicator_system or not indicator_system.get("level1_indicators"):
                indicator_system = backup.get("indicator_system", {})
                project_core_content = backup.get("effective_data", "") or project_core_content
                logger.info(f"从文件备份恢复了指标体系: {session_id}")

        if not workflow and (not indicator_system or not indicator_system.get("level1_indicators")):
            raise HTTPException(status_code=404, detail="工作流不存在，请先启动")

        if not indicator_system or not indicator_system.get("level1_indicators"):
            raise HTTPException(status_code=400, detail="指标体系尚未生成，请先完成Step 3")

        generator = QuestionnaireGenerator()
        # 使用工作流已有的 base_agent，避免重复创建LLM连接
        if workflow and hasattr(workflow, 'base_agent') and workflow.base_agent:
            generator.base_agent = workflow.base_agent

        questions = generator.generate_questions(
            indicator_system,
            project_core_content=project_core_content,
            custom_indicators=request.custom_indicator_ids,
            max_questions=request.max_questions,
        )
        display_md = generator.format_questionnaire_for_display(questions, title="调查问卷")

        # 获取项目名称
        project_name = "未命名项目"
        if state_obj and getattr(state_obj, 'values', None):
            project_name = state_obj.values.get("project_name") or project_name
        if project_name == "未命名项目" and backup:
            project_name = backup.get("project_name") or project_name

        # 缓存问卷到内存（供 list 端点使用）
        _QUESTIONNAIRE_CACHE[session_id] = {
            "project_name": project_name,
            "questions": questions,
            "display_md": display_md,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # 也保存到文件备份，防止服务器重启丢失问卷数据
        try:
            backup_save = PerformanceAgent.load_state_backup(session_id) or {}
            backup_save["questionnaire"] = {
                "questions": questions,
                "display_md": display_md,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "project_name": project_name,
            }
            backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f"{session_id}.json"), "w", encoding="utf-8") as f:
                json.dump(backup_save, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        return AgentResponse(success=True, data={
            "session_id": session_id,
            "questionnaire": questions,
            "display_markdown": display_md,
            "total_questions": len(questions),
        }, message=f"已生成 {len(questions)} 道问卷题目")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"问卷生成失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

