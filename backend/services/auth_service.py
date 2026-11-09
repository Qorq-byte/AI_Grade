
import hashlib
import secrets
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from loguru import logger
from backend.models.user_models import User, UserRole

#: 首次启动自动创建的管理员口令。它是公开可见的（写在源码和 README 里），
#: 所以创建后必须置 must_change_password，登录第一件事就是改掉（批次 24）。
DEFAULT_ADMIN_PASSWORD = "Admin@123456"

#: 迁移时用来识别「还在用初始口令」的账号。只认这些；用户自己设的密码不会被误标。
KNOWN_DEFAULT_PASSWORDS = (DEFAULT_ADMIN_PASSWORD,)


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


def create_user(db: Session, username: str, password: str, phone: str = None, role: str = "user",
                must_change_password: bool = False) -> User:
    """创建用户（含密码复杂度校验）

    must_change_password=True 用于「初始口令不是本人设定」的账号（管理员建号、
    默认管理员），登录后先强制改密。
    """
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

    user = User(
        username=username,
        password_hash=stored_value,
        phone=phone,
        role=UserRole.ADMIN if role == "admin" else UserRole.USER,
        must_change_password=must_change_password,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"创建用户成功: username={username}, role={role}, 需改密={must_change_password}")
    return user


def authenticate_user(db: Session, username: str, password: str) -> User:
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user:
        raise ValueError("用户名或密码错误")

    stored_value = user.password_hash
    if ":" not in stored_value:
        raise ValueError("密码格式错误")

    salt, stored_hash = stored_value.split(":", 1)
    if not verify_password(password, stored_hash, salt):
        raise ValueError("用户名或密码错误")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return user


def get_user_by_id(db: Session, user_id: int) -> User:
    return db.query(User).filter(User.id == user_id).first()


def get_all_users(db: Session) -> list:
    return db.query(User).order_by(User.created_at.desc()).all()


def update_user_status(db: Session, user_id: int, is_active: bool) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("用户不存在")
    user.is_active = is_active
    db.commit()
    db.refresh(user)
    return user


def create_admin_if_not_exists(db: Session):
    admin = db.query(User).filter(User.role == UserRole.ADMIN).first()
    if not admin:
        try:
            admin = create_user(
                db=db,
                username="admin",
                password=DEFAULT_ADMIN_PASSWORD,
                role="admin",
                must_change_password=True,
            )
            # 不再把口令打进日志：日志会被收集、转发、贴进工单，等于二次泄露
            logger.info("默认管理员账号已创建（初始口令见文档，首次登录必须修改）")
        except ValueError as e:
            # 如果密码复杂度校验失败，使用更简单的密码通过hash直接创建
            logger.warning(f"默认管理员创建失败({e})，尝试替代方式")
            # 注意 hash_password 返回 (hash, salt) 元组，必须拼成 "salt:hash" 才能过后续校验
            raw_hash, raw_salt = hash_password(DEFAULT_ADMIN_PASSWORD)
            admin = User(
                username="admin",
                password_hash=f"{raw_salt}:{raw_hash}",
                role=UserRole.ADMIN,
                is_active=True,
                must_change_password=True,
                created_at=datetime.utcnow()
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
            logger.info("默认管理员账号已创建(替代方式)")
    return admin