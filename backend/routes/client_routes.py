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


def _call_ai_chat(conversation_id, db, model_name: str = None):
    """调用 AI 聊天 API 并返回回复文本和token使用量"""
    used_model = model_name or settings.DEFAULT_MODEL
    client = _get_openai_client(used_model)
    db_messages = db.query(AiMessage).filter(
        AiMessage.conversation_id == conversation_id
    ).order_by(AiMessage.created_at).all()
    openai_messages = [{"role": "system", "content": "你是云海睿评的AI助手，专门帮助用户进行绩效评价相关的工作。请用中文回答。"}]
    for m in db_messages:
        openai_messages.append({"role": m.role, "content": m.content})
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
            "created_at": current_user.created_at.isoformat(),
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
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
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
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
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
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
            "messages": [
                {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
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

    conv, conversation_id = _get_or_create_conversation(req.conversation_id, current_user.id, db)

    user_msg = AiMessage(conversation_id=conversation_id, role="user", content=req.message)
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    try:
        reply, token_info, used_model = _call_ai_chat(conversation_id, db, req.model)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI聊天请求失败: {e}")
        # 返回友好的错误消息，不暴露原始错误细节（可能含敏感关键字）
        error_msg = str(e).lower()
        if "quota" in error_msg or "free tier" in error_msg or "freetier" in error_msg or "free quota" in error_msg:
            raise HTTPException(status_code=503, detail="AI服务配额已用完，请充值或更换模型")
        if "api_key" in error_msg or "authentication" in error_msg or "unauthorized" in error_msg:
            raise HTTPException(status_code=500, detail="AI服务认证失败，请检查API密钥配置")
        if "rate" in error_msg and "limit" in error_msg:
            raise HTTPException(status_code=429, detail="AI服务请求过于频繁，请稍后重试")
        if "model" in error_msg and ("not" in error_msg or "invalid" in error_msg):
            raise HTTPException(status_code=500, detail="AI模型配置异常，请联系管理员")
        raise HTTPException(status_code=500, detail="AI服务暂时不可用，请稍后重试")

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
        }
    }

