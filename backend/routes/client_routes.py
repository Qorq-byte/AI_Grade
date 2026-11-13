"""
客户端功能API路由
头像上传 / 用户名修改 / 注销账户 / AI聊天 / AI对话管理 / AI聊天开关
"""
import os
import json
import uuid
import shutil
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session, joinedload
from loguru import logger
from pathlib import Path

from backend.database import get_db
from backend.middleware.auth_middleware import get_current_user
from backend.models.user_models import (
    User, OperationLog, AiConversation, AiMessage,
    Project, ProjectFile, Report, WorkflowSession,
    QuestionnaireAnswer, TokenUsage, PromptTemplate, Notification,
)
from backend.models.memory_models import (
    UserSession, ShortTermMemory, LongTermMemory, ConversationSummary,
)
from backend.config import settings
from backend.routes import _shared_state
from backend.services import chat_context
from backend.utils.time_utils import utc_iso, utcnow

router = APIRouter(prefix="/api/client", tags=["客户端功能"])

AVATARS_DIR = Path(__file__).parent.parent.parent / "frontend" / "avatars"
AVATARS_DIR.mkdir(parents=True, exist_ok=True)

# ── 模块级缓存的 OpenAI 客户端（复用，避免每次请求重建）──
_openai_client = None
_deepseek_client = None


