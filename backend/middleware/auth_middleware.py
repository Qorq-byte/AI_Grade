"""
JWT认证中间件
提供token生成、验证和FastAPI依赖注入
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from loguru import logger

from backend.config import settings
from backend.database import get_db
from backend.models.user_models import User, UserRole

# ── JWT 配置 ──
SECRET_KEY = settings.JWT_SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS = settings.REFRESH_TOKEN_EXPIRE_DAYS

# ── Bearer安全方案 ──
security = HTTPBearer()

# ── 强制改密（批次 24）──
# 未完成首登改密时，除下面这些路径外一律 403。
# 必须放开改密接口本身，否则用户被锁死在一个「改不了密也干不了事」的死角里。
PASSWORD_CHANGE_EXEMPT_PATHS = (
    "/api/client/password",   # 唯一的改密入口
    "/api/auth/me",           # 前端要知道自己是谁、还差哪一步
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/refresh",
)

#: 前端靠这个 code 区分「要改密」和「登录失效」——
#: 两者都是 403/401，如果共用 code，前端会清 token 把人踢回登录页，形成死循环。
PASSWORD_CHANGE_REQUIRED_CODE = "password_change_required"

def _sign_token(payload: dict) -> str:
    if not SECRET_KEY:
        raise HTTPException(status_code=503, detail="认证服务未配置")
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

# ═══════════════════════════════════════════════════════
#  JWT 工具函数
# ═══════════════════════════════════════════════════════

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建访问令牌"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access"
    })
    return _sign_token(to_encode)


def create_refresh_token(data: dict) -> str:
    """创建刷新令牌"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh"
    })
    return _sign_token(to_encode)


def decode_token(token: str) -> Optional[dict]:
    """解码并验证token"""
    if not SECRET_KEY:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.warning(f"Token解码失败: {e}")
        return None

# ═══════════════════════════════════════════════════════
#  FastAPI 依赖注入
# ═══════════════════════════════════════════════════════

def is_password_change_exempt(path: str) -> bool:
    """该路径是否允许「未完成首登改密」的账号访问。"""
    clean = (path or "").rstrip("/") or "/"
    return any(clean == p or clean.startswith(p + "/") for p in PASSWORD_CHANGE_EXEMPT_PATHS)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """
    从Bearer token中提取当前登录用户
    所有需要认证的端点使用此依赖
    """
    token = credentials.credentials
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 验证token类型
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请使用访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
        )
    
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )
    
    return user


async def get_admin_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """验证当前用户是否为管理员"""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    ),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """可选用户认证 — 未登录返回None，不抛出异常"""
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials, db)
    except HTTPException:
        return None

