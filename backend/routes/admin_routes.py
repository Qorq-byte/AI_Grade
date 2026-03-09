"""
Admin 管理路由
仪表盘统计 / 用户管理 / 操作日志审计 / 系统配置
所有端点需要管理员权限
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from loguru import logger
import os
import json

from backend.database import get_db
from backend.models.user_models import (
    User, UserRole, WorkflowSession, OperationLog,
    ModelConfig, PromptTemplate, KnowledgeBase,
    AiConversation, AiMessage, TokenUsage, Notification, Report,
    ReportModuleSequence, Project, ProjectFile,
)

# 可选模型（可能不存在）
try:
    from backend.models.user_models import QuestionnaireAnswer
    HAS_QUESTIONNAIRE_TABLES = True
except ImportError:
    HAS_QUESTIONNAIRE_TABLES = False

try:
    from backend.models.user_models import ShortTermMemory, LongTermMemory
    from backend.models.memory_models import ConversationSummary
    HAS_MEMORY_TABLES = True
except ImportError:
    try:
        from backend.models.memory_models import ShortTermMemory, LongTermMemory, ConversationSummary
        HAS_MEMORY_TABLES = True
    except ImportError:
        HAS_MEMORY_TABLES = False
from backend.middleware.auth_middleware import get_current_user, get_admin_user
from backend.services.auth_service import create_user, get_user_by_id, get_all_users, update_user_status, hash_password

router = APIRouter(
    prefix="/api/admin",
    tags=["管理后台"],
    dependencies=[Depends(get_admin_user)],  # 所有端点全部要求管理员
)


# ─── 请求模型 ───

class AdminCreateUserRequest(BaseModel):
    username: str
    password: str
    phone: Optional[str] = None
    role: str = "user"


class AdminUpdateUserRequest(BaseModel):
    username: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class AdminChangePasswordRequest(BaseModel):
    new_password: str


class DashboardOverview(BaseModel):
    total_users: int
    active_users_today: int
    total_projects: int
    active_workflows: int
    total_reports: int
    total_tokens_used: int
    recent_operations: List[dict]


# ═══════════════════════════════════════════════════════
#  仪表盘
# ═══════════════════════════════════════════════════════

@router.get("/dashboard")
def admin_dashboard(db: Session = Depends(get_db)):
    """管理员仪表盘 — 核心统计数据"""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # 用户统计
    total_users = db.query(func.count(User.id)).scalar() or 0
    active_users_today = db.query(func.count(User.id)).filter(
        User.last_login_at >= today, User.is_active == True
    ).scalar() or 0

    # 项目统计
    total_projects = db.query(func.count(WorkflowSession.id)).scalar() or 0
    active_workflows = db.query(func.count(WorkflowSession.id)).filter(
        WorkflowSession.status.in_(["created", "running", "paused"])
    ).scalar() or 0
    completed_workflows = db.query(func.count(WorkflowSession.id)).filter(
        WorkflowSession.status == "completed"
    ).scalar() or 0
    failed_workflows = db.query(func.count(WorkflowSession.id)).filter(
        WorkflowSession.status == "failed"
    ).scalar() or 0

    # Token 统计
    total_tokens = db.query(func.sum(User.tokens_used)).scalar() or 0

    # 最近操作
    recent_logs = db.query(OperationLog).order_by(
        OperationLog.created_at.desc()
    ).limit(20).all()

    return {
        "success": True,
        "data": {
            "overview": {
                "total_users": total_users,
                "active_users_today": active_users_today,
                "total_projects": total_projects,
                "active_workflows": active_workflows,
                "completed_workflows": completed_workflows,
                "failed_workflows": failed_workflows,
                "total_tokens_used": total_tokens,
            },
            "recent_operations": [
                {
                    "id": l.id,
                    "username": l.username,
                    "action": l.action,
                    "resource_type": l.resource_type,
                    "detail": l.detail,
                    "created_at": l.created_at.isoformat(),
                }
                for l in recent_logs
            ],
        }
    }


# ═══════════════════════════════════════════════════════
#  用户管理
# ═══════════════════════════════════════════════════════

@router.get("/users")
def admin_list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    role: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """管理员查看用户列表（分页 + 筛选）"""
    q = db.query(User)
    if search:
        q = q.filter(User.username.contains(search))
    if role:
        q = q.filter(User.role == role)

    total = q.count()
    users = q.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return {
        "success": True,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "phone": u.phone,
                    "role": u.role.value if isinstance(u.role, UserRole) else u.role,
                    "is_active": u.is_active,
                    "tokens_used": u.tokens_used,
                    "last_login": u.last_login_at.isoformat() if u.last_login_at else None,
                    "created_at": u.created_at.isoformat(),
                }
                for u in users
            ],
        }
    }


@router.post("/users")
def admin_create_user(req: AdminCreateUserRequest, db: Session = Depends(get_db)):
    """管理员创建新用户"""
    try:
        user = create_user(db, req.username, req.password, req.phone, role=req.role)
        _log_operation(
            db, user_id=user.id, username=req.username,
            action="admin_create_user", resource_type="user",
            detail=f"管理员创建用户 {req.username}, 角色={req.role}",
        )
        return {
            "success": True,
            "message": f"用户 '{req.username}' 创建成功",
            "data": {"user_id": user.id, "username": user.username, "role": req.role},
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/users/{user_id}")
def admin_get_user(user_id: int, db: Session = Depends(get_db)):
    """管理员查看单个用户详情"""
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 获取该用户的项目数
    project_count = db.query(func.count(WorkflowSession.id)).filter(
        WorkflowSession.user_id == user_id
    ).scalar() or 0

    # 最近操作
    recent_ops = db.query(OperationLog).filter(
        OperationLog.user_id == user_id
    ).order_by(OperationLog.created_at.desc()).limit(10).all()

    return {
        "success": True,
        "data": {
            "id": user.id,
            "username": user.username,
            "phone": user.phone,
            "password_hash": user.password_hash,
            "role": user.role.value if isinstance(user.role, UserRole) else user.role,
            "is_active": user.is_active,
            "tokens_used": user.tokens_used,
            "last_login": user.last_login_at.isoformat() if user.last_login_at else None,
            "created_at": user.created_at.isoformat(),
            "project_count": project_count,
            "recent_operations": [
                {"action": op.action, "detail": op.detail, "time": op.created_at.isoformat()}
                for op in recent_ops
            ],
        }
    }


@router.put("/users/{user_id}")
def admin_update_user(user_id: int, req: AdminUpdateUserRequest, db: Session = Depends(get_db)):
    """管理员更新用户信息"""
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

