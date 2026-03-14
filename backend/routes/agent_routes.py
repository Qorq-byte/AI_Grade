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


@router.get("/workflow/questionnaire/list", response_model=AgentResponse)
async def list_questionnaires():
    """
    列出所有已有问卷的 WorkflowSession 记录
    前端「问卷填写」Tab 调用此接口获取问卷列表
    """
    try:
        results = []
        for sid, data in _QUESTIONNAIRE_CACHE.items():
            results.append({
                "session_id": sid,
                "project_name": data.get("project_name", "未命名项目"),
                "question_count": len(data.get("questions", [])),
                "updated_at": data.get("updated_at", ""),
            })

        # 也检查 active_workflows 中有 indicator_system 的session（可生成问卷但尚未生成）
        for sid, wf in active_workflows.items():
            if sid in _QUESTIONNAIRE_CACHE:
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
            backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
            if os.path.exists(backup_dir):
                for fname in os.listdir(backup_dir):
                    if not fname.endswith(".json"):
                        continue
                    sid = fname[:-5]
                    # 跳过已在内存中的 session
                    if any(r.get("session_id") == sid for r in results):
                        continue
                    try:
                        with open(os.path.join(backup_dir, fname), "r", encoding="utf-8") as f:
                            backup = json.load(f)
                        qdata = backup.get("questionnaire", {})
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


@router.delete("/workflow/questionnaire/{session_id}", response_model=AgentResponse)
async def delete_questionnaire(session_id: str):
    """
    删除指定 session 的问卷数据（从内存缓存、数据库和磁盘备份中彻底移除）
    """
    try:
        deleted = False
        if session_id in _QUESTIONNAIRE_CACHE:
            del _QUESTIONNAIRE_CACHE[session_id]
            deleted = True
        if session_id in active_workflows:
            try:
                active_workflows[session_id].close()
            except Exception:
                pass
            del active_workflows[session_id]
            deleted = True
        if session_id in active_workflow_configs:
            del active_workflow_configs[session_id]
            deleted = True
        if session_id in _WORKFLOW_PROGRESS:
            del _WORKFLOW_PROGRESS[session_id]
            deleted = True

        # 删除数据库中的 WorkflowSession 记录
        try:
            from backend.database import SessionLocal
            from backend.models.user_models import WorkflowSession
            db = SessionLocal()
            try:
                ws = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
                if ws:
                    db.delete(ws)
                    db.commit()
                    deleted = True
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"删除问卷DB记录失败: {e}")

        # 删除磁盘备份文件
        try:
            backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
            backup_file = os.path.join(backup_dir, f"{session_id}.json")
            if os.path.exists(backup_file):
                os.remove(backup_file)
                logger.info(f"已删除备份文件: {backup_file}")
                deleted = True
        except Exception as e:
            logger.warning(f"删除备份文件失败: {e}")

        if deleted:
            logger.info(f"问卷已删除: {session_id}")
            return AgentResponse(success=True, data={"session_id": session_id}, message="问卷已删除")
        else:
            return AgentResponse(success=False, data=None, message="未找到对应的问卷")
    except Exception as e:
        logger.error(f"删除问卷失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


class BatchDeleteQRequest(BaseModel):
    session_ids: list[str]

@router.post("/workflow/questionnaire/batch-delete", response_model=AgentResponse)
async def batch_delete_questionnaires(req: BatchDeleteQRequest):
    """批量删除问卷（内存、数据库、磁盘备份）"""
    deleted = []
    for session_id in req.session_ids:
        try:
            if session_id in _QUESTIONNAIRE_CACHE: del _QUESTIONNAIRE_CACHE[session_id]
            if session_id in active_workflows:
                try: active_workflows[session_id].close()
                except Exception: pass
                del active_workflows[session_id]
            if session_id in active_workflow_configs: del active_workflow_configs[session_id]
            if session_id in _WORKFLOW_PROGRESS: del _WORKFLOW_PROGRESS[session_id]
            # 删除磁盘备份文件
            try:
                backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
                backup_file = os.path.join(backup_dir, f"{session_id}.json")
                if os.path.exists(backup_file):
                    os.remove(backup_file)
                    logger.info(f"已删除备份文件: {backup_file}")
            except Exception:
                pass
            deleted.append(session_id)
        except Exception:
            pass
    # 批量删除数据库中的 WorkflowSession 记录
    try:
        from backend.database import SessionLocal
        from backend.models.user_models import WorkflowSession
        db = SessionLocal()
        try:
            db.query(WorkflowSession).filter(WorkflowSession.session_id.in_(req.session_ids)).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"批量删除问卷DB记录失败: {e}")
    return AgentResponse(success=True, data={"deleted": len(deleted), "session_ids": deleted}, message=f"成功删除 {len(deleted)} 条问卷")


