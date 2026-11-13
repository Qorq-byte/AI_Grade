"""
Admin 管理路由
仪表盘统计 / 用户管理 / 操作日志审计 / 系统配置
所有端点需要管理员权限
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, text, case
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from loguru import logger
import os
import json
import sys
import time
import shutil
import platform
from pathlib import Path

from backend.database import get_db
from backend.services.secret_box import SECRET_MASK, encrypt_secret, mask_secret
from backend.models.user_models import (
    User, UserRole, WorkflowSession, OperationLog,
    ModelConfig, PromptTemplate, KnowledgeBase, PromptVersion,
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
from backend.utils.time_utils import utc_iso, utcnow, local_day_start_utc

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
    old_password: str
    new_password: str


# ═══════════════════════════════════════════════════════
#  当前管理员（强制改密逃生通道）
# ═══════════════════════════════════════════════════════
# 这两个端点在 `PASSWORD_CHANGE_EXEMPT_PATHS` 里，是「首登未改密」状态下
# 唯一还能访问的管理端接口。它们不返回任何业务数据，只回自己的身份和改密能力，
# 所以放行不会削弱那道护栏；反之缺少它们，admin 会被永久锁在管理端外。

@router.get("/me")
def admin_me(admin: User = Depends(get_admin_user)):
    """当前登录管理员的自述信息 — 供管理端判断是否处于「待改初始口令」状态"""
    return {
        "success": True,
        "data": {
            "id": admin.id,
            "username": admin.username,
            "role": admin.role.value if isinstance(admin.role, UserRole) else admin.role,
            "must_change_password": bool(getattr(admin, "must_change_password", False)),
        },
    }


@router.put("/me/password")
def admin_change_own_password(
    req: AdminChangePasswordRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """管理员自助改密 — 用自己的旧口令换新口令，不依赖任何业务接口"""
    from backend.services.auth_service import verify_password

    old_password = (req.old_password or "").strip()
    new_password = req.new_password or ""
    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="请填写旧密码和新密码")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于 6 位")
    if new_password == old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")

    stored = admin.password_hash or ""
    if ":" not in stored:
        raise HTTPException(status_code=500, detail="账号口令数据异常，请联系系统维护人员")
    salt, stored_hash = stored.split(":", 1)
    if not verify_password(old_password, stored_hash, salt):
        raise HTTPException(status_code=400, detail="旧密码不正确")

    new_hash, new_salt = hash_password(new_password)
    admin.password_hash = f"{new_salt}:{new_hash}"
    admin.must_change_password = False
    # 旧令牌立即作废，但补发一枚给当前这台设备，避免刚改完就被踢出去
    admin.token_version = int(getattr(admin, "token_version", 0) or 0) + 1
    db.commit()

    _log_operation(
        db,
        user_id=admin.id,
        username=admin.username,
        action="admin_change_password",
        resource_type="user",
        resource_id=str(admin.id),
        detail="管理员自助修改密码",
    )

    from backend.middleware.auth_middleware import create_access_token

    new_access = create_access_token(data={
        "user_id": admin.id,
        "username": admin.username,
        "role": admin.role.value if isinstance(admin.role, UserRole) else admin.role,
        "ver": int(admin.token_version or 0),
    })
    return {
        "success": True,
        "message": "密码修改成功",
        "data": {"access_token": new_access},
    }


# ═══════════════════════════════════════════════════════
#  仪表盘
# ═══════════════════════════════════════════════════════

@router.get("/dashboard")
def admin_dashboard(db: Session = Depends(get_db)):
    """管理员仪表盘 — 核心统计数据

    原来是 8 条独立的 COUNT/SUM 串着跑，全部在「首屏」这条路径上。
    同一张表的多口径统计合成一条（用户 3 个口径 1 条、工作流 4 个口径 1 条），
    8 次往返压到 3 次；口径一个没变。
    """
    today = local_day_start_utc()

    # 用户：总数 / 今日活跃 / Token 合计
    total_users, active_users_today, total_tokens = db.query(
        func.count(User.id),
        func.coalesce(func.sum(case(
            ((User.last_login_at >= today) & (User.is_active.is_(True)), 1),
            else_=0,
        )), 0),
        func.coalesce(func.sum(User.tokens_used), 0),
    ).one()
    total_users = int(total_users or 0)
    active_users_today = int(active_users_today or 0)
    total_tokens = int(total_tokens or 0)

    # 工作流：按状态一次分组，四个口径都从这里取
    wf_rows = db.query(
        WorkflowSession.status, func.count(WorkflowSession.id)
    ).group_by(WorkflowSession.status).all()
    wf_counts = {status: count for status, count in wf_rows}
    total_projects = sum(wf_counts.values())
    active_workflows = sum(wf_counts.get(s, 0) for s in ("created", "running", "paused"))
    completed_workflows = wf_counts.get("completed", 0)
    failed_workflows = wf_counts.get("failed", 0)

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
                    "created_at": utc_iso(l.created_at),
                }
                for l in recent_logs
            ],
        }
    }


# ═══════════════════════════════════════════════════════
#  用户管理
# ═══════════════════════════════════════════════════════

_USER_SORT_FIELDS = {
    # 「谁最烧 Token」是这个列表的主要用途，必须在 SQL 里排，
    # 否则只能排当前这一页 —— 翻页后答案就变了。
    "tokens_desc": lambda: func.coalesce(User.tokens_used, 0).desc(),
    "tokens_asc": lambda: func.coalesce(User.tokens_used, 0).asc(),
    "created_desc": lambda: User.created_at.desc(),
    "created_asc": lambda: User.created_at.asc(),
    "last_login_desc": lambda: func.coalesce(User.last_login_at, User.created_at).desc(),
    "last_login_asc": lambda: func.coalesce(User.last_login_at, User.created_at).asc(),
}


@router.get("/users")
def admin_list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    role: Optional[str] = None,
    sort: Optional[str] = Query(None, description="tokens_desc / tokens_asc / created_desc / created_asc / last_login_desc / last_login_asc"),
    db: Session = Depends(get_db),
):
    """管理员查看用户列表（分页 + 筛选 + 排序）"""
    q = db.query(User)
    if search:
        q = q.filter(User.username.contains(search))
    if role:
        q = q.filter(User.role == role)

    total = q.count()
    order_by = _USER_SORT_FIELDS.get((sort or "").strip(), _USER_SORT_FIELDS["created_desc"])()
    # 主排序键可能重复（同 token 数），补 id 兜底，否则翻页会漏人/重复
    users = q.order_by(order_by, User.id.asc()).offset((page - 1) * page_size).limit(page_size).all()

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
                    "last_login": utc_iso(u.last_login_at),
                    "created_at": utc_iso(u.created_at),
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


@router.get("/users/options")
def admin_user_options(
    role: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(2000, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """用户下拉选项（轻量）— 只回 id/username/role

    必须声明在 `/users/{user_id}` 之前：否则 `options` 会先被当作 int 路径参数
    匹配进详情接口，直接 422。
    这个端点的存在是为了让「通知-指定用户」这类需要全量用户的地方不必去调
    `/users?page_size=9999` —— 那个参数超过 `le=100` 会被 FastAPI 挡下。
    """
    q = db.query(User.id, User.username, User.role, User.is_active)
    if role:
        q = q.filter(User.role == role)
    if search:
        q = q.filter(User.username.contains(search))
    rows = q.order_by(User.id.asc()).limit(limit + 1).all()
    truncated = len(rows) > limit
    rows = rows[:limit]

    return {
        "success": True,
        "data": {
            "total": len(rows),
            "truncated": truncated,
            "users": [
                {
                    "id": r[0],
                    "username": r[1],
                    "role": r[2].value if isinstance(r[2], UserRole) else r[2],
                    "is_active": r[3],
                }
                for r in rows
            ],
        },
    }


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
            "last_login": utc_iso(user.last_login_at),
            "created_at": utc_iso(user.created_at),
            "project_count": project_count,
            "recent_operations": [
                {"action": op.action, "detail": op.detail, "time": utc_iso(op.created_at)}
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
        # 管理员设的密码本人并不知道（或只是临时口令），置位后强制其下次登录自行改密
        user.must_change_password = True
        # 管理员重置口令 → 该用户已登录的会话必须立刻失效（批次 25）
        user.token_version = int(getattr(user, "token_version", 0) or 0) + 1
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
def admin_delete_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
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
        db, user_id=admin.id, username=admin.username,
        action="admin_delete_user", resource_type="user", resource_id=str(user_id),
        detail=f"管理员禁用用户 {username}",
    )
    return {"success": True, "message": f"用户 '{username}' 已被禁用"}


@router.post("/users/{user_id}/deactivate")
def admin_deactivate_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
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

    # 磁盘上的上传件与导出件：库里的记录跟着 cascade 没了，但盘上的文件不会自己消失。
    # 只删库不删盘 = 空间没释放、数据还在，等于「注销」了两次都没删干净。
    disk_stats = {"files": 0, "bytes": 0, "dirs": 0, "errors": []}
    try:
        from backend.services.cleanup_service import CleanupService
        disk_stats = CleanupService.purge_user_disk_artifacts(user_id)
    except Exception as e:  # noqa: BLE001 磁盘清理失败不该影响已完成的注销
        logger.error("清理用户 {} 的磁盘残留失败: {}", user_id, str(e))

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
        from backend.routes import _shared_state
        _QUESTIONNAIRE_CACHE = _shared_state._QUESTIONNAIRE_CACHE
        for sid in session_ids:
            _QUESTIONNAIRE_CACHE.pop(sid, None)
            if sid in _shared_state.active_workflows:
                try: _shared_state.active_workflows[sid].close()
                except Exception: pass
                _shared_state.active_workflows.pop(sid, None)
            _shared_state._WORKFLOW_PROGRESS.pop(sid, None)
    except Exception:
        pass

    # ── 操作日志 ──
    # 必须记在「操作者」名下：被注销的用户行已经删除，再用它的 id 写 OperationLog
    # 会撞 `operation_logs.user_id → users.id` 外键，写入必然失败；而失败又被
    # 下面的 except 吞掉，于是最该留痕的操作反而一条记录都没有。
    try:
        _log_operation(
            db,
            user_id=admin.id,
            username=admin.username,
            action="admin_deactivate_user",
            resource_type="user",
            resource_id=str(user_id),
            detail=(
                f"管理员注销并删除用户 {username}(id={user_id}) 及其所有关联数据；"
                f"清理磁盘文件 {disk_stats['files']} 个 / "
                f"{disk_stats['bytes'] / 1024 / 1024:.2f} MB、"
                f"导出目录 {disk_stats['dirs']} 个"
            ),
        )
    except Exception as e:  # noqa: BLE001 审计失败不应回滚已完成的删除
        logger.error("注销用户的审计日志写入失败: target={} err={}", user_id, str(e))

    return {
        "success": True,
        "message": (
            f"用户 '{username}' 已注销，数据库记录与磁盘文件均已删除"
            f"（文件 {disk_stats['files']} 个，释放 {disk_stats['bytes'] / 1024 / 1024:.2f} MB）"
        ),
        "data": {
            "deleted_files": disk_stats["files"],
            "freed_bytes": disk_stats["bytes"],
            "deleted_export_dirs": disk_stats["dirs"],
            "errors": disk_stats["errors"],
        },
    }


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
                    "created_at": utc_iso(l.created_at),
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
                "last_time": utc_iso(r.last_time),
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
    from backend.routes import _shared_state

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
                    "current_step": _shared_state._WORKFLOW_PROGRESS.get(w.session_id, {}).get("last_step", w.current_step),
                    "model_name": w.model_name,
                    "token_used": w.token_used,
                    "created_at": utc_iso(w.created_at),
                    "completed_at": utc_iso(w.completed_at),
                }
                for w in workflows
            ],
        }
    }



# ═══════════════════════════════════════════════════════
#  工作流详情 / 手动终止
# ═══════════════════════════════════════════════════════
# 健康页能报出「N 个卡住超 24h」，但没法点进去看是哪一个、卡在第几步、报了什么错。
# 这两个接口补的就是那段闭环：定位 → 处置。

_TERMINAL_WF_STATUS = {"completed", "failed", "cancelled"}


def _parse_json_list(raw) -> list:
    """errors / warnings / completed_steps 都是 JSON 字符串，坏了就当空。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_completed_steps(raw) -> list[int]:
    steps = []
    for item in _parse_json_list(raw):
        try:
            steps.append(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(set(steps))


@router.get("/workflows/{session_id}")
def admin_workflow_detail(session_id: str, db: Session = Depends(get_db)):
    """单个工作流的完整画像：步骤时间线、错误、Token 分布、已产出的报告"""
    from backend.routes import _shared_state

    w = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="工作流不存在")

    done_steps = _parse_completed_steps(w.completed_steps)
    progress = dict(_shared_state._WORKFLOW_PROGRESS.get(session_id) or {})
    live_step = int(progress.get("last_step") or progress.get("current_step") or 0)
    current_step = live_step or (w.current_step or 0)

    # 14 步逐格状态（在 tokens_by_step 之后计算，需要用到 token 作为「跑过」的证据）

    # Token 按步骤分布（一条 group by，不要逐行懒加载）
    token_rows = db.query(
        TokenUsage.step_number, func.sum(TokenUsage.total_tokens)
    ).filter(TokenUsage.session_id == session_id).group_by(TokenUsage.step_number).all()
    tokens_by_step = [
        {"step": row[0], "tokens": int(row[1] or 0)}
        for row in sorted(token_rows, key=lambda r: (r[0] is None, r[0] or 0))
    ]

    # 有 token 消耗但没进 completed_steps 的步骤 = 跑过但没跑完。
    # 真实库里这种会话很常见：第 1 步烧了 3500 token 然后卡死，completed_steps 是空的 ——
    # 只按 completed_steps 画会显示成「一步都没做」，把问题掩盖掉。
    touched_steps = {t["step"] for t in tokens_by_step if t["step"] and t["tokens"] > 0}
    tokens_map = {t["step"]: t["tokens"] for t in tokens_by_step}

    timeline = []
    for step in range(1, 15):
        if step in done_steps:
            state = "done"
        elif step == current_step and w.status not in _TERMINAL_WF_STATUS:
            state = "current"
        elif step in touched_steps:
            state = "partial"
        else:
            state = "pending"
        timeline.append({"step": step, "state": state, "tokens": tokens_map.get(step, 0)})

    # 产出物。注意：`reports` 表实际上没人写入（全仓没有 db.add(Report)），
    # 真正的内容在 `final_report`、`checkpoint_data` 和 state_backups 里的 JSON。
    # 第一版照着 reports 表查，结果永远是空的 —— 这种「看起来有、其实没有」的字段
    # 比缺字段更糟，所以这里如实报「有多大」，而不是列一堆空行。
    root = _project_root()
    backup_file = root / "state_backups" / f"{w.session_id}.json"
    try:
        backup_kb = round(backup_file.stat().st_size / 1024, 1) if backup_file.is_file() else None
    except OSError:
        backup_kb = None
    artifacts = {
        "final_report_chars": len(w.final_report or ""),
        "checkpoint_kb": round(len(w.checkpoint_data or "") / 1024, 1),
        "state_backup_kb": backup_kb,
    }

    # 卡住时长：未终态 + 有创建时间才算
    stalled_hours = None
    if w.status not in _TERMINAL_WF_STATUS and w.created_at:
        stalled_hours = round((utcnow() - w.created_at).total_seconds() / 3600, 1)

    username = None
    if w.user_id:
        row = db.query(User.username).filter(User.id == w.user_id).first()
        username = row[0] if row else None

    return {
        "success": True,
        "data": {
            "session_id": w.session_id,
            "project_name": w.project_name,
            "project_type": w.project_type,
            "user_id": w.user_id,
            "username": username,
            "status": w.status,
            "current_step": current_step,
            "total_steps": 14,
            "model_name": w.model_name,
            "use_rag": bool(w.use_rag),
            "comparison_mode": w.comparison_mode,
            "token_used": w.token_used,
            "created_at": utc_iso(w.created_at),
            "updated_at": utc_iso(w.updated_at),
            "completed_at": utc_iso(w.completed_at),
            "stalled_hours": stalled_hours,
            "timeline": timeline,
            "completed_steps": done_steps,
            "errors": _parse_json_list(w.errors),
            "warnings": _parse_json_list(w.warnings),
            "tokens_by_step": tokens_by_step,
            "artifacts": artifacts,
            # 哪些步骤有正文可看（详情页用它点亮时间线），取自 state_backups
            "sections": _sections_of(_load_state_snapshot(w.session_id)[0]),
            "live": {
                "in_memory": bool(progress),
                "status": progress.get("status"),
                "last_step": live_step or None,
                "has_error": bool(progress.get("has_error")),
                "started_steps": progress.get("started_steps") or [],
            },
        },
    }


