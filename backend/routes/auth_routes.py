from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from backend.database import get_db
from backend.services.auth_service import (
    create_user, authenticate_user, get_user_by_id,
    get_all_users, update_user_status, create_admin_if_not_exists
)
from backend.middleware.auth_middleware import (
    create_access_token, create_refresh_token,
    get_current_user, get_admin_user
)
from backend.models.user_models import User

router = APIRouter(prefix="/api/auth", tags=["认证管理"])


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    phone: str = None


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    tokens_used: int


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    try:
        user = authenticate_user(db, req.username, req.password)
        # 生成 JWT tokens
        access_token = create_access_token(data={
            "user_id": user.id,
            "username": user.username,
            "role": user.role.value,
        })
        refresh_token = create_refresh_token(data={
            "user_id": user.id,
            "username": user.username,
        })
        # 记录操作日志
        _log_operation(
            db, user_id=user.id, username=user.username,
            action="login", resource_type="user", resource_id=str(user.id),
            detail=f"用户 {user.username} 登录成功",
        )
        return {
            "success": True,
            "message": "登录成功",
            "data": {
                "user_id": user.id,
                "username": user.username,
                "role": user.role.value,
                "tokens_used": user.tokens_used,
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user = create_user(db, req.username, req.password, req.phone)
        return {
            "success": True,
            "message": "注册成功",
            "data": {"user_id": user.id, "username": user.username}
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/create-user")
def admin_create_user(req: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user = create_user(db, req.username, req.password, req.phone)
        return {
            "success": True,
            "message": f"用户 '{req.username}' 创建成功",
            "data": {"user_id": user.id, "username": user.username}
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    users = get_all_users(db)
    return {
        "success": True,
        "data": [
            {
                "id": u.id,
                "username": u.username,
                "phone": u.phone,
                "role": u.role.value,
                "is_active": u.is_active,
                "tokens_used": u.tokens_used,
                "last_login": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat()
            }
            for u in users
        ]
    }


@router.post("/users/{user_id}/toggle-status")
def toggle_user_status(user_id: int, db: Session = Depends(get_db)):
    try:
        user = get_user_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        user = update_user_status(db, user_id, not user.is_active)
        return {
            "success": True,
            "message": f"用户状态已更新: {'启用' if user.is_active else '禁用'}"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/init-admin")
def init_admin(db: Session = Depends(get_db)):
    admin = create_admin_if_not_exists(db)
    return {
        "success": True,
        "message": "管理员账号已就绪",
        "data": {"username": admin.username}
    }

@router.post("/refresh")
def refresh_token(refresh_token_str: str, db: Session = Depends(get_db)):
    """使用refresh token获取新的access token"""
    from backend.middleware.auth_middleware import decode_token
    payload = decode_token(refresh_token_str)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="无效的刷新凭据")
    user_id = payload.get("user_id")
    user = get_user_by_id(db, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已被禁用")
    new_access = create_access_token(data={
        "user_id": user.id,
        "username": user.username,
        "role": user.role.value,
    })
    return {
        "success": True,
        "data": {"access_token": new_access}
    }


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return {
        "success": True,
        "data": {
            "user_id": current_user.id,
            "username": current_user.username,
            "role": current_user.role.value,
            "phone": current_user.phone,
            "is_active": current_user.is_active,
            "tokens_used": current_user.tokens_used,
            "created_at": current_user.created_at.isoformat(),
        }
    }


from backend.utils.log_utils import log_operation as _log_operation
