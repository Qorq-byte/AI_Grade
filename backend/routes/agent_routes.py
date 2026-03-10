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


