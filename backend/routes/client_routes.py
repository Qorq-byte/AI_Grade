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