@router.post("/workflows/{session_id}/fail")
def admin_fail_workflow(
    session_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """把卡住的工作流标记为失败（健康页发现异常后的处置动作）。

    只对未终态生效：已经 completed 的会话被误标成 failed，会让用户丢掉报告入口。
    """
    from backend.routes import _shared_state

    w = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="工作流不存在")
    if w.status in _TERMINAL_WF_STATUS:
        raise HTTPException(status_code=400, detail=f"工作流已是终态（{w.status}），无需处置")

    old_status = w.status
    w.status = "failed"
    w.completed_at = utcnow()
    errors = _parse_json_list(w.errors)
    errors.append({
        "step": w.current_step,
        "message": f"管理员手动终止（原状态 {old_status}）",
        "by": admin.username,
    })
    w.errors = json.dumps(errors, ensure_ascii=False)
    db.commit()

    # 进程内实例与进度一起收掉，否则列表还会显示它在「运行中」
    released = False
    try:
        inst = _shared_state.active_workflows.pop(session_id, None)
        if inst is not None:
            released = True
            try:
                inst.close()
            except Exception:  # noqa: BLE001 关闭失败不影响标记结果
                pass
        _shared_state.active_workflow_configs.pop(session_id, None)
        _shared_state._WORKFLOW_PROGRESS.pop(session_id, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理工作流内存状态失败: {} {}", session_id, str(exc))

    _log_operation(
        db, admin.id, admin.username, "admin_fail_workflow",
        resource_type="workflow", resource_id=session_id,
        detail=f"管理员将工作流 {session_id}（{w.project_name}）从 {old_status} 标记为失败",
    )

    return {
        "success": True,
        "message": f"已标记为失败（原状态 {old_status}）",
        "data": {"released_in_memory": released},
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
                "sort_order": m.sort_order, "created_at": utc_iso(m.created_at),
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
                "updated_at": utc_iso(p.updated_at),
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
    # 新建的提示词也留一份 v1 快照，历史从第一天就是完整的
    _snapshot_prompt(db, prompt, version=prompt.version, change_log=prompt.change_log,
                     modified_by=admin.id)
    db.commit()
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
    prompt.updated_at = utcnow()
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
            "updated_at": utc_iso(prompt.updated_at),
        }
    }