def _get_openai_client(model_name: str = None):
    """根据模型名称返回对应的LLM客户端，支持通义千问和DeepSeek"""
    global _openai_client, _deepseek_client
    from backend.utils.llm_helper import get_model_provider

    provider = get_model_provider(model_name) if model_name else "qwen"

    # DeepSeek 系列模型
    if provider == "deepseek":
        if _deepseek_client is None:
            if not settings.DEEPSEEK_API_KEY or settings.DEEPSEEK_API_KEY.startswith("your_"):
                raise HTTPException(status_code=500, detail="DeepSeek API密钥未配置，请在.env中设置DEEPSEEK_API_KEY")
            from openai import OpenAI
            _deepseek_client = OpenAI(
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL,
            )
        return _deepseek_client

    # 默认使用通义千问
    if _openai_client is None:
        if not settings.DASHSCOPE_API_KEY or settings.DASHSCOPE_API_KEY.startswith("your_"):
            raise HTTPException(status_code=500, detail="通义千问API密钥未配置，请在.env中设置DASHSCOPE_API_KEY")
        from openai import OpenAI
        _openai_client = OpenAI(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    return _openai_client


def _get_or_create_conversation(conversation_id, user_id, db):
    """获取已有对话或创建新对话，返回 (conv, conversation_id)"""
    if conversation_id:
        conv = db.query(AiConversation).filter(AiConversation.id == conversation_id, AiConversation.user_id == user_id).first()
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")
        return conv, conversation_id
    conv = AiConversation(user_id=user_id, title="新对话")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv, conv.id


BASE_SYSTEM_PROMPT = "你是云海睿评的AI助手，专门帮助用户进行绩效评价相关的工作。请用中文回答。"


def _build_chat_messages(conversation_id, db, context_text: str = "") -> list:
    """从 DB 取历史、注入报告上下文，再压进预算（同步/流式共用）。"""
    system = BASE_SYSTEM_PROMPT
    if context_text:
        system = f"{BASE_SYSTEM_PROMPT}\n\n{context_text}"
    db_messages = db.query(AiMessage).filter(
        AiMessage.conversation_id == conversation_id
    ).order_by(AiMessage.created_at).all()
    messages = [{"role": "system", "content": system}]
    messages += [{"role": m.role, "content": m.content} for m in db_messages]
    return chat_context.trim_history(messages)


def _chat_error_detail(exc: Exception) -> tuple:
    """把上游异常翻成 (状态码, 可读提示)。同步与流式共用一份口径。"""
    error_msg = str(exc).lower()
    if "quota" in error_msg or "free tier" in error_msg or "freetier" in error_msg or "free quota" in error_msg:
        return 503, "AI服务配额已用完，请充值或更换模型"
    if "api_key" in error_msg or "authentication" in error_msg or "unauthorized" in error_msg:
        return 500, "AI服务认证失败，请检查API密钥配置"
    if "rate" in error_msg and "limit" in error_msg:
        return 429, "AI服务请求过于频繁，请稍后重试"
    if "model" in error_msg and ("not" in error_msg or "invalid" in error_msg):
        return 500, "AI模型配置异常，请联系管理员"
    return 500, "AI服务暂时不可用，请稍后重试"


def _resolve_chat_context(req, current_user, db) -> dict:
    """取要引用的那份报告：显式 id 优先（校验归属），否则取最近一份已完成的。"""
    from backend.agents.performance_agent import PerformanceAgent

    record = None
    if req.workflow_session_id:
        from backend.routes._workflow_deps import _require_workflow

        _require_workflow(req.workflow_session_id, current_user, db)
        record = db.query(WorkflowSession).filter(
            WorkflowSession.session_id == req.workflow_session_id).first()
    else:
        record = (db.query(WorkflowSession)
                  .filter(WorkflowSession.user_id == current_user.id,
                          WorkflowSession.status == "completed")
                  .order_by(WorkflowSession.updated_at.desc())
                  .first())
    if record is None:
        return {"text": "", "references": []}
    values = PerformanceAgent.load_state_backup(record.session_id) or {}
    return chat_context.build_report_context(values, record.project_name or "",
                                               query=req.message or "")


def _call_ai_chat(conversation_id, db, model_name: str = None, context_text: str = ""):
    """调用 AI 聊天 API 并返回回复文本和token使用量"""
    used_model = model_name or settings.DEFAULT_MODEL
    client = _get_openai_client(used_model)
    openai_messages = _build_chat_messages(conversation_id, db, context_text)
    response = client.chat.completions.create(
        model=used_model,
        messages=openai_messages,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
        max_tokens=settings.MAX_TOKENS,
    )
    reply = response.choices[0].message.content
    # 提取token使用量
    usage = getattr(response, 'usage', None)
    token_info = {
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "total_tokens": usage.total_tokens if usage else 0,
    }
    return reply, token_info, used_model


def _get_ai_chat_enabled(db: Session) -> bool:
    from backend.models.user_models import ModelConfig
    cfg = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
    if cfg and cfg.api_base_url:
        try:
            saved = json.loads(cfg.api_base_url)
            return saved.get("ai_chat_enabled", True)
        except Exception:
            pass
    return True


class UpdateProfileRequest(BaseModel):
    username: str


class AiChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None
    model: Optional[str] = None
    #: 要引用哪份报告。空 = 取该用户最近一份已完成的报告；传了就必须是本人的
    workflow_session_id: Optional[str] = None


class CreateConversationRequest(BaseModel):
    title: str = "新对话"


class UpdateConversationTitleRequest(BaseModel):
    title: str


@router.get("/me")
def get_client_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from backend.models.user_models import Report, WorkflowSession
    from sqlalchemy import func, case
    # Combine two WorkflowSession count queries into one using conditional aggregation
    ws_counts = db.query(
        func.count(WorkflowSession.id).label("total"),
        func.count(case((WorkflowSession.status == "completed", 1))).label("completed")
    ).filter(WorkflowSession.user_id == current_user.id).first()
    project_count = ws_counts.total
    completed_workflows = ws_counts.completed or 0
    report_count = db.query(Report).filter(Report.user_id == current_user.id).count()
    ai_dialog_count = db.query(AiConversation).filter(AiConversation.user_id == current_user.id).count()
    return {
        "success": True,
        "data": {
            "user_id": current_user.id,
            "username": current_user.username,
            "role": current_user.role.value,
            "phone": current_user.phone,
            "avatar_url": current_user.avatar_url,
            "is_active": current_user.is_active,
            "tokens_used": current_user.tokens_used,
            "created_at": utc_iso(current_user.created_at),
            "project_count": project_count,
            "completed_workflows": completed_workflows,
            "report_count": report_count,
            "ai_dialog_count": ai_dialog_count,
        }
    }


@router.put("/profile")
def update_profile(req: UpdateProfileRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not req.username or not req.username.strip():
        raise HTTPException(status_code=400, detail="用户名不能为空")
    username = req.username.strip()
    if len(username) < 2 or len(username) > 50:
        raise HTTPException(status_code=400, detail="用户名长度应在2-50个字符之间")
    existing = db.query(User).filter(User.username == username, User.id != current_user.id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"用户名 '{username}' 已被占用")
    old_name = current_user.username
    current_user.username = username
    current_user.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(current_user)
    _log_operation(db, current_user.id, username, "update_profile", resource_type="user", resource_id=str(current_user.id), detail=f"用户名从 '{old_name}' 改为 '{username}'")
    return {
        "success": True,
        "message": "用户名修改成功",
        "data": {
            "user_id": current_user.id,
            "username": current_user.username,
            "avatar_url": current_user.avatar_url,
            "role": current_user.role.value,
        }
    }


@router.get("/token-stats")
def get_my_token_stats(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取当前用户的Token消耗统计"""
    try:
        from backend.services.token_service import get_user_token_usage
        stats = get_user_token_usage(db, current_user.id)
        return {"success": True, "data": stats}
    except Exception as e:
        logger.error(f"获取Token统计失败: {e}")
        return {"success": True, "data": {"total_tokens": current_user.tokens_used or 0, "daily_usage": [], "by_model": []}}


@router.post("/avatar")
async def upload_avatar(file: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片格式")
    if file.size and file.size > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片大小不能超过5MB")
    ext = os.path.splitext(file.filename or "avatar.png")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    filename = f"{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = AVATARS_DIR / filename
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    relative_url = f"/static/avatars/{filename}"
    current_user.avatar_url = relative_url
    current_user.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(current_user)
    return {
        "success": True,
        "message": "头像上传成功",
        "data": {"avatar_url": relative_url}
    }


@router.delete("/account")
def delete_account(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """注销账户 — 级联删除用户所有数据"""
    username = current_user.username
    user_id = current_user.id
    logger.info(f"开始注销用户账号: username={username}, user_id={user_id}")

    # 1. 删除用户上传的文件（头像等）
    if current_user.avatar_url:
        avatar_path = Path(__file__).parent.parent.parent / "frontend" / current_user.avatar_url.lstrip("/")
        if avatar_path.exists():
            avatar_path.unlink()

    # 2. 删除项目的上传文件（从文件系统清理）
    projects = db.query(Project).filter(Project.user_id == user_id).all()
    for proj in projects:
        if proj.uploaded_files:
            try:
                files = json.loads(proj.uploaded_files) if isinstance(proj.uploaded_files, str) else proj.uploaded_files
                for f in (files or []):
                    fname = f.get("file_path") or f.get("path") or f.get("name", "")
                    if fname:
                        fpath = Path(__file__).parent.parent.parent / "uploads" / Path(fname).name
                        if fpath.exists():
                            fpath.unlink()
            except Exception:
                pass

    # 3. 删除项目关联文件记录
    db.query(ProjectFile).filter(ProjectFile.project_id.in_(
        db.query(Project.id).filter(Project.user_id == user_id).subquery()
    )).delete(synchronize_session=False)

    # 4. 删除项目文件（file_processor上传路径）
    upload_dir = Path(__file__).parent.parent.parent / "uploads"
    for proj in projects:
        # 删除与项目相关的所有上传文件
        for f in upload_dir.glob(f"*_{user_id}_*"):
            try:
                f.unlink()
            except Exception:
                pass

    # 5. 删除工作流会话（问卷答案、报告等关联数据）
    sessions = db.query(WorkflowSession).filter(WorkflowSession.user_id == user_id).all()
    for sess in sessions:
        db.query(QuestionnaireAnswer).filter(QuestionnaireAnswer.session_id == sess.session_id).delete(synchronize_session=False)
    db.query(WorkflowSession).filter(WorkflowSession.user_id == user_id).delete(synchronize_session=False)

    # 6. 删除AI聊天数据
    conversations = db.query(AiConversation).filter(AiConversation.user_id == user_id).all()
    for conv in conversations:
        db.query(AiMessage).filter(AiMessage.conversation_id == conv.id).delete(synchronize_session=False)
    db.query(AiConversation).filter(AiConversation.user_id == user_id).delete(synchronize_session=False)

    # 7. 删除Token使用记录
    db.query(TokenUsage).filter(TokenUsage.user_id == user_id).delete(synchronize_session=False)

    # 8. 删除通知记录
    db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)

    # 9. 删除操作日志
    db.query(OperationLog).filter(OperationLog.user_id == user_id).delete(synchronize_session=False)

    # 10. 删除记忆数据（通过 UserSession 的 session_id 关联）
    user_sessions = db.query(UserSession).filter(UserSession.user_id == str(user_id)).all()
    session_ids = [s.session_id for s in user_sessions]
    if session_ids:
        db.query(ShortTermMemory).filter(ShortTermMemory.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(LongTermMemory).filter(LongTermMemory.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(ConversationSummary).filter(ConversationSummary.session_id.in_(session_ids)).delete(synchronize_session=False)

    # 11. 删除报告的提示词模板（用户自定义的，非系统默认）
    db.query(PromptTemplate).filter(
        PromptTemplate.modified_by == user_id, PromptTemplate.is_system == False
    ).delete(synchronize_session=False)

    # 12. 删除项目
    db.query(Project).filter(Project.user_id == user_id).delete(synchronize_session=False)
    db.query(Report).filter(Report.user_id == user_id).delete(synchronize_session=False)
    db.query(ProjectFile).filter(ProjectFile.user_id == user_id).delete(synchronize_session=False)

    # 13. 删除用户会话
    db.query(UserSession).filter(UserSession.user_id == str(user_id)).delete(synchronize_session=False)

    # 14. 最后删除用户本身
    db.delete(current_user)
    db.commit()

    logger.info(f"用户账号及全部数据已注销: username={username}, user_id={user_id}")
    return {"success": True, "message": "账号及所有关联数据已成功注销"}


@router.post("/ai/conversations")
def create_conversation(req: CreateConversationRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conv = AiConversation(user_id=current_user.id, title=req.title or "新对话")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {
        "success": True,
        "data": {
            "id": conv.id,
            "title": conv.title,
            "created_at": utc_iso(conv.created_at),
            "updated_at": utc_iso(conv.updated_at),
        }
    }


@router.get("/ai/conversations")
def list_conversations(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import func
    conversations = db.query(AiConversation).filter(AiConversation.user_id == current_user.id).order_by(AiConversation.updated_at.desc()).all()
    conv_ids = [c.id for c in conversations]
    msg_counts = {}
    if conv_ids:
        rows = db.query(AiMessage.conversation_id, func.count(AiMessage.id)).filter(
            AiMessage.conversation_id.in_(conv_ids)
        ).group_by(AiMessage.conversation_id).all()
        msg_counts = dict(rows)
    return {
        "success": True,
        "data": [
            {
                "id": c.id,
                "title": c.title,
                "created_at": utc_iso(c.created_at),
                "updated_at": utc_iso(c.updated_at),
                "message_count": msg_counts.get(c.id, 0),
            }
            for c in conversations
        ]
    }


@router.get("/ai/conversations/{conversation_id}")
def get_conversation(conversation_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conv = db.query(AiConversation).options(joinedload(AiConversation.messages)).filter(AiConversation.id == conversation_id, AiConversation.user_id == current_user.id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    return {
        "success": True,
        "data": {
            "id": conv.id,
            "title": conv.title,
            "created_at": utc_iso(conv.created_at),
            "updated_at": utc_iso(conv.updated_at),
            "messages": [
                {"id": m.id, "role": m.role, "content": m.content, "created_at": utc_iso(m.created_at)}
                for m in conv.messages
            ]
        }
    }


@router.put("/ai/conversations/{conversation_id}")
def update_conversation_title(conversation_id: int, req: UpdateConversationTitleRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conv = db.query(AiConversation).filter(AiConversation.id == conversation_id, AiConversation.user_id == current_user.id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    conv.title = req.title or "未命名对话"
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return {"success": True, "data": {"id": conv.id, "title": conv.title}}


@router.delete("/ai/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conv = db.query(AiConversation).filter(AiConversation.id == conversation_id, AiConversation.user_id == current_user.id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    conv_title = conv.title
    db.delete(conv)
    db.commit()
    return {"success": True, "message": f"对话 '{conv_title}' 已删除"}


@router.post("/ai/chat")
async def ai_chat(req: AiChatRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _get_ai_chat_enabled(db):
        raise HTTPException(status_code=403, detail="AI聊天功能已被管理员禁用")
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 用户问「我这份报告扣分点在哪」时，模型得知道是哪份报告（批次 38）
    context = _resolve_chat_context(req, current_user, db)
    conv, conversation_id = _get_or_create_conversation(req.conversation_id, current_user.id, db)

    user_msg = AiMessage(conversation_id=conversation_id, role="user", content=req.message)
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    try:
        reply, token_info, used_model = _call_ai_chat(
            conversation_id, db, req.model, context_text=context["text"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI聊天请求失败: {e}")
        # 返回友好的错误消息，不暴露原始错误细节（可能含敏感关键字）
        status, detail = _chat_error_detail(e)
        raise HTTPException(status_code=status, detail=detail)

    assistant_msg = AiMessage(conversation_id=conversation_id, role="assistant", content=reply)
    db.add(assistant_msg)

    if conv.title == "新对话":
        title = req.message[:20].replace("\n", " ").strip() or "新对话"
        conv.title = title
    conv.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(assistant_msg)

    # 记录token消耗
    if token_info["total_tokens"] > 0:
        try:
            from backend.services.token_service import record_token_usage
            record_token_usage(
                db, current_user.id,
                model_name=used_model,
                prompt_tokens=token_info["prompt_tokens"],
                completion_tokens=token_info["completion_tokens"],
                total_tokens=token_info["total_tokens"],
            )
        except Exception as te:
            logger.warning(f"AI聊天Token记录失败: {te}")

    return {
        "success": True,
        "data": {
            "conversation_id": conversation_id,
            "conversation_title": conv.title,
            "reply": reply,
            "model": used_model,
            "user_message_id": user_msg.id,
            "assistant_message_id": assistant_msg.id,
            "references": context["references"],
        }
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_completion(messages, model_name: str, usage_out: dict):
    """同步生成器：逐段产文本。usage 写进 usage_out（部分供应商不返回）。"""
    client = _get_openai_client(model_name)
    kwargs = dict(model=model_name, messages=messages, temperature=settings.TEMPERATURE,
                  top_p=settings.TOP_P, max_tokens=settings.MAX_TOKENS, stream=True)
    try:
        stream = client.chat.completions.create(**kwargs, stream_options={"include_usage": True})
    except TypeError:
        # 有些兼容端点不认 stream_options，退回普通流式
        stream = client.chat.completions.create(**kwargs)
    for chunk in stream:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            usage_out.update({
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            })
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        text = getattr(delta, "content", None) if delta is not None else None
        if text:
            yield text


@router.post("/ai/chat/stream")
async def ai_chat_stream(req: AiChatRequest, current_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """SSE 版聊天：先逐段推 delta，最后推 done（含 conversation_id 与 references）。

    历史与上下文构建与非流式共用 `_build_chat_messages`，两条路径不会给出不同的答案依据。
    """
    if not _get_ai_chat_enabled(db):
        raise HTTPException(status_code=403, detail="AI聊天功能已被管理员禁用")
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    context = _resolve_chat_context(req, current_user, db)
    conv, conversation_id = _get_or_create_conversation(req.conversation_id, current_user.id, db)
    user_msg = AiMessage(conversation_id=conversation_id, role="user", content=req.message)
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    used_model = req.model or settings.DEFAULT_MODEL
    messages = _build_chat_messages(conversation_id, db, context["text"])

    async def event_stream():
        parts, usage = [], {}
        try:
            async for piece in iterate_in_threadpool(
                    _stream_completion(messages, used_model, usage)):
                parts.append(piece)
                yield _sse("delta", {"text": piece})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AI聊天流式失败: {exc}")
            status, detail = _chat_error_detail(exc)
            yield _sse("error", {"status": status, "detail": detail})
            return

        reply = "".join(parts)
        # 生成器里不要复用请求作用域的 db：响应已经开始，依赖可能已经收尾
        from backend.database import SessionLocal
        with SessionLocal() as session:
            session.add(AiMessage(conversation_id=conversation_id, role="assistant", content=reply))
            conv_row = session.query(AiConversation).filter(
                AiConversation.id == conversation_id).first()
            if conv_row is not None:
                if conv_row.title == "新对话":
                    conv_row.title = req.message[:20].replace("\n", " ").strip() or "新对话"
                conv_row.updated_at = datetime.utcnow()
            session.commit()
            if usage.get("total_tokens"):
                try:
                    from backend.services.token_service import record_token_usage
                    record_token_usage(session, current_user.id, model_name=used_model, **usage)
                except Exception as te:  # noqa: BLE001
                    logger.warning(f"AI聊天Token记录失败: {te}")
        yield _sse("done", {"conversation_id": conversation_id, "reply": reply,
                            "model": used_model, "references": context["references"]})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/ai/voice-chat")
async def ai_voice_chat(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _get_ai_chat_enabled(db):
        raise HTTPException(status_code=403, detail="AI聊天功能已被管理员禁用")
    form = await request.form()
    text = form.get("text", "")
    conversation_id_str = form.get("conversation_id", "0")
    conversation_id = int(conversation_id_str) if conversation_id_str and conversation_id_str != "0" else None
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="语音识别文本不能为空")

    conv, conversation_id = _get_or_create_conversation(conversation_id, current_user.id, db)

    user_msg = AiMessage(conversation_id=conversation_id, role="user", content=text)
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    model_name = form.get("model", "") or None
    try:
        reply, token_info, used_model = _call_ai_chat(conversation_id, db, model_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI语音聊天请求失败: {e}")
        error_msg = str(e).lower()
        if "quota" in error_msg or "free tier" in error_msg or "freetier" in error_msg or "free quota" in error_msg:
            raise HTTPException(status_code=503, detail="AI服务配额已用完，请充值或更换模型")
        if "api_key" in error_msg or "authentication" in error_msg or "unauthorized" in error_msg:
            raise HTTPException(status_code=500, detail="AI服务认证失败，请检查API密钥配置")
        if "rate" in error_msg and "limit" in error_msg:
            raise HTTPException(status_code=429, detail="AI服务请求过于频繁，请稍后重试")
        raise HTTPException(status_code=500, detail="AI服务暂时不可用，请稍后重试")

    assistant_msg = AiMessage(conversation_id=conversation_id, role="assistant", content=reply)
    db.add(assistant_msg)

    if conv.title == "新对话":
        title = text[:20].replace("\n", " ").strip() or "新对话"
        conv.title = title
    conv.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(assistant_msg)

    # 记录token消耗
    if token_info["total_tokens"] > 0:
        try:
            from backend.services.token_service import record_token_usage
            record_token_usage(
                db, current_user.id,
                model_name=used_model,
                prompt_tokens=token_info["prompt_tokens"],
                completion_tokens=token_info["completion_tokens"],
                total_tokens=token_info["total_tokens"],
            )
        except Exception as te:
            logger.warning(f"AI语音聊天Token记录失败: {te}")

    return {
        "success": True,
        "data": {
            "conversation_id": conversation_id,
            "conversation_title": conv.title,
            "reply": reply,
            "model": used_model,
        }
    }


@router.get("/ai/chat-status")
def get_ai_chat_status(db: Session = Depends(get_db)):
    return {"success": True, "data": {"enabled": _get_ai_chat_enabled(db)}}


@router.get("/ai/models")
def get_available_models(current_user: User = Depends(get_current_user)):
    """获取可用的AI模型列表，根据API密钥配置情况过滤"""
    from backend.utils.llm_helper import get_supported_models
    all_models = get_supported_models()
    available = []
    for m in all_models:
        cat = m.get("category")
        if cat == "qwen":
            if settings.DASHSCOPE_API_KEY and not settings.DASHSCOPE_API_KEY.startswith("your_"):
                available.append({**m, "configured": True})
        elif cat == "deepseek":
            if settings.DEEPSEEK_API_KEY and not settings.DEEPSEEK_API_KEY.startswith("your_"):
                available.append({**m, "configured": True})
        elif cat == "openai":
            if settings.OPENAI_API_KEY and not settings.OPENAI_API_KEY.startswith("your_"):
                available.append({**m, "configured": True})
        elif cat == "anthropic":
            if settings.ANTHROPIC_API_KEY and not settings.ANTHROPIC_API_KEY.startswith("your_"):
                available.append({**m, "configured": True})
    return {
        "success": True,
        "data": {
            "models": available,
            "default_model": settings.DEFAULT_MODEL,
        }
    }


@router.get("/dashboard")
def get_client_dashboard(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """客户端仪表盘数据"""
    from backend.models.user_models import WorkflowSession, Report
    completed_workflows = db.query(WorkflowSession).filter(
        WorkflowSession.user_id == current_user.id,
        WorkflowSession.status == "completed"
    ).count()
    total_reports = db.query(Report).filter(Report.user_id == current_user.id).count()
    ai_dialog_count = db.query(AiConversation).filter(AiConversation.user_id == current_user.id).count()
    return {
        "success": True,
        "data": {
            "completed_workflows": completed_workflows,
            "total_reports": total_reports,
            "tokens_used": current_user.tokens_used or 0,
            "ai_dialog_count": ai_dialog_count,
            "username": current_user.username,
        }
    }


@router.get("/activity")
def get_recent_activity(current_user: User = Depends(get_current_user), db: Session = Depends(get_db), limit: int = 10):
    logs = db.query(OperationLog).filter(
        OperationLog.user_id == current_user.id
    ).order_by(OperationLog.created_at.desc()).limit(limit).all()
    return {
        "success": True,
        "data": [
            {
                "action": log.action,
                "description": log.detail or log.action,
                "time": log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "",
            }
            for log in logs
        ]
    }


@router.get("/reports")
def get_user_reports(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 从数据库查询已持久化的 WorkflowSession 记录
    from backend.models.user_models import WorkflowSession
    records = db.query(WorkflowSession).filter(
        WorkflowSession.user_id == current_user.id
    ).order_by(WorkflowSession.updated_at.desc()).all()

    status_map = {"completed": "已完成", "running": "运行中", "awaiting_approval": "待审批", "cancelled": "已取消"}
    from backend.services import quality

    result = []
    for r in records:
        # 质量结论现算（读磁盘投影，按 (路径, mtime, 大小) 缓存）。
        # 读不到备份 / 解析失败一律返回 unknown，不在这里抛 —— 一份坏掉的备份
        # 不该让「我的报告」整页 500（`check_session` 内部已 fail-open）。
        try:
            review = quality.review_brief(quality.check_session(r.session_id))
        except Exception:
            logger.exception("读取报告质量结论失败: {}", r.session_id)
            review = {"checked": False, "verdict": "unknown", "required": False, "reasons": []}
        result.append({
            "session_id": r.session_id,
            "project_name": r.project_name,
            "status": status_map.get(r.status, r.status or "已完成"),
            "created_at": utc_iso(r.created_at) if r.created_at else "",
            "updated_at": utc_iso(r.updated_at) if r.updated_at else "",
            "review": review,
        })

    return {"success": True, "data": result}


def _delete_report_records(session_id: str, user_id: int, db: Session) -> None:
    from backend.models.memory_models import ShortTermMemory, LongTermMemory, ConversationSummary
    from backend.routes._shared_state import _validate_session_id

    _validate_session_id(session_id)
    record = db.query(WorkflowSession).filter(
        WorkflowSession.session_id == session_id, WorkflowSession.user_id == user_id,
    ).first()
    if record is None or record.session_id != session_id:
        raise HTTPException(status_code=404, detail="报告不存在")
    if session_id in _shared_state._WORKFLOW_PROGRESS:
        raise HTTPException(status_code=409, detail="工作流正在执行")
    for model in (ShortTermMemory, LongTermMemory, ConversationSummary, QuestionnaireAnswer):
        db.query(model).filter(model.session_id == session_id).delete(synchronize_session=False)
    db.delete(record)


def _remove_report_snapshot(session_id: str) -> None:
    from backend.agents import performance_agent

    workflow = _shared_state.active_workflows.pop(session_id, None)
    _shared_state.active_workflow_configs.pop(session_id, None)
    _shared_state._QUESTIONNAIRE_CACHE.pop(session_id, None)
    if workflow:
        try:
            workflow.close()
        except Exception:
            logger.exception("关闭已删除的工作流失败: {}", session_id)
    try:
        performance_agent.PerformanceAgent.delete_state_backup(session_id)
    except OSError:
        logger.exception("删除报告备份失败: {}", session_id)


@router.delete("/reports/{session_id}")
def delete_report(session_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _delete_report_records(session_id, current_user.id, db)
    db.commit()
    _remove_report_snapshot(session_id)
    return {"success": True, "message": "报告已删除"}


class BatchDeleteRequest(BaseModel):
    session_ids: list[str]


@router.post("/reports/batch-delete")
def batch_delete_reports(req: BatchDeleteRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    deleted = []
    errors = []
    for session_id in dict.fromkeys(req.session_ids):
        try:
            _delete_report_records(session_id, current_user.id, db)
        except HTTPException as exc:
            errors.append(f"{session_id}: {exc.detail}")
        else:
            deleted.append(session_id)

    db.commit()
    for session_id in deleted:
        _remove_report_snapshot(session_id)
    return {"success": True, "data": {"deleted": len(deleted), "errors": errors},
            "message": f"成功删除 {len(deleted)} 条记录"}


from backend.utils.log_utils import log_operation as _log_operation


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.put("/password")
def change_password(req: ChangePasswordRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from backend.services.auth_service import verify_password, hash_password

    if not req.old_password or not req.new_password:
        raise HTTPException(status_code=400, detail="旧密码和新密码不能为空")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于6位")

    stored_value = current_user.password_hash
    if ":" not in stored_value:
        raise HTTPException(status_code=500, detail="密码格式错误")

    salt, stored_hash = stored_value.split(":", 1)
    if not verify_password(req.old_password, stored_hash, salt):
        raise HTTPException(status_code=400, detail="旧密码不正确")

    new_hash, new_salt = hash_password(req.new_password)
    current_user.password_hash = f"{new_salt}:{new_hash}"
    # 改完就解除强制改密，否则用户改完还是被自己的 403 拦着（批次 24）
    current_user.must_change_password = False
    # 口令一变，之前签发的所有令牌立即作废（批次 25）。
    # 但不能把正在改密的这台设备也踢下线，所以下面补发一枚新令牌。
    current_user.token_version = int(getattr(current_user, "token_version", 0) or 0) + 1
    current_user.updated_at = utcnow()
    db.commit()

    _log_operation(db, current_user.id, current_user.username, "change_password", resource_type="user", resource_id=str(current_user.id), detail="用户修改密码")

    from backend.middleware.auth_middleware import create_access_token

    new_access = create_access_token(data={
        "user_id": current_user.id,
        "username": current_user.username,
        "role": current_user.role.value,
        "ver": int(current_user.token_version or 0),
    })
    return {
        "success": True,
        "message": "密码修改成功",
        "data": {"access_token": new_access},
    }


# ─── 通知相关 ───

@router.get("/notifications")
def get_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
    unread_only: bool = False,
):
    """获取当前用户的通知列表"""
    q = db.query(Notification).filter(Notification.user_id == current_user.id)
    if unread_only:
        q = q.filter(Notification.is_read == False)
    notifications = q.order_by(Notification.created_at.desc()).limit(limit).all()
    unread_count = db.query(Notification).filter(
        Notification.user_id == current_user.id, Notification.is_read == False
    ).count()
    return {
        "success": True,
        "data": {
            "unread_count": unread_count,
            "notifications": [
                {
                    "id": n.id,
                    "title": n.title,
                    "content": n.content,
                    "is_read": n.is_read,
                    "created_at": utc_iso(n.created_at),
                }
                for n in notifications
            ],
        }
    }


@router.put("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """标记单条通知为已读"""
    n = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == current_user.id
    ).first()
    if not n:
        raise HTTPException(status_code=404, detail="通知不存在")
    n.is_read = True
    db.commit()
    return {"success": True, "message": "已标记为已读"}


@router.put("/notifications/read-all")
def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """标记所有通知为已读"""
    db.query(Notification).filter(
        Notification.user_id == current_user.id, Notification.is_read == False
    ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return {"success": True, "message": "所有通知已标记为已读"}


@router.delete("/notifications/{notification_id}")
def delete_notification(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除单条通知 — 仅能删除本人的通知"""
    n = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == current_user.id
    ).first()
    if not n:
        raise HTTPException(status_code=404, detail="通知不存在")
    db.delete(n)
    db.commit()
    return {"success": True, "message": "通知已删除"}


@router.delete("/notifications")
def clear_all_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """清空当前用户的所有通知"""
    count = db.query(Notification).filter(
        Notification.user_id == current_user.id
    ).delete(synchronize_session=False)
    db.commit()
    return {"success": True, "message": f"已清空 {count} 条通知"}