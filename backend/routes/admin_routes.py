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

    changed = []
    if req.username and req.username != user.username:
        existing = db.query(User).filter(User.username == req.username).first()
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")
        user.username = req.username
        changed.append("username")
    if req.phone is not None:
        user.phone = req.phone
        changed.append("phone")
    if req.is_active is not None:
        user.is_active = req.is_active
        changed.append(f"is_active={req.is_active}")
    if req.password:
        # 修改密码
        password_hash, salt = hash_password(req.password)
        user.password_hash = f"{salt}:{password_hash}"
        changed.append("password")

    db.commit()
    db.refresh(user)

    _log_operation(
        db, user_id=user_id, username=user.username,
        action="admin_update_user", resource_type="user",
        detail=f"管理员更新用户 {user.username}: {', '.join(changed)}",
    )
    return {"success": True, "message": f"用户 {user_id} 已更新: {', '.join(changed)}"}


@router.delete("/users/{user_id}")
def admin_delete_user(user_id: int, db: Session = Depends(get_db)):
    """管理员删除用户（软删除 — 禁用账户）"""
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == UserRole.ADMIN:
        raise HTTPException(status_code=400, detail="不能删除管理员账户")

    username = user.username
    user.is_active = False
    db.commit()

    _log_operation(
        db, user_id=user_id, username=username,
        action="admin_delete_user", resource_type="user",
        detail=f"管理员禁用用户 {username}",
    )
    return {"success": True, "message": f"用户 '{username}' 已被禁用"}


@router.post("/users/{user_id}/deactivate")
def admin_deactivate_user(user_id: int, db: Session = Depends(get_db)):
    """管理员注销用户（硬删除 — 彻底删除用户及所有关联数据）"""
    username = ""
    session_ids = []
    try:
        user = get_user_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        if user.role == UserRole.ADMIN:
            raise HTTPException(status_code=400, detail="不能注销管理员账户")

        username = user.username

        # 提前获取session_ids
        try:
            session_ids = [
                s[0] for s in db.query(WorkflowSession.session_id)
                .filter(WorkflowSession.user_id == user_id).all()
            ]
        except Exception:
            session_ids = []

        # 提前获取对话ID
        conv_ids = []
        try:
            conv_ids = [
                c[0] for c in db.query(AiConversation.id)
                .filter(AiConversation.user_id == user_id).all()
            ]
        except Exception:
            conv_ids = []

        # ── 按依赖顺序逐表删除（使用ORM，失败不回滚整个事务） ──

        # 1. AiMessage → AiConversation（外键依赖）
        if conv_ids:
            db.query(AiMessage).filter(AiMessage.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        db.query(AiConversation).filter(AiConversation.user_id == user_id).delete(synchronize_session=False)

        # 2. Notification, TokenUsage, Report（都有 user_id）
        db.query(Notification).filter(Notification.user_id == user_id).delete()
        db.query(TokenUsage).filter(TokenUsage.user_id == user_id).delete()
        db.query(Report).filter(Report.user_id == user_id).delete()

        # 3. QuestionnaireAnswer（没有 user_id，用 session_id 关联）
        if session_ids and HAS_QUESTIONNAIRE_TABLES:
            try:
                db.query(QuestionnaireAnswer).filter(
                    QuestionnaireAnswer.session_id.in_(session_ids)
                ).delete(synchronize_session=False)
            except Exception as e:
                logger.warning("删除问卷答案失败: {}", str(e))

        # 4. 记忆表（FK → user_sessions.session_id）
        if session_ids and HAS_MEMORY_TABLES:
            for cls in [ShortTermMemory, LongTermMemory, ConversationSummary]:
                try:
                    db.query(cls).filter(cls.session_id.in_(session_ids)).delete(synchronize_session=False)
                except Exception:
                    pass

        # 5. WorkflowSession
        db.query(WorkflowSession).filter(WorkflowSession.user_id == user_id).delete(synchronize_session=False)

        # 6. OperationLog（FK → users.id，必须在User前删除）
        db.query(OperationLog).filter(OperationLog.user_id == user_id).delete()

        # 7. User（ORM cascade → Project → Report/ProjectFile）
        db.delete(user)
        db.commit()
        logger.info("用户注销成功: {} (id={})", username, user_id)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("注销用户 {} (id={}) 失败: {}", username, user_id, str(e))
        raise HTTPException(status_code=500, detail=f"注销失败: {str(e)}")

    # ── 提交后清理 ──

    # 磁盘备份
    try:
        backup_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "state_backups"
        )
        for sid in session_ids:
            f = os.path.join(backup_dir, f"{sid}.json")
            if os.path.exists(f):
                os.remove(f)
    except Exception:
        pass

    # 问卷缓存
    try:
        from backend.routes.agent_routes import _QUESTIONNAIRE_CACHE, active_workflows, _WORKFLOW_PROGRESS
        for sid in session_ids:
            _QUESTIONNAIRE_CACHE.pop(sid, None)
            if sid in active_workflows:
                try: active_workflows[sid].close()
                except Exception: pass
                active_workflows.pop(sid, None)
            _WORKFLOW_PROGRESS.pop(sid, None)
    except Exception:
        pass

    # 操作日志
    try:
        _log_operation(
            db, user_id=user_id, username=username,
            action="admin_deactivate_user", resource_type="user",
            detail=f"管理员注销并删除用户 {username} 及其所有关联数据",
        )
    except Exception:
        pass

    return {"success": True, "message": f"用户 '{username}' 已注销，所有关联数据已删除"}


# ═══════════════════════════════════════════════════════
#  审计日志
# ═══════════════════════════════════════════════════════

@router.get("/audit-logs")
def admin_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """管理员查看审计日志（分页 + 筛选）"""
    q = db.query(OperationLog)

    if action:
        q = q.filter(OperationLog.action == action)
    if user_id:
        q = q.filter(OperationLog.user_id == user_id)
    if start_date:
        try:
            sd = datetime.fromisoformat(start_date)
            q = q.filter(OperationLog.created_at >= sd)
        except ValueError:
            pass
    if end_date:
        try:
            ed = datetime.fromisoformat(end_date)
            q = q.filter(OperationLog.created_at <= ed)
        except ValueError:
            pass

    total = q.count()
    logs = q.order_by(OperationLog.created_at.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()

    return {
        "success": True,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "logs": [
                {
                    "id": l.id,
                    "user_id": l.user_id,
                    "username": l.username,
                    "action": l.action,
                    "resource_type": l.resource_type,
                    "resource_id": l.resource_id,
                    "detail": l.detail,
                    "ip_address": l.ip_address,
                    "created_at": l.created_at.isoformat(),
                }
                for l in logs
            ],
        }
    }


@router.get("/audit-logs/summary")
def admin_audit_summary(db: Session = Depends(get_db)):
    """审计日志摘要 — 按操作类型聚合"""
    results = db.query(
        OperationLog.action,
        func.count(OperationLog.id).label("count"),
        func.max(OperationLog.created_at).label("last_time"),
    ).group_by(OperationLog.action).order_by(func.count(OperationLog.id).desc()).all()

    return {
        "success": True,
        "data": [
            {
                "action": r.action,
                "count": r.count,
                "last_time": r.last_time.isoformat() if r.last_time else None,
            }
            for r in results
        ]
    }


# ═══════════════════════════════════════════════════════
#  工作流管理
# ═══════════════════════════════════════════════════════

@router.get("/workflows")
def admin_list_workflows(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """管理员查看所有工作流"""
    q = db.query(WorkflowSession)
    if status:
        q = q.filter(WorkflowSession.status == status)