# ── 提示词版本历史 ─────────────────────────────────────
# `prompt_templates` 只存「当前内容」，改坏了就只剩「重置为系统默认」一条路。
# 下面两个小函数负责在每次写入前后打点，让回滚和对比成为可能。

def _snapshot_prompt(db: Session, prompt: PromptTemplate, *, version: int,
                     change_log: str, modified_by: Optional[int]) -> None:
    """把 prompt 的**当前**内容追加进历史。调用方负责 commit。"""
    db.add(PromptVersion(
        prompt_id=prompt.id,
        version=version,
        prompt_content=prompt.prompt_content,
        change_log=change_log,
        modified_by=modified_by,
    ))


def _ensure_prompt_baseline(db: Session, prompt: PromptTemplate) -> None:
    """首次改动前，先把「改动前的那一版」存进去。

    不然历史里永远缺最早的内容：用户改坏之后想回到第一次保存前的样子，无从下手。
    """
    exists = db.query(PromptVersion.id).filter(PromptVersion.prompt_id == prompt.id).first()
    if exists or prompt.prompt_content is None:
        return
    db.add(PromptVersion(
        prompt_id=prompt.id,
        version=prompt.version or 1,
        prompt_content=prompt.prompt_content,
        change_log="（历史起点）首次改动前的原始内容",
        modified_by=prompt.modified_by,
    ))


