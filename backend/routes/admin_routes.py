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
from backend.services.secret_box import SECRET_MASK, encrypt_secret, mask_secret
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
            # 不再返回 password_hash：哈希本身就是可离线爆破的凭据材料，
            # 前端任何地方都用不到它（批次 24 安全收尾）
            "role": user.role.value if isinstance(user.role, UserRole) else user.role,
            "is_active": user.is_active,
            "must_change_password": bool(getattr(user, "must_change_password", False)),
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
    from backend.routes.agent_routes import _WORKFLOW_PROGRESS

    q = db.query(WorkflowSession)
    if status:
        q = q.filter(WorkflowSession.status == status)

    total = q.count()
    workflows = q.order_by(WorkflowSession.created_at.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()

    return {
        "success": True,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "workflows": [
                {
                    "id": w.id,
                    "session_id": w.session_id,
                    "user_id": w.user_id,
                    "project_name": w.project_name,
                    "status": w.status,
                    "current_step": _WORKFLOW_PROGRESS.get(w.session_id, {}).get("last_step", w.current_step),
                    "model_name": w.model_name,
                    "token_used": w.token_used,
                    "created_at": w.created_at.isoformat(),
                    "completed_at": w.completed_at.isoformat() if w.completed_at else None,
                }
                for w in workflows
            ],
        }
    }


# ═══════════════════════════════════════════════════════
#  模型管理
# ═══════════════════════════════════════════════════════


class ModelConfigRequest(BaseModel):
    platform: str
    model_name: str
    model_id: Optional[str] = None
    api_key: Optional[str] = None
    api_base_url: Optional[str] = None
    is_enabled: bool = True
    supports_vision: bool = False
    supports_file: bool = False


class ModelToggleRequest(BaseModel):
    is_enabled: bool


@router.get("/models")
def admin_list_models(db: Session = Depends(get_db)):
    """获取所有模型配置

    密钥一律返回固定掩码 + has_api_key 布尔位：管理员只需要知道「配没配」，
    不需要（也不应该）看到密钥本身。旧实现返回前 8 位，等于泄露半个密钥。
    """
    models = db.query(ModelConfig).order_by(ModelConfig.sort_order).all()
    return {
        "success": True,
        "data": [
            {
                "id": m.id, "platform": m.platform, "model_name": m.model_name,
                "model_id": m.model_id, "api_key": mask_secret(m.api_key),
                "has_api_key": bool(m.api_key),
                "api_base_url": m.api_base_url, "is_enabled": m.is_enabled,
                "supports_vision": m.supports_vision, "supports_file": m.supports_file,
                "sort_order": m.sort_order, "created_at": m.created_at.isoformat(),
            }
            for m in models
        ]
    }