# ═══════════════════════════════════════════════════════
#  工作流 - 步骤9多视角分析
# ═══════════════════════════════════════════════════════

@router.post("/workflow/{session_id}/perspective", response_model=AgentResponse)
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


@router.get("/workflow/{session_id}/indicators-for-scoring")
async def get_indicators_for_scoring(session_id: str):
    """
    步骤7：获取需要进行评分的指标体系结构（用于前端生成评分输入框）
    返回所有三级指标及其评分标准、指标解释，供逐指标填写得分和原因
    """
    try:
        workflow, values = _get_workflow_values(session_id)
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


@router.post("/workflow/generate-indicator-analysis")
async def generate_indicator_analysis(req: GenerateIndicatorAnalysisRequest):
    """
    步骤7：为单个三级指标生成分析内容
    根据操作者输入的评分要点的得分和原因，由大模型撰写指标分析
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.post("/workflow/save-indicator-scores")
async def save_indicator_scores(req: BatchIndicatorScoreRequest):
    """
    步骤7：保存逐指标评分结果并生成评分表
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
        # 保存评分到state（写回checkpoint）
        score_map = {}
        for s in req.scores:
            score_map[s.indicator_id] = {
                "indicator_name": s.indicator_name,
                "level1_name": s.level1_name,
                "level2_name": s.level2_name,
                "full_score": s.full_score,
                "actual_score": s.actual_score,
                "score_reason": s.score_reason,
            }

        # 生成评分表
        score_table = _generate_score_table(values.get("indicator_system", {}), score_map)
        _persist_state(req.session_id, {
            "indicator_score_map": score_map,
            "score_table": score_table,
        })

        return {"success": True, "data": {"score_table": score_table, "saved_count": len(score_map)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存评分结果失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤9 - 扣分点一键提取
# ═══════════════════════════════════════════════════════

@router.get("/workflow/{session_id}/extract-deductions")
async def extract_deduction_points(session_id: str):
    """
    步骤9：从步骤7指标分析中提取扣分点
    返回扣分指标列表，供操作者选择哪些作为问题提出
    """
    try:
        workflow, values = _get_workflow_values(session_id)
        indicator_analysis = values.get("indicator_analysis", "")
        score_table = values.get("score_table", "")
        score_map = values.get("indicator_score_map", {})

        if not indicator_analysis and not score_map:
            raise HTTPException(status_code=400, detail="步骤7尚未完成，请先生成指标分析")

        deduction_points = []
        if score_map:
            for ind_id, info in score_map.items():
                actual = info.get("actual_score", 0)
                full = info.get("full_score", 0)
                if actual < full:
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

        # 如果score_map为空，从分析文本中智能提取
        if not deduction_points and indicator_analysis:
            lines = indicator_analysis.split("\n")
            for line in lines:
                if "扣" in line or "得分" in line or "分" in line:
                    match = re.search(r'(?:扣|减)\s*(\d+\.?\d*)\s*分', line)
                    if match:
                        deduction_points.append({
                            "indicator_name": line[:60],
                            "deduction": float(match.group(1)),
                            "context": line[:200],
                            "selected": True,
                            "source": "text_extraction",
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


@router.post("/workflow/confirm-deductions")
async def confirm_deduction_points(req: DeductionSelectionRequest):
    """
    步骤9：确认扣分点选择（含手动添加的问题）
    操作者选择哪些扣分点作为问题提出，也可以手动添加问题
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
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

@router.post("/workflow/{session_id}/verify-logic")
async def verify_logic_consistency(session_id: str):
    """
    步骤10后：对指标体系、指标分析、问题、建议进行逻辑校对
    确保通篇逻辑合理、数据一致
    """
    try:
        workflow, values = _get_workflow_values(session_id)
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


@router.get("/workflow/{session_id}/score-summary")
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
        workflow, values = _get_workflow_values(session_id)
        indicator_system = values.get("indicator_system", {})
        score_map = values.get("indicator_score_map", {})

        if not indicator_system:
            raise HTTPException(status_code=400, detail="指标体系尚未生成")

        # 计算各一级指标得分
        level1_scores = []
        total_score = 0
        total_full = 0

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
                    l3_full_score = l3.get("score", 0)
                    l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                    actual = score_map.get(l3_id, {}).get("actual_score", 0) if score_map else l3_full_score
                    l3_score = float(actual) if actual else l3_full_score

                    l2_score += l3_score
                    l2_full += l3_full_score
                    level3_data.append({
                        "name": l3_name,
                        "full_score": l3_full_score,
                        "actual_score": l3_score,
                        "score_rate": round(l3_score / l3_full_score * 100, 1) if l3_full_score > 0 else 0,
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
        if score_100 >= excellent:
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
            "thresholds": {"excellent": excellent, "good": good, "medium": medium},
            "level1_scores": level1_scores,
            "chart_data": chart_data,
        }

        # 保存到state（写回checkpoint）
        _persist_state(session_id, {"score_summary": result})

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


@router.post("/workflow/generate-module")
async def generate_module_content(req: ModuleGenerateRequest):
    """
    步骤12：单独生成某一个模块的内容
    支持：project_background, project_content, project_management, project_funding, project_targets
    """
    try:
        if req.module_key not in STEP12_MODULES and not req.module_name:
            raise HTTPException(status_code=400, detail=f"无效的模块: {req.module_key}，支持: {list(STEP12_MODULES.keys())}")

        module_name = req.module_name or STEP12_MODULES.get(req.module_key, req.module_key)

        workflow, values = _get_workflow_values(req.session_id)
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


@router.get("/workflow/{session_id}/modules")
async def get_module_contents(session_id: str):
    """获取步骤12各模块内容"""
    try:
        workflow, values = _get_workflow_values(session_id)
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


@router.post("/workflow/questionnaire/add-open-question")
async def add_open_ended_question(req: AddOpenQuestionRequest):
    """
    向已有问卷手动添加开放性问题（填空题）
    操作者可以根据需要在里克特量表问卷基础上添加自定义开放性问题
    """
    try:
        session_id = req.session_id

        # 从缓存获取已有问卷
        if session_id in _QUESTIONNAIRE_CACHE:
            qcache = _QUESTIONNAIRE_CACHE[session_id]
            questions = qcache.get("questions", [])
        else:
            # 尝试从备份恢复
            backup = PerformanceAgent.load_state_backup(session_id)
            qcache = backup.get("questionnaire", {})
            questions = qcache.get("questions", [])
            if not questions:
                raise HTTPException(status_code=404, detail="未找到问卷，请先生成问卷")

        import uuid
        new_question = {
            "id": str(uuid.uuid4())[:8],
            "index": len(questions) + 1,
            "question": req.question_text,
            "question_type": "open_ended",
            "related_indicator_id": req.related_indicator_id or "",
            "related_indicator_name": req.related_indicator_name or "",
            "scale_levels": [],
            "created_by": "user",
        }
        questions.append(new_question)

        # 更新缓存
        if session_id in _QUESTIONNAIRE_CACHE:
            _QUESTIONNAIRE_CACHE[session_id]["questions"] = questions
        else:
            _QUESTIONNAIRE_CACHE[session_id] = {
                "questions": questions,
                "display_md": "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        # 同时保存到文件备份
        try:
            backup_save = PerformanceAgent.load_state_backup(session_id) or {}
            backup_save["questionnaire"] = {
                "questions": questions,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "project_name": qcache.get("project_name", backup_save.get("project_name", "")),
            }
            backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state_backups")
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f"{session_id}.json"), "w", encoding="utf-8") as f:
                json.dump(backup_save, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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


@router.post("/workflow/set-language-style")
async def set_language_style(req: LanguageStyleRequest):
    """
    设置步骤9、10的语言风格（中立/尖锐/委婉）
    """
    valid_styles = ["中立", "尖锐", "委婉"]
    if req.language_style not in valid_styles:
        raise HTTPException(status_code=400, detail=f"无效的语言风格，支持: {valid_styles}")

    try:
        workflow, values = _get_workflow_values(req.session_id)
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

def _get_workflow_values(session_id: str):
    """获取工作流状态值（自动重建workflow）"""
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
    return workflow, state_obj.values


def _persist_state(session_id: str, updates: dict) -> bool:
    """
    将状态修改写入LangGraph checkpoint。
    注意：get_state() 返回的 values 是快照，直接修改不会持久化，
    必须通过 update_state 写回，否则下一次读取会丢失修改。
    """
    try:
        workflow = active_workflows.get(session_id)
        if workflow is None:
            if session_id not in active_workflow_configs:
                return False
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
        config = {"configurable": {"thread_id": session_id}}
        workflow.app.update_state(config, updates)
        return True
    except Exception as e:
        logger.warning(f"工作流状态写入checkpoint失败（非致命）: {e}")
        return False


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
                l3_full = l3.get("score", 0)
                l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                actual = score_map.get(l3_id, {}).get("actual_score", l3_full)
                rate = round(actual / l3_full * 100, 1) if l3_full > 0 else 0
                l2_full += l3_full
                l2_score += actual
                sub_rows.append(f"| {l1_name} | {l2_name} | {l3_name} | {l3_full} | {actual} | {rate}% |")
            for sr in sub_rows:
                rows.append(sr)
            l2_rate = round(l2_score / l2_full * 100, 1) if l2_full > 0 else 0
            rows.append(f"| | **{l2_name} 小计** | | **{l2_full}** | **{l2_score}** | **{l2_rate}%** |")

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


@router.post("/workflow/reorder-indicators")
async def reorder_indicators(req: ReorderRequest):
    """
    步骤6：拖拽重排序指标体系
    level1: 一级指标顺序拖动（其下所有二级、三级指标跟随移动）
    level2: 二级指标在某一级指标内拖动（其下三级指标跟随）
    level3: 三级指标在某二级指标内拖动（不能跨二级指标）
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
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
        _persist_state(req.session_id, {"indicator_system": indicator_system})

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


@router.get("/workflow/templates")
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


@router.get("/workflow/templates/{template_id}")
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


@router.post("/workflow/templates/import")
async def import_template(req: ImportTemplateRequest):
    """
    步骤3：一键导入模板指标体系到当前工作流
    """
    try:
        templates = _load_templates()
        if req.template_id not in templates:
            raise HTTPException(status_code=404, detail="模板不存在")

        workflow, values = _get_workflow_values(req.session_id)
        template = templates[req.template_id]

        # 导入模板的指标体系
        indicator_system = {
            "indicator_type": template["type"],
            "level1_indicators": template["level1_indicators"],
            "source": f"template:{req.template_id}",
        }
        _persist_state(req.session_id, {"indicator_system": indicator_system})

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


@router.post("/workflow/templates/save")
async def save_as_template(req: SaveTemplateRequest):
    """将当前工作流的指标体系保存为新模板"""
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.get("/workflow/{session_id}/report-order")
async def get_report_order(session_id: str):
    """获取最终报告的章节顺序"""
    try:
        workflow, values = _get_workflow_values(session_id)
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


@router.post("/workflow/report-order")
async def set_report_order(req: ReportOrderRequest):
    """设置最终报告的自定义章节顺序"""
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.post("/workflow/assemble-report")
async def assemble_final_report(req: AssembleReportRequest):
    """
    步骤14：按自定义顺序组装最终报告
    支持选择性地包含某些章节
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
        project_name = values.get("project_name", "未命名项目")

        # 使用自定义顺序或默认顺序
        custom_order = values.get("report_section_order")
        if custom_order:
            section_map = {s["key"]: s for s in custom_order}
        else:
            section_map = {k: {"key": k, "name": v, "enabled": True} for k, v in REPORT_SECTION_ORDER.items()}

        # 按顺序组装
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

        _persist_state(req.session_id, {"final_report": full_report})
        return {"success": True, "data": {"report": full_report, "section_count": len(report_sections)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"组装报告失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/{session_id}/format-check")
async def check_report_format(session_id: str):
    """
    步骤14：依据396号文要求进行格式校对
    检查最终报告的格式规范性和内容完整性
    """
    try:
        workflow, values = _get_workflow_values(session_id)
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


@router.post("/workflow/add-custom-module")
async def add_custom_module(req: AddCustomModuleRequest):
    """
    步骤12：添加自定义模块（除默认5个模块外）
    操作者可根据项目需要新增模块内容
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.post("/workflow/delete-module")
async def delete_module(req: DeleteModuleRequest):
    """步骤12：删除自定义模块"""
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.post("/workflow/update-module-content")
async def update_module_content(req: AddCustomModuleRequest):
    """
    步骤12：更新已有模块内容（支持默认5模块+自定义模块）
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.post("/workflow/export-style")
async def set_export_style(req: ExportStyleRequest):
    """
    设置导出PDF/Word文档的自定义样式
    支持的字体：SimSun(宋体), SimHei(黑体), KaiTi(楷体), FangSong(仿宋)
    """
    try:
        workflow, values = _get_workflow_values(req.session_id)
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


@router.get("/workflow/{session_id}/export-style")
async def get_export_style(session_id: str):
    """获取当前的导出样式设置"""
    try:
        workflow, values = _get_workflow_values(session_id)
        style = values.get("export_style", {
            "font_family": "SimSun", "font_size": 12,
            "line_spacing": 1.5, "page_margin": 2.5,
        })
        return {"success": True, "data": style}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))