def _invalidate_prompt_cache() -> None:
    """让进程内的提示词缓存立刻作废。

    PromptManager 是单例 + 60 秒 TTL 缓存：改完 / 回滚完如果不清，工作流还会拿
    旧内容跑一分钟。「回滚」这种救火操作，等一分钟等于没用。
    """
    try:
        from backend.agents.prompts.prompt_manager import get_prompt_manager
        get_prompt_manager().clear_cache()
    except Exception as e:  # noqa: BLE001 缓存清不掉不该让保存失败
        logger.warning("清理提示词缓存失败: {}", str(e))


@router.put("/prompts/{prompt_id}")
def admin_update_prompt(
    prompt_id: int, req: PromptUpdateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """更新提示词（自动版本递增 + 变更日志 + 历史快照）"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")
    if not (req.prompt_content or "").strip():
        raise HTTPException(status_code=400, detail="提示词内容不能为空")

    _ensure_prompt_baseline(db, prompt)

    prompt.prompt_content = req.prompt_content
    prompt.version = (prompt.version or 0) + 1
    prompt.modified_by = admin.id
    prompt.change_log = req.change_log or "管理员手动更新"
    prompt.updated_at = utcnow()
    _snapshot_prompt(db, prompt, version=prompt.version,
                     change_log=prompt.change_log, modified_by=admin.id)
    db.commit()
    _invalidate_prompt_cache()
    return {"success": True, "message": f"提示词 v{prompt.version} 已更新"}


@router.get("/prompts/{prompt_id}/versions")
def admin_list_prompt_versions(
    prompt_id: int,
    db: Session = Depends(get_db),
):
    """列出某个提示词的历史版本（默认不返回正文，避免列表拖成几百 KB）"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")

    rows = (
        db.query(PromptVersion)
        .filter(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version.desc())
        .all()
    )
    # 操作者名字：一次查完，别在列表里逐行懒加载
    operator_ids = {r.modified_by for r in rows if r.modified_by}
    names = {}
    if operator_ids:
        names = {
            uid: uname for uid, uname in
            db.query(User.id, User.username).filter(User.id.in_(operator_ids)).all()
        }

    return {
        "success": True,
        "data": {
            "prompt": {
                "id": prompt.id,
                "step_number": prompt.step_number,
                "step_name": prompt.step_name,
                "current_version": prompt.version,
                "is_system": bool(prompt.is_system),
            },
            "versions": [
                {
                    "id": r.id,
                    "version": r.version,
                    "change_log": r.change_log,
                    "modified_by": r.modified_by,
                    "modified_by_name": names.get(r.modified_by) or ("系统" if not r.modified_by else f"#{r.modified_by}"),
                    "length": len(r.prompt_content or ""),
                    "is_current": r.version == prompt.version,
                    "created_at": utc_iso(r.created_at),
                }
                for r in rows
            ],
        },
    }