@router.post("/models")
def admin_create_model(req: ModelConfigRequest, db: Session = Depends(get_db)):
    """创建模型配置（密钥加密后落库）"""
    existing = db.query(ModelConfig).filter(ModelConfig.model_name == req.model_name).first()
    if existing:
        raise HTTPException(status_code=400, detail="模型名称已存在")
    max_order = db.query(func.max(ModelConfig.sort_order)).scalar() or 0
    model = ModelConfig(
        platform=req.platform, model_name=req.model_name, model_id=req.model_id or req.model_name,
        api_key=encrypt_secret(req.api_key), api_base_url=req.api_base_url, is_enabled=req.is_enabled,
        supports_vision=req.supports_vision, supports_file=req.supports_file,
        sort_order=max_order + 1,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return {"success": True, "data": {"id": model.id, "model_name": model.model_name}}


@router.put("/models/{model_id}")
def admin_update_model(model_id: int, req: ModelConfigRequest, db: Session = Depends(get_db)):
    """更新模型配置（密钥加密后落库；传空表示不改动密钥）"""
    model = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    model.platform = req.platform
    model.model_name = req.model_name
    model.model_id = req.model_id or req.model_name
    if req.api_key:
        # 前端拿回来的是掩码 `********`：那不是真密钥，不能拿它覆盖已存的密文
        if req.api_key != SECRET_MASK:
            model.api_key = encrypt_secret(req.api_key)
    model.api_base_url = req.api_base_url
    model.is_enabled = req.is_enabled
    model.supports_vision = req.supports_vision
    model.supports_file = req.supports_file
    db.commit()
    return {"success": True, "message": f"模型 '{req.model_name}' 已更新"}


@router.patch("/models/{model_id}/toggle")
def admin_toggle_model(model_id: int, req: ModelToggleRequest, db: Session = Depends(get_db)):
    """切换模型启用/禁用"""
    model = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    model.is_enabled = req.is_enabled
    db.commit()
    return {"success": True, "message": f"模型已{'启用' if req.is_enabled else '禁用'}"}


@router.delete("/models/{model_id}")
def admin_delete_model(model_id: int, db: Session = Depends(get_db)):
    """删除模型配置"""
    model = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    db.delete(model)
    db.commit()
    return {"success": True, "message": "模型已删除"}


# ═══════════════════════════════════════════════════════
#  提示词 (Prompt) 管理
# ═══════════════════════════════════════════════════════


class PromptUpdateRequest(BaseModel):
    prompt_content: str
    change_log: Optional[str] = None
    step_name: Optional[str] = None
    indicator_level: Optional[int] = None
    parent_indicator: Optional[str] = None
    dimension: Optional[str] = None


class GranularPromptRequest(BaseModel):
    step_number: int
    step_name: str
    prompt_content: str
    indicator_level: Optional[int] = None          # 1/2/3 级指标
    parent_indicator: Optional[str] = None          # 所属上级指标
    dimension: Optional[str] = None                 # 决策/过程/产出/效益
    change_log: Optional[str] = None


@router.get("/prompts")
def admin_list_prompts(
    step_number: Optional[int] = None,
    indicator_level: Optional[int] = None,
    dimension: Optional[str] = None,
    parent_indicator: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """获取所有提示词模板（支持按步骤/指标层级/维度筛选）"""
    q = db.query(PromptTemplate)
    if step_number is not None:
        q = q.filter(PromptTemplate.step_number == step_number)
    if indicator_level is not None:
        q = q.filter(PromptTemplate.indicator_level == indicator_level)
    if dimension:
        q = q.filter(PromptTemplate.dimension == dimension)
    if parent_indicator:
        q = q.filter(PromptTemplate.parent_indicator == parent_indicator)

    prompts = q.order_by(
        PromptTemplate.step_number,
        func.coalesce(PromptTemplate.indicator_level, 0),
        PromptTemplate.id,
    ).all()
    return {
        "success": True,
        "data": [
            {
                "id": p.id, "step_number": p.step_number, "step_name": p.step_name,
                "prompt_content": p.prompt_content, "version": p.version,
                "indicator_level": p.indicator_level,
                "parent_indicator": p.parent_indicator,
                "dimension": p.dimension,
                "modified_by": p.modified_by, "change_log": p.change_log,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                "is_system": p.is_system,
            }
            for p in prompts
        ]
    }


@router.post("/prompts/granular")
def admin_create_granular_prompt(
    req: GranularPromptRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """创建细粒度提示词（含指标层级/维度信息）"""
    prompt = PromptTemplate(
        step_number=req.step_number,
        step_name=req.step_name,
        prompt_content=req.prompt_content,
        indicator_level=req.indicator_level,
        parent_indicator=req.parent_indicator,
        dimension=req.dimension,
        is_system=False,
        version=1,
        modified_by=admin.id,
        change_log=req.change_log or "管理员创建",
    )
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    _log_operation(db, admin.id, admin.username, "create_granular_prompt",
                   resource_type="prompt", resource_id=str(prompt.id),
                   detail=f"创建提示词 step={req.step_number} name={req.step_name}")
    return {"success": True, "data": {"id": prompt.id, "step_name": prompt.step_name}, "message": "提示词创建成功"}


@router.put("/prompts/{prompt_id}/granular")
def admin_update_granular_prompt(
    prompt_id: int,
    req: GranularPromptRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """更新细粒度提示词"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")
    prompt.step_number = req.step_number
    prompt.step_name = req.step_name
    prompt.prompt_content = req.prompt_content
    prompt.indicator_level = req.indicator_level
    prompt.parent_indicator = req.parent_indicator
    prompt.dimension = req.dimension
    prompt.version = (prompt.version or 0) + 1
    prompt.modified_by = admin.id
    prompt.change_log = req.change_log or "管理员更新"
    prompt.updated_at = datetime.now(timezone.utc)
    db.commit()
    _log_operation(db, admin.id, admin.username, "update_granular_prompt",
                   resource_type="prompt", resource_id=str(prompt_id),
                   detail=f"更新提示词 v{prompt.version}")
    return {"success": True, "message": f"提示词 v{prompt.version} 已更新"}


@router.get("/prompts/defaults")
def admin_get_default_prompts():
    """获取所有步骤的默认Prompts（用于对比预览）"""
    from backend.agents.prompts.default_prompts import PromptTemplateFactory, STEP_META
    result = {}
    for step_number in range(15):
        meta = STEP_META.get(step_number, {})
        result[str(step_number)] = {
            "step_number": step_number,
            "step_name": meta.get("name", f"Step {step_number}"),
            "system_prompt": PromptTemplateFactory.build_raw_system_prompt(step_number),
            "human_prompt": PromptTemplateFactory.build_raw_human_prompt(step_number),
        }
    return {"success": True, "data": result}


@router.get("/prompts/{prompt_id}")
def admin_get_prompt(prompt_id: int, db: Session = Depends(get_db)):
    """获取单个提示词详情"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")
    return {
        "success": True,
        "data": {
            "id": prompt.id, "step_number": prompt.step_number,
            "step_name": prompt.step_name, "prompt_content": prompt.prompt_content,
            "version": prompt.version, "modified_by": prompt.modified_by,
            "change_log": prompt.change_log,
            "updated_at": prompt.updated_at.isoformat() if prompt.updated_at else None,
        }
    }


@router.put("/prompts/{prompt_id}")
def admin_update_prompt(
    prompt_id: int, req: PromptUpdateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """更新提示词（自动版本递增 + 变更日志）"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")
    prompt.prompt_content = req.prompt_content
    prompt.version = (prompt.version or 0) + 1
    prompt.modified_by = admin.id
    prompt.change_log = req.change_log or "管理员手动更新"
    prompt.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"success": True, "message": f"提示词 v{prompt.version} 已更新"}


@router.post("/prompts/{prompt_id}/reset")
def admin_reset_prompt(prompt_id: int, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    """重置提示词为系统默认。

    默认提示词的唯一来源是 `default_prompts.PromptTemplateFactory`
    （此前误从 `services.rag_service` 导入不存在的 `get_default_prompts`，接口必 500）。
    库内格式沿用 `PromptManager.save_to_database` 的约定：`system + "\\n\\n---\\n\\n" + human`。
    """
    from backend.agents.prompts.default_prompts import get_all_default_prompts_as_dict

    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")

    default_data = get_all_default_prompts_as_dict().get(prompt.step_number) or {}
    if not default_data.get("system_prompt") and not default_data.get("human_prompt"):
        raise HTTPException(status_code=400, detail=f"步骤 {prompt.step_number} 没有系统默认提示词")

    prompt.prompt_content = (
        f"{default_data.get('system_prompt', '')}\n\n---\n\n{default_data.get('human_prompt', '')}"
    )
    prompt.step_name = default_data.get("step_name") or prompt.step_name
    prompt.version = (prompt.version or 0) + 1
    prompt.modified_by = admin.id
    prompt.change_log = "重置为系统默认"
    prompt.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"success": True, "message": "已重置为系统默认", "data": {"version": prompt.version}}


@router.delete("/prompts/{prompt_id}")
def admin_delete_prompt(
    prompt_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """删除提示词（系统默认提示词不可删除）"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")
    if prompt.is_system:
        raise HTTPException(status_code=403, detail="系统默认提示词不可删除")
    step_name = prompt.step_name
    _log_operation(
        db, admin.id, admin.username, "delete_prompt",
        resource_type="prompt", resource_id=str(prompt_id),
        detail=f"删除提示词 step={prompt.step_number} name={step_name}",
    )
    db.delete(prompt)
    db.commit()
    return {"success": True, "message": f"提示词「{step_name}」已删除"}


# ═══════════════════════════════════════════════════════
#  知识库管理
# ═══════════════════════════════════════════════════════


class KnowledgeBaseRequest(BaseModel):
    name: str
    content: str
    category: Optional[str] = None
    step_number: Optional[int] = None
    indicator_level: Optional[int] = None
    is_active: bool = True


@router.get("/knowledge")
def admin_list_knowledge(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    step_number: Optional[int] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """获取知识库列表（分页 + 按步骤/分类筛选）"""
    q = db.query(KnowledgeBase)
    if step_number is not None:
        q = q.filter(KnowledgeBase.step_number == step_number)
    if category:
        q = q.filter(KnowledgeBase.category == category)
    total = q.count()
    items = q.order_by(KnowledgeBase.step_number, KnowledgeBase.created_at.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()
    return {
        "success": True,
        "data": {
            "total": total, "page": page, "page_size": page_size,
            "items": [
                {
                    "id": k.id, "name": k.name, "category": k.category,
                    "step_number": k.step_number, "indicator_level": k.indicator_level,
                    "content": k.content[:200] + "..." if len(k.content) > 200 else k.content,
                    "is_active": k.is_active,
                    "updated_at": k.updated_at.isoformat() if k.updated_at else None,
                }
                for k in items
            ],
        }
    }


@router.get("/knowledge/{kb_id}")
def admin_get_knowledge(kb_id: int, db: Session = Depends(get_db)):
    """获取知识库单条详情"""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库条目不存在")
    return {
        "success": True,
        "data": {
            "id": kb.id, "name": kb.name, "content": kb.content,
            "category": kb.category, "step_number": kb.step_number,
            "indicator_level": kb.indicator_level, "is_active": kb.is_active,
            "updated_at": kb.updated_at.isoformat() if kb.updated_at else None,
        }
    }


@router.post("/knowledge")
def admin_create_knowledge(req: KnowledgeBaseRequest, db: Session = Depends(get_db)):
    """创建知识库条目"""
    kb = KnowledgeBase(
        name=req.name, content=req.content, category=req.category,
        step_number=req.step_number, indicator_level=req.indicator_level,
        is_active=req.is_active,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return {"success": True, "data": {"id": kb.id, "name": kb.name}}


@router.put("/knowledge/{kb_id}")
def admin_update_knowledge(kb_id: int, req: KnowledgeBaseRequest, db: Session = Depends(get_db)):
    """更新知识库条目"""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库条目不存在")
    kb.name = req.name
    kb.content = req.content
    kb.category = req.category
    kb.step_number = req.step_number
    kb.indicator_level = req.indicator_level
    kb.is_active = req.is_active
    db.commit()
    return {"success": True, "message": "知识库条目已更新"}


@router.delete("/knowledge/{kb_id}")
def admin_delete_knowledge(kb_id: int, db: Session = Depends(get_db)):
    """删除知识库条目"""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库条目不存在")
    db.delete(kb)
    db.commit()
    return {"success": True, "message": "知识库条目已删除"}


# ═══════════════════════════════════════════════════════
#  系统设置
# ═══════════════════════════════════════════════════════


class AIChatToggleRequest(BaseModel):
    ai_chat_enabled: bool


class RatingThresholdRequest(BaseModel):
    excellent: float = 90.0
    good: float = 75.0
    pass_: float = 60.0


@router.get("/settings")
def admin_get_settings(db: Session = Depends(get_db)):
    """获取系统设置"""
    cfg = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
    ai_chat_enabled = True
    rating_thresholds = {"excellent": 90.0, "good": 75.0, "pass": 60.0}
    if cfg and cfg.api_base_url:
        try:
            saved = json.loads(cfg.api_base_url)
            ai_chat_enabled = saved.get("ai_chat_enabled", True)
            rating_thresholds = saved.get("rating_thresholds", rating_thresholds)
        except Exception:
            pass
    return {
        "success": True,
        "data": {
            "ai_chat_enabled": ai_chat_enabled,
            "rating_thresholds": rating_thresholds,
        }
    }


@router.post("/settings/ai-chat-toggle")
def admin_toggle_ai_chat(req: AIChatToggleRequest, db: Session = Depends(get_db)):
    """切换AI聊天功能开关"""
    cfg = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
    current = {"ai_chat_enabled": True, "rating_thresholds": {"excellent": 90.0, "good": 75.0, "pass": 60.0}}
    if cfg and cfg.api_base_url:
        try:
            current = json.loads(cfg.api_base_url)
        except Exception:
            pass
    current["ai_chat_enabled"] = req.ai_chat_enabled
    if not cfg:
        cfg = ModelConfig(platform="system_config", model_name="system_config", api_base_url=json.dumps(current))
        db.add(cfg)
    else:
        cfg.api_base_url = json.dumps(current)
    db.commit()
    return {"success": True, "message": f"AI聊天已{'启用' if req.ai_chat_enabled else '禁用'}"}


@router.post("/settings/rating-thresholds")
def admin_set_rating_thresholds(req: RatingThresholdRequest, db: Session = Depends(get_db)):
    """设置评分等级阈值"""
    cfg = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
    current = {"ai_chat_enabled": True, "rating_thresholds": {"excellent": 90.0, "good": 75.0, "pass": 60.0}}
    if cfg and cfg.api_base_url:
        try:
            current = json.loads(cfg.api_base_url)
        except Exception:
            pass
    current["rating_thresholds"] = {"excellent": req.excellent, "good": req.good, "pass": req.pass_}
    if not cfg:
        cfg = ModelConfig(platform="system_config", model_name="system_config", api_base_url=json.dumps(current))
        db.add(cfg)
    else:
        cfg.api_base_url = json.dumps(current)
    db.commit()
    return {"success": True, "message": "评分阈值已更新", "data": current["rating_thresholds"]}


# ═══════════════════════════════════════════════════════
#  Token 监控（客户端管控）
# ═══════════════════════════════════════════════════════

@router.get("/token-stats")
def admin_token_stats(db: Session = Depends(get_db)):
    """Token消耗统计 — 趋势、分布、Top用户"""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    total_tokens = db.query(func.sum(User.tokens_used)).scalar() or 0
    total_cost = db.query(func.sum(TokenUsage.cost)).scalar() or 0.0

    today_usage = db.query(func.sum(TokenUsage.total_tokens)).filter(
        TokenUsage.created_at >= today
    ).scalar() or 0

    week_usage = db.query(func.sum(TokenUsage.total_tokens)).filter(
        TokenUsage.created_at >= week_ago
    ).scalar() or 0

    month_usage = db.query(func.sum(TokenUsage.total_tokens)).filter(
        TokenUsage.created_at >= month_ago
    ).scalar() or 0

    top_users = db.query(
        User.username, User.tokens_used, User.id
    ).filter(User.tokens_used > 0).order_by(User.tokens_used.desc()).limit(10).all()

    daily_trend = db.query(
        func.date(TokenUsage.created_at).label("date"),
        func.sum(TokenUsage.total_tokens).label("tokens")
    ).filter(TokenUsage.created_at >= month_ago).group_by("date").order_by("date").all()

    model_usage = db.query(
        TokenUsage.model_name,
        func.sum(TokenUsage.total_tokens).label("tokens"),
        func.count(TokenUsage.id).label("calls")
    ).group_by(TokenUsage.model_name).order_by(func.sum(TokenUsage.total_tokens).desc()).all()

    return {
        "success": True,
        "data": {
            "overview": {
                "total_tokens": total_tokens,
                "total_cost": round(total_cost, 4),
                "today_tokens": today_usage,
                "week_tokens": week_usage,
                "month_tokens": month_usage,
            },
            "top_users": [{"id": u[2], "username": u[0], "tokens_used": u[1]} for u in top_users],
            "daily_trend": [{"date": str(d[0]), "tokens": d[1]} for d in daily_trend],
            "model_usage": [{"model": m[0], "tokens": m[1], "calls": m[2]} for m in model_usage],
        }
    }


@router.get("/ai-chat-stats")
def admin_ai_chat_stats(db: Session = Depends(get_db)):
    """AI聊天使用统计"""
    total_conversations = db.query(func.count(AiConversation.id)).scalar() or 0
    total_messages = db.query(func.count(AiMessage.id)).scalar() or 0
    active_conversations_today = db.query(func.count(AiConversation.id)).filter(
        AiConversation.updated_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    ).scalar() or 0

    top_users = db.query(
        User.username, func.count(AiConversation.id).label("conv_count")
    ).join(AiConversation, AiConversation.user_id == User.id).group_by(
        User.id
    ).order_by(func.count(AiConversation.id).desc()).limit(10).all()

    return {
        "success": True,
        "data": {
            "total_conversations": total_conversations,
            "total_messages": total_messages,
            "active_conversations_today": active_conversations_today,
            "top_users": [{"username": u[0], "conversation_count": u[1]} for u in top_users],
        }
    }


# ═══════════════════════════════════════════════════════
#  通知管理
# ═══════════════════════════════════════════════════════


class AdminSendNotificationRequest(BaseModel):
    title: str
    content: str
    user_ids: Optional[List[int]] = None  # None/空 = 全部用户


@router.post("/notifications")
def admin_send_notification(
    req: AdminSendNotificationRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """管理员发送通知 — 支持指定用户或全部用户"""
    if not req.title or not req.title.strip():
        raise HTTPException(status_code=400, detail="通知标题不能为空")
    if not req.content or not req.content.strip():
        raise HTTPException(status_code=400, detail="通知内容不能为空")

    # 确定目标用户
    if req.user_ids and len(req.user_ids) > 0:
        # 发送给指定用户
        users = db.query(User).filter(User.id.in_(req.user_ids), User.role == UserRole.USER).all()
        if not users:
            raise HTTPException(status_code=404, detail="未找到目标用户")
    else:
        # 发送给全部普通用户
        users = db.query(User).filter(User.role == UserRole.USER).all()
        if not users:
            raise HTTPException(status_code=404, detail="系统中暂无普通用户")

    notifications = []
    for user in users:
        n = Notification(
            user_id=user.id,
            title=req.title.strip(),
            content=req.content.strip(),
            sender_id=admin.id,
        )
        db.add(n)
        notifications.append({"user_id": user.id, "username": user.username})

    db.commit()

    _log_operation(
        db, user_id=admin.id, username=admin.username,
        action="admin_send_notification", resource_type="notification",
        detail=f"发送通知「{req.title}」给 {len(users)} 个用户",
    )

    return {
        "success": True,
        "message": f"通知已发送给 {len(users)} 个用户",
        "data": {"recipient_count": len(users), "recipients": notifications},
    }


@router.get("/notifications")
def admin_get_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """管理员查看已发送的通知历史"""
    q = db.query(Notification).order_by(Notification.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()

    return {
        "success": True,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "notifications": [
                {
                    "id": n.id,
                    "user_id": n.user_id,
                    "username": n.user.username if n.user else "未知",
                    "title": n.title,
                    "content": n.content,
                    "is_read": n.is_read,
                    "sender_id": n.sender_id,
                    "created_at": n.created_at.isoformat(),
                }
                for n in items
            ],
        }
    }


@router.delete("/notifications/by-title")
def admin_delete_notifications_by_title(
    title: str = Query(..., description="通知标题"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """管理员批量撤回通知 — 按标题删除所有匹配的通知"""
    count = db.query(Notification).filter(Notification.title == title).delete(synchronize_session=False)
    db.commit()

    _log_operation(
        db, user_id=admin.id, username=admin.username,
        action="admin_batch_delete_notifications", resource_type="notification",
        detail=f"批量撤回通知「{title}」共 {count} 条",
    )
    return {"success": True, "message": f"已撤回 {count} 条通知"}


@router.delete("/notifications/{notification_id}")
def admin_delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """管理员删除/撤回通知 — 从数据库中彻底删除，所有客户端将不可见"""
    n = db.query(Notification).filter(Notification.id == notification_id).first()
    if not n:
        raise HTTPException(status_code=404, detail="通知不存在")
    title = n.title
    db.delete(n)
    db.commit()

    _log_operation(
        db, user_id=admin.id, username=admin.username,
        action="admin_delete_notification", resource_type="notification",
        detail=f"撤回通知「{title}」(ID:{notification_id})",
    )
    return {"success": True, "message": "通知已撤回"}


# ═══════════════════════════════════════════════════════
#  报告模块顺序管理（步骤6/13/14）
# ═══════════════════════════════════════════════════════


class ModuleSequenceRequest(BaseModel):
    step_number: int
    module_key: str
    module_name: str
    sort_order: int = 0
    is_visible: bool = True
    client_editable: bool = True


class BatchModuleSequenceRequest(BaseModel):
    modules: list[ModuleSequenceRequest]


@router.get("/module-sequences")
def admin_list_module_sequences(
    step_number: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """获取报告模块顺序配置"""
    q = db.query(ReportModuleSequence)
    if step_number is not None:
        q = q.filter(ReportModuleSequence.step_number == step_number)
    modules = q.order_by(ReportModuleSequence.step_number, ReportModuleSequence.sort_order).all()
    return {
        "success": True,
        "data": [
            {
                "id": m.id, "step_number": m.step_number,
                "module_key": m.module_key, "module_name": m.module_name,
                "sort_order": m.sort_order, "is_visible": m.is_visible,
                "client_editable": m.client_editable,
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            }
            for m in modules
        ]
    }


@router.post("/module-sequences")
def admin_create_module_sequence(
    req: ModuleSequenceRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """创建模块顺序配置"""
    existing = db.query(ReportModuleSequence).filter(
        ReportModuleSequence.step_number == req.step_number,
        ReportModuleSequence.module_key == req.module_key,
    ).first()
    if existing:
        existing.module_name = req.module_name
        existing.sort_order = req.sort_order
        existing.is_visible = req.is_visible
        existing.client_editable = req.client_editable
        existing.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": True, "message": "模块顺序已更新", "data": {"id": existing.id}}

    module = ReportModuleSequence(
        step_number=req.step_number, module_key=req.module_key,
        module_name=req.module_name, sort_order=req.sort_order,
        is_visible=req.is_visible, client_editable=req.client_editable,
    )
    db.add(module)
    db.commit()
    db.refresh(module)
    _log_operation(db, admin.id, admin.username, "create_module_sequence",
                   resource_type="module_sequence", resource_id=str(module.id),
                   detail=f"创建模块顺序 step={req.step_number} key={req.module_key}")
    return {"success": True, "data": {"id": module.id}, "message": "模块顺序创建成功"}


@router.put("/module-sequences/batch")
def admin_batch_update_module_sequences(
    req: BatchModuleSequenceRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """批量更新模块顺序（支持拖拽排序）"""
    updated = 0
    for m in req.modules:
        existing = db.query(ReportModuleSequence).filter(
            ReportModuleSequence.step_number == m.step_number,
            ReportModuleSequence.module_key == m.module_key,
        ).first()
        if existing:
            existing.sort_order = m.sort_order
            existing.module_name = m.module_name
            existing.is_visible = m.is_visible
            existing.client_editable = m.client_editable
            existing.updated_at = datetime.now(timezone.utc)
        else:
            new_m = ReportModuleSequence(
                step_number=m.step_number, module_key=m.module_key,
                module_name=m.module_name, sort_order=m.sort_order,
                is_visible=m.is_visible, client_editable=m.client_editable,
            )
            db.add(new_m)
        updated += 1
    db.commit()
    _log_operation(db, admin.id, admin.username, "batch_update_module_sequence",
                   resource_type="module_sequence",
                   detail=f"批量更新 {updated} 个模块顺序")
    return {"success": True, "message": f"已更新 {updated} 个模块顺序"}


@router.delete("/module-sequences/{module_id}")
def admin_delete_module_sequence(
    module_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """删除模块顺序配置"""
    module = db.query(ReportModuleSequence).filter(ReportModuleSequence.id == module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="模块顺序不存在")
    db.delete(module)
    db.commit()
    return {"success": True, "message": "模块顺序已删除"}


# ═══════════════════════════════════════════════════════
#  知识库管理（增强：支持按 parent_indicator 筛选）
# ═══════════════════════════════════════════════════════


@router.put("/knowledge/{kb_id}/granular")
def admin_update_granular_knowledge(
    kb_id: int,
    req: KnowledgeBaseRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """更新知识库条目（含 parent_indicator 字段）"""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库条目不存在")
    kb.name = req.name
    kb.content = req.content
    kb.category = req.category
    kb.step_number = req.step_number
    kb.indicator_level = req.indicator_level
    kb.is_active = req.is_active
    kb.updated_at = datetime.now(timezone.utc)
    db.commit()
    _log_operation(db, admin.id, admin.username, "update_granular_knowledge",
                   resource_type="knowledge", resource_id=str(kb_id),
                   detail=f"更新知识库: {req.name}")
    return {"success": True, "message": "知识库条目已更新"}


# ═══════════════════════════════════════════════════════
#  步骤配置摘要（管理端概览用）
# ═══════════════════════════════════════════════════════

@router.get("/step-config")
def admin_get_step_config(db: Session = Depends(get_db)):
    """获取所有步骤的配置摘要（Prompt数量 + 知识库数量 + 模块序列）"""
    steps = []
    for step_number in range(1, 15):
        prompt_count = db.query(func.count(PromptTemplate.id)).filter(
            PromptTemplate.step_number == step_number
        ).scalar() or 0
        kb_count = db.query(func.count(KnowledgeBase.id)).filter(
            KnowledgeBase.step_number == step_number
        ).scalar() or 0
        module_count = db.query(func.count(ReportModuleSequence.id)).filter(
            ReportModuleSequence.step_number == step_number
        ).scalar() or 0
        steps.append({
            "step_number": step_number,
            "prompt_count": prompt_count,
            "kb_count": kb_count,
            "module_count": module_count,
        })
    return {"success": True, "data": steps}


# ═══════════════════════════════════════════════════════
#  数据清理管理（文件保留策略）
# ═══════════════════════════════════════════════════════


class CleanupRequest(BaseModel):
    retention_days: Optional[int] = None


@router.get("/cleanup/preview")
def admin_cleanup_preview(
    retention_days: Optional[int] = Query(None, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """预览即将被清理的数据量（不执行删除）"""
    from backend.services.cleanup_service import CleanupService
    return {"success": True, "data": CleanupService.get_cleanup_preview(db, retention_days)}


@router.post("/cleanup/execute")
def admin_execute_cleanup(
    req: CleanupRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """手动执行数据清理"""
    from backend.services.cleanup_service import CleanupService
    result = CleanupService.cleanup_expired_files(db, req.retention_days)
    _log_operation(
        db, admin.id, admin.username, "execute_cleanup",
        resource_type="system",
        detail=f"执行清理: 删除{result['deleted_files']}文件, 释放{result['freed_disk_bytes']/1024/1024:.2f}MB",
    )
    return {"success": True, "data": result}


@router.post("/cleanup/fix-failed")
def admin_cleanup_stuck_workflows(
    max_age_hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """清理卡住的失败工作流"""
    from backend.services.cleanup_service import CleanupService
    count = CleanupService.cleanup_failed_workflows(db, max_age_hours)
    _log_operation(
        db, admin.id, admin.username, "cleanup_stuck_workflows",
        resource_type="system",
        detail=f"清理了 {count} 个卡住的工作流",
    )
    return {"success": True, "data": {"cleaned_count": count}}


# ═══════════════════════════════════════════════════════
#  文件管理统计
# ═══════════════════════════════════════════════════════

@router.get("/file-stats")
def admin_file_stats(db: Session = Depends(get_db)):
    """获取文件存储统计信息"""
    total_files = db.query(func.count(ProjectFile.id)).scalar() or 0
    total_size_db = db.query(func.sum(ProjectFile.file_size)).scalar() or 0

    # 按文件类型统计
    by_type = db.query(
        ProjectFile.file_type,
        func.count(ProjectFile.id).label("count"),
        func.sum(ProjectFile.file_size).label("total_size"),
    ).group_by(ProjectFile.file_type).all()

    # 按上传类型统计
    by_upload_type = db.query(
        func.coalesce(ProjectFile.upload_type, "通用").label("upload_type"),
        func.count(ProjectFile.id).label("count"),
    ).group_by("upload_type").all()

    # 90天内即将到期文件数
    cutoff = datetime.now(timezone.utc) + timedelta(days=90)
    expiring = db.query(func.count(ProjectFile.id)).filter(
        ProjectFile.expires_at.isnot(None),
        ProjectFile.expires_at <= cutoff,
        ProjectFile.expires_at > datetime.now(timezone.utc),
    ).scalar() or 0

    return {
        "success": True,
        "data": {
            "total_files": total_files,
            "total_size_mb": round(total_size_db / (1024 * 1024), 2) if total_size_db else 0,
            "by_type": [
                {"type": t[0], "count": t[1], "size_mb": round((t[2] or 0) / (1024 * 1024), 2)}
                for t in by_type
            ],
            "by_upload_type": [
                {"type": t[0], "count": t[1]}
                for t in by_upload_type
            ],
            "expiring_in_90_days": expiring,
            "retention_days": 90,
        }
    }


from backend.utils.log_utils import log_operation as _log_operation