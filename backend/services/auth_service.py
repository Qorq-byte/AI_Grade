
import hashlib
import secrets
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from loguru import logger
from backend.models.user_models import User, UserRole


def hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return hashed, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    hashed, _ = hash_password(password, salt)
    return hashed == stored_hash


def validate_password_strength(password: str) -> list:
    """
    密码复杂度校验
    要求：8位以上，包含大小写字母+数字+特殊字符中至少3种
    返回错误列表，空列表表示通过
    """
    errors = []
    if len(password) < 8:
        errors.append("密码长度至少8位")
    checks = [
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"\d", password)),
        bool(re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?`~]", password)),
    ]
    if sum(checks) < 3:
        errors.append("密码需包含大写字母、小写字母、数字、特殊字符中至少3种")
    if " " in password:
        errors.append("密码不能包含空格")
    return errors


def create_user(db: Session, username: str, password: str, phone: str = None, role: str = "user") -> User:
    """创建用户（含密码复杂度校验）"""
    # 密码复杂度校验
    validation_errors = validate_password_strength(password)
    if validation_errors:
        raise ValueError("密码不符合要求: " + "; ".join(validation_errors))
    
    # 用户名校验
    username = username.strip()
    if not username or len(username) < 2:
        raise ValueError("用户名至少2个字符")
    if not re.match(r'^[\w\u4e00-\u9fff]+$', username):
        raise ValueError("用户名只能包含字母、数字、下划线和中文")
    
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise ValueError(f"用户名 '{username}' 已存在")

    password_hash, salt = hash_password(password)
    stored_value = f"{salt}:{password_hash}"