@router.get("/prompts/{prompt_id}/versions/{version}")
def admin_get_prompt_version(
    prompt_id: int,
    version: int,
    db: Session = Depends(get_db),
):
    """取某一版正文（对比/预览用）"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")

    row = (
        db.query(PromptVersion)
        .filter(PromptVersion.prompt_id == prompt_id, PromptVersion.version == version)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"v{version} 不存在")

    return {
        "success": True,
        "data": {
            "version": row.version,
            "prompt_content": row.prompt_content or "",
            "change_log": row.change_log,
            "created_at": utc_iso(row.created_at),
            # 顺手把当前内容带上，前端拿来算 diff 就不用再发一次请求
            "current_version": prompt.version,
            "current_content": prompt.prompt_content or "",
        },
    }


@router.post("/prompts/{prompt_id}/rollback/{version}")
def admin_rollback_prompt(
    prompt_id: int,
    version: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """回滚到某一版：把内容写回当前行，并作为新版本追加（历史不丢）"""
    prompt = db.query(PromptTemplate).filter(PromptTemplate.id == prompt_id).first()
    if not prompt:
        raise HTTPException(status_code=404, detail="提示词不存在")

    row = (
        db.query(PromptVersion)
        .filter(PromptVersion.prompt_id == prompt_id, PromptVersion.version == version)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"v{version} 不存在")
    if row.version == prompt.version:
        raise HTTPException(status_code=400, detail=f"当前就是 v{version}，无需回滚")

    _ensure_prompt_baseline(db, prompt)

    prompt.prompt_content = row.prompt_content
    prompt.version = (prompt.version or 0) + 1
    prompt.modified_by = admin.id
    prompt.change_log = f"回滚到 v{version}（{row.change_log or '无说明'}）"
    prompt.updated_at = utcnow()
    _snapshot_prompt(db, prompt, version=prompt.version,
                     change_log=prompt.change_log, modified_by=admin.id)
    db.commit()
    _invalidate_prompt_cache()

    _log_operation(
        db, admin.id, admin.username, "rollback_prompt",
        resource_type="prompt", resource_id=str(prompt_id),
        detail=f"提示词 {prompt.step_name} 回滚到 v{version}，新版本 v{prompt.version}",
    )
    return {
        "success": True,
        "message": f"已回滚到 v{version}（当前为 v{prompt.version}）",
        "data": {"version": prompt.version},
    }


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

    _ensure_prompt_baseline(db, prompt)

    prompt.prompt_content = (
        f"{default_data.get('system_prompt', '')}\n\n---\n\n{default_data.get('human_prompt', '')}"
    )
    prompt.step_name = default_data.get("step_name") or prompt.step_name
    prompt.version = (prompt.version or 0) + 1
    prompt.modified_by = admin.id
    prompt.change_log = "重置为系统默认"
    prompt.updated_at = utcnow()
    _snapshot_prompt(db, prompt, version=prompt.version,
                     change_log=prompt.change_log, modified_by=admin.id)
    db.commit()
    _invalidate_prompt_cache()
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
    # 历史快照没有 ORM 关系（PromptVersion 不挂在 PromptTemplate 上），
    # 不显式删就会撞 prompt_versions.prompt_id 外键，删除直接 500。
    db.query(PromptVersion).filter(PromptVersion.prompt_id == prompt_id).delete(synchronize_session=False)
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
                    "updated_at": utc_iso(k.updated_at),
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
            "updated_at": utc_iso(kb.updated_at),
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
    fallback_models = []
    if cfg and cfg.api_base_url:
        try:
            saved = json.loads(cfg.api_base_url)
            ai_chat_enabled = saved.get("ai_chat_enabled", True)
            rating_thresholds = saved.get("rating_thresholds", rating_thresholds)
            # 批次 32：降级链的备选模型（按顺序，最多 2 个）
            fallback_models = saved.get("fallback_models", []) or []
        except Exception:
            pass
    return {
        "success": True,
        "data": {
            "ai_chat_enabled": ai_chat_enabled,
            "rating_thresholds": rating_thresholds,
            "fallback_models": fallback_models,
        }
    }


class FallbackModelsRequest(BaseModel):
    models: list[str] = []


def _system_config_row(db: Session) -> ModelConfig:
    """取（或建）`platform="system_config"` 那一行。

    设置都塞在 `api_base_url` 的 JSON 里 —— 沿用项目已有约定，不新增列。
    """
    cfg = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
    if cfg is None:
        cfg = ModelConfig(platform="system_config", model_name="system_config",
                          api_base_url="{}")
        db.add(cfg)
    return cfg


@router.post("/settings/fallback-models")
def admin_set_fallback_models(req: FallbackModelsRequest, db: Session = Depends(get_db)):
    """设置降级链的备选模型（按顺序，最多 2 个）。

    留空 = 关闭降级。为什么限 2 个：一次调用最多试 主 + 2 备选 = 3 次，
    再多的降级不只是烧钱，报告已经是"另一个模型写的"、可信度说不清。
    """
    cleaned: list[str] = []
    for name in req.models:
        text = str(name or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > 2:
        raise HTTPException(status_code=400, detail="备选模型最多 2 个")
    cfg = _system_config_row(db)
    current = {}
    if cfg.api_base_url:
        try:
            current = json.loads(cfg.api_base_url)
        except Exception:
            current = {}
    current["fallback_models"] = cleaned
    cfg.api_base_url = json.dumps(current, ensure_ascii=False)
    db.commit()
    message = ("备选模型已设置为：" + " → ".join(cleaned)) if cleaned else "备选模型已清空（降级关闭）"
    return {"success": True, "data": {"fallback_models": cleaned}, "message": message}


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
    today = local_day_start_utc()
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

    # 图表按「北京日」分桶：库里存的是 UTC，MySQL 上先加 8 小时再取日期。
    # 非 MySQL（单测用的 SQLite）退回 UTC 日 —— 只影响柱子的边界，不影响上面的总数。
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "mysql":
        day_expr = func.date(func.date_add(TokenUsage.created_at, text("INTERVAL 8 HOUR")))
    else:
        day_expr = func.date(TokenUsage.created_at)

    daily_trend = db.query(
        day_expr.label("date"),
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
        AiConversation.updated_at >= local_day_start_utc()
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
    # joinedload：用户名在同一页里逐行懒加载 = 20 条通知 21 次查询。
    # 一页最多 100 条，这个 N+1 在「全选发送」之后会明显拖慢列表。
    q = (
        db.query(Notification)
        .options(joinedload(Notification.user))
        .order_by(Notification.created_at.desc())
    )
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
                    "created_at": utc_iso(n.created_at),
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
                "updated_at": utc_iso(m.updated_at),
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
        existing.updated_at = utcnow()
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
            existing.updated_at = utcnow()
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
    kb.updated_at = utcnow()
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
    """获取所有步骤的配置摘要（Prompt数量 + 知识库数量 + 模块序列）

    三张表各一条 GROUP BY 就够了。原来是 14 步 × 3 张表 = 42 条 COUNT，
    一屏概览要来回 42 次，纯粹是把数据库当计数器用。
    """
    def _counts_by_step(model):
        rows = db.query(model.step_number, func.count(model.id)).group_by(model.step_number).all()
        return {step: count for step, count in rows if step is not None}

    prompt_counts = _counts_by_step(PromptTemplate)
    kb_counts = _counts_by_step(KnowledgeBase)
    module_counts = _counts_by_step(ReportModuleSequence)

    steps = [
        {
            "step_number": step_number,
            "prompt_count": prompt_counts.get(step_number, 0),
            "kb_count": kb_counts.get(step_number, 0),
            "module_count": module_counts.get(step_number, 0),
        }
        for step_number in range(1, 15)
    ]
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
    cutoff = utcnow() + timedelta(days=90)
    expiring = db.query(func.count(ProjectFile.id)).filter(
        ProjectFile.expires_at.isnot(None),
        ProjectFile.expires_at <= cutoff,
        ProjectFile.expires_at > utcnow(),
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



# ═══════════════════════════════════════════════════════
#  系统健康
# ═══════════════════════════════════════════════════════
# 只报**能真测到**的东西。LLM 那栏明确标成「配置状态」而不是「连通性」——
# 真去 ping 一次要花钱、还可能拖慢首屏；想验活最靠谱的仍然是跑一次工作流。
# 宁可写清楚「这是配置」，也不做一个看起来像探活的假绿灯。

_PROCESS_STARTED_AT = time.time()
_disk_cache = {"at": 0.0, "data": None}
_DISK_CACHE_TTL = 30.0
_TREE_FILE_CAP = 50000


def _tree_usage(path: Path) -> dict:
    """递归统计目录下文件数与字节数；超过上限就停下并标注不完整。"""
    files, size, truncated = 0, 0, False
    if not path.is_dir():
        return {"files": 0, "mb": 0.0, "truncated": False}
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                size += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                continue
            if files >= _TREE_FILE_CAP:
                truncated = True
                break
        if truncated:
            break
    return {"files": files, "mb": round(size / 1024 / 1024, 2), "truncated": truncated}


def _disk_snapshot(project_root: Path) -> dict:
    """磁盘快照带 30 秒缓存：全量遍历目录不该每刷新一次都做一遍。"""
    now = time.time()
    if _disk_cache["data"] is not None and now - _disk_cache["at"] < _DISK_CACHE_TTL:
        return _disk_cache["data"]

    data = {
        "uploads": _tree_usage(project_root / "uploads"),
        "exports": _tree_usage(project_root / "backend" / "exports"),
    }
    try:
        usage = shutil.disk_usage(str(project_root))
        data["volume"] = {
            "total_gb": round(usage.total / 1024 ** 3, 1),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "used_pct": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        }
    except OSError as exc:
        data["volume"] = None
        data["volume_error"] = str(exc)

    _disk_cache["data"] = data
    _disk_cache["at"] = now
    return data


@router.get("/system-health")
def admin_system_health(db: Session = Depends(get_db)):
    """系统健康概览：进程 / 数据库 / 磁盘 / 工作流 / LLM 配置 / 清理定时器"""
    checks = []

    # ── 1. 数据库 ──
    db_ok, db_error, latency_ms = True, None, None
    try:
        started = time.perf_counter()
        db.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
    except Exception as exc:  # noqa: BLE001 这里就是要抓到并展示
        db_ok = False
        db_error = str(exc)

    pool_status = None
    try:
        pool_status = db.get_bind().pool.status()
    except Exception:  # noqa: BLE001 NullPool / 自定义池没有 status()
        pass

    if not db_ok:
        db_level = "down"
    elif latency_ms is not None and latency_ms >= 200:
        db_level = "warn"
    else:
        db_level = "ok"
    checks.append({
        "key": "database", "label": "数据库", "level": db_level,
        "summary": f"{latency_ms} ms" if db_ok else "连接失败",
        "detail": {"latency_ms": latency_ms, "pool": pool_status, "error": db_error},
    })

    # ── 2. 磁盘 ──
    project_root = Path(__file__).resolve().parent.parent.parent
    disk = _disk_snapshot(project_root)
    vol = disk.get("volume")
    if vol is None:
        disk_level = "warn"
        disk_summary = "无法读取磁盘信息"
    else:
        disk_level = "down" if vol["used_pct"] >= 95 else ("warn" if vol["used_pct"] >= 85 else "ok")
        disk_summary = f"剩余 {vol['free_gb']} GB / 共 {vol['total_gb']} GB（已用 {vol['used_pct']}%）"
    checks.append({
        "key": "disk", "label": "磁盘", "level": disk_level, "summary": disk_summary,
        "detail": disk,
    })

    # ── 3. 工作流 ──
    wf_rows = db.query(
        WorkflowSession.status, func.count(WorkflowSession.id)
    ).group_by(WorkflowSession.status).all()
    wf_counts = {status: count for status, count in wf_rows}
    running = wf_counts.get("running", 0)
    awaiting = wf_counts.get("awaiting_approval", 0) + wf_counts.get("paused", 0)
    stale = 0
    try:
        stale = db.query(func.count(WorkflowSession.id)).filter(
            WorkflowSession.status.in_(["created", "running", "paused"]),
            WorkflowSession.created_at < utcnow() - timedelta(hours=24),
        ).scalar() or 0
    except Exception:  # noqa: BLE001 老库异常不该拦住整页
        pass

    from backend.routes import _shared_state
    in_memory = len(_shared_state.active_workflows)

    checks.append({
        "key": "workflows", "label": "工作流", "level": "warn" if stale else "ok",
        "summary": f"运行中 {running} · 待确认 {awaiting} · 卡住超 24h {stale}",
        "detail": {
            "by_status": wf_counts,
            "in_memory": in_memory,
            "stale_over_24h": stale,
            "note": "进程内实例数是单 worker 的视角，多 worker 部署下各算各的",
        },
    })

    # ── 4. LLM 配置（不是连通性） ──
    from backend.config import settings

    providers = {
        "DeepSeek": bool(settings.DEEPSEEK_API_KEY),
        "阿里云百炼": bool(settings.DASHSCOPE_API_KEY),
        "OpenAI": bool(settings.OPENAI_API_KEY),
        "Anthropic": bool(settings.ANTHROPIC_API_KEY),
    }
    enabled_models = db.query(ModelConfig).filter(
        ModelConfig.is_enabled.is_(True), ModelConfig.platform != "system_config"
    ).all()
    # 模型也可能直接写在 .env 里（QWEN_MODELS / DEEPSEEK_MODELS …），
    # 只数数据库里的行会得出「启用 0 个模型」，看着像坏了其实一切正常。
    env_models = [
        m.strip()
        for blob in (settings.QWEN_MODELS, settings.DEEPSEEK_MODELS,
                     settings.OPENAI_MODELS, settings.ANTHROPIC_MODELS)
        for m in (blob or "").split(",")
        if m.strip()
    ]

    cfg = db.query(ModelConfig).filter(ModelConfig.platform == "system_config").first()
    saved = {}
    if cfg and cfg.api_base_url:
        try:
            saved = json.loads(cfg.api_base_url) or {}
        except Exception:
            saved = {}
    fallback = saved.get("fallback_models") or []

    has_any_key = any(providers.values())
    db_model_names = [m.model_name for m in enabled_models]
    usable_models = sorted(set(env_models) | set(db_model_names))
    llm_level = "ok" if (has_any_key and usable_models) else ("warn" if has_any_key else "down")
    checks.append({
        "key": "llm", "label": "LLM 配置", "level": llm_level,
        "summary": (
            f"默认 {settings.DEFAULT_MODEL} · 可用 {len(usable_models)} 个模型 · "
            f"降级链 {len(fallback)} 个"
        ),
        "detail": {
            "providers": providers,
            "default_model": settings.DEFAULT_MODEL,
            "fallback_models": fallback,
            "env_models": env_models,
            "db_models": db_model_names,
            "note": "只反映配置是否齐全，不代表 Key 有效；真要验活请跑一次工作流",
        },
    })

    # ── 5. 清理定时器 ──
    scheduler_alive = False
    next_run = None
    try:
        import backend.main as main_module
        task = getattr(main_module, "_CLEANUP_TASK", None)
        scheduler_alive = task is not None and not task.done()
        from backend.utils.time_utils import local_now
        candidate = local_now().replace(hour=3, minute=0, second=0, microsecond=0)
        if candidate <= local_now():
            candidate = candidate + timedelta(days=1)
        next_run = candidate.isoformat()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取清理定时器状态失败: {}", str(exc))

    checks.append({
        "key": "scheduler", "label": "清理定时器", "level": "ok" if scheduler_alive else "warn",
        "summary": "运行中，每日 03:00 清理" if scheduler_alive else "未启动（重启后才会拉起）",
        "detail": {"alive": scheduler_alive, "next_run": next_run},
    })

    # ── 6. 进程 ──
    from backend.config import settings as _settings_for_process
    uptime = int(time.time() - _PROCESS_STARTED_AT)
    checks.append({
        "key": "process", "label": "服务进程", "level": "ok",
        "summary": f"已运行 {uptime // 3600} 小时 {(uptime % 3600) // 60} 分",
        "detail": {
            "version": _settings_for_process.APP_VERSION,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pid": os.getpid(),
            "debug": bool(_settings_for_process.DEBUG),
            "uptime_seconds": uptime,
        },
    })

    order = {"down": 0, "warn": 1, "ok": 2}
    overall = min((c["level"] for c in checks), key=lambda lv: order[lv])

    return {
        "success": True,
        "data": {"overall": overall, "checked_at": utc_iso(utcnow()), "checks": checks},
    }



# ── 分步正文 ──────────────────────────────────────────
# 内容不在数据库里（`reports` 表从来没被写过），真正躺在 state_backups/{sid}.json。
# 字段名与步骤的对应关系以 performance_state.PerformanceState 的注释为准。

_STEP_CONTENT_FIELDS = {
    1: [("cost_list", "资料清单")],
    2: [("effective_data", "核心数据提取")],
    3: [("indicator_system", "绩效评价指标体系")],
    4: [("indicator_scores", "指标分值分配")],
    5: [("scoring_standards", "指标评分标准")],
    6: [("evaluation_table", "评价指标体系表")],
    7: [("indicator_analysis", "指标得分分析"), ("score_table", "评分表")],
    8: [("experience_summary", "主要经验做法")],
    9: [("problems_analysis", "问题及原因分析")],
    10: [("suggestions", "改进建议")],
    11: [("comprehensive_evaluation", "综合评价分析及结论")],
    12: [("basic_info", "基本情况")],
    13: [("work_introduction", "评价工作开展情况")],
    14: [("final_report", "最终评价报告")],
}

_STEP_TITLES = {step: fields[0][1] for step, fields in _STEP_CONTENT_FIELDS.items()}

#: 单次返回上限：最终报告能到 5 万字以上，一口气塞进 JSON 只会把浏览器拖住
_CONTENT_CHAR_CAP = 200_000

#: 备份解析缓存：{path: (mtime, size, dict)}。360KB 的 JSON 每看一次解析一次不划算。
_BACKUP_CACHE: dict = {}
_BACKUP_CACHE_MAX = 4


def _project_root() -> Path:
    """项目根目录。抽成函数是为了测试能把它指到临时目录。"""
    return Path(__file__).resolve().parent.parent.parent


def _load_state_snapshot(session_id: str) -> tuple[dict, str]:
    """返回 (状态字典, 来源)。来源：state_backup / checkpoint_data / none。

    `session_id` 会被拼进文件名，所以必须先过格式校验，否则就是路径穿越。
    """
    from backend.routes._shared_state import _validate_session_id

    _validate_session_id(session_id)

    root = _project_root()
    path = root / "state_backups" / f"{session_id}.json"
    try:
        stat = path.stat()
    except OSError:
        stat = None

    if stat is not None:
        cached = _BACKUP_CACHE.get(str(path))
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2], "state_backup"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if len(_BACKUP_CACHE) >= _BACKUP_CACHE_MAX:
                    _BACKUP_CACHE.pop(next(iter(_BACKUP_CACHE)), None)
                _BACKUP_CACHE[str(path)] = (stat.st_mtime, stat.st_size, data)
                return data, "state_backup"
        except Exception as exc:  # noqa: BLE001 备份坏了就退回 checkpoint
            logger.warning("读取状态备份失败 {}: {}", path.name, str(exc))

    # 退回数据库里的 checkpoint（可能是 langgraph 序列化后的结构，尽量解析）
    w = None
    from backend.database import SessionLocal
    try:
        db = SessionLocal()
        try:
            w = db.query(WorkflowSession).filter(
                WorkflowSession.session_id == session_id).first()
            raw = w.checkpoint_data if w else None
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        raw = None

    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data, "checkpoint_data"
        except Exception:  # noqa: BLE001
            pass
    return {}, "none"


def _section_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return str(value)


def _sections_of(snapshot: dict) -> list:
    """目录：哪些步骤有内容、各多长。详情页用它点亮时间线。"""
    out = []
    for step in range(1, 15):
        chars = 0
        for field, _label in _STEP_CONTENT_FIELDS[step]:
            chars += len(_section_text(snapshot.get(field)))
        if chars:
            out.append({"step": step, "title": _STEP_TITLES[step], "chars": chars})
    return out


@router.get("/workflows/{session_id}/content")
def admin_workflow_content(
    session_id: str,
    step: int = Query(..., ge=1, le=14, description="步骤号 1-14"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """取某一步的产出正文。

    报告是用户的业务数据，管理员能看是为了核实「报告里那句话是哪来的」，
    所以每次查看都写一条审计 —— 谁、在什么时候、看了谁的哪一步。
    """
    w = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="工作流不存在")

    snapshot, source = _load_state_snapshot(session_id)
    sections = []
    truncated = False
    for field, label in _STEP_CONTENT_FIELDS[step]:
        text = _section_text(snapshot.get(field))
        if not text:
            continue
        if len(text) > _CONTENT_CHAR_CAP:
            text = text[:_CONTENT_CHAR_CAP]
            truncated = True
        sections.append({"field": field, "label": label, "chars": len(text), "content": text})

    if not sections and step == 14 and w.final_report:
        text = w.final_report
        if len(text) > _CONTENT_CHAR_CAP:
            text = text[:_CONTENT_CHAR_CAP]
            truncated = True
        sections.append({"field": "final_report", "label": "最终评价报告",
                         "chars": len(text), "content": text})
        source = "final_report_column"

    _log_operation(
        db, admin.id, admin.username, "admin_view_workflow_content",
        resource_type="workflow", resource_id=session_id,
        detail=f"管理员查看 {w.project_name} 第 {step} 步产出（{len(sections)} 段）",
    )

    return {
        "success": True,
        "data": {
            "session_id": session_id,
            "step": step,
            "title": _STEP_TITLES.get(step, f"步骤 {step}"),
            "source": source,
            "truncated": truncated,
            "sections": sections,
        },
    }


from backend.utils.log_utils import log_operation as _log_operation