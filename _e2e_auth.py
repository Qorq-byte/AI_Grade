"""E2E 脚本的管理员凭据（批次 24）

背景：`admin` 账号在首登强制改密上线后会被打标（它至今仍用着源码里那串初始口令），
所有管理端接口对它返回 403。E2E 脚本如果继续用 `admin/Admin@123456`，会整片红，
但那不是回归、是安全策略在正常工作。

所以给自动化测试一个**独立**的管理员账号 `e2e_admin`：
  * 口令随机生成，写进 `.e2e_credentials.json`（已 gitignore，不入库）；
  * `must_change_password=False`——它本来就是脚本自己设的口令，没有"初始口令"一说；
  * 每台机器只生成一次，之后复用；登录失败（库被重置/口令改过）就重新设置。

这样既不动 `admin` 的强制改密状态，脚本也不用硬编码任何口令。
"""
from __future__ import annotations

import json
import secrets
import string
from pathlib import Path
from typing import Optional, Tuple

CRED_PATH = Path(__file__).resolve().parent / ".e2e_credentials.json"
E2E_ADMIN_USERNAME = "e2e_admin"


def _random_password() -> str:
    """生成满足 `validate_password_strength` 的随机口令（4 类字符齐全）。"""
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"E2e#{core}9a"


def _load() -> Optional[Tuple[str, str]]:
    try:
        data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
        return data["username"], data["password"]
    except Exception:
        return None


def _save(username: str, password: str) -> None:
    CRED_PATH.write_text(
        json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _upsert_admin(username: str, password: str) -> None:
    """在库里创建或重置这个测试管理员（直接写库，绕开 HTTP 层）。"""
    from backend.database import SessionLocal
    from backend.models.user_models import User, UserRole
    from backend.services.auth_service import hash_password

    db = SessionLocal()
    try:
        raw_hash, salt = hash_password(password)
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            user = User(username=username, role=UserRole.ADMIN, is_active=True)
            db.add(user)
        user.password_hash = f"{salt}:{raw_hash}"
        user.role = UserRole.ADMIN
        user.is_active = True
        # 脚本自己设定的随机口令，不需要再走首登改密
        user.must_change_password = False
        db.commit()
    finally:
        db.close()


def _can_login(base_url: str, username: str, password: str) -> bool:
    try:
        import requests
        r = requests.post(
            f"{base_url.rstrip('/')}/api/auth/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        return r.status_code == 200 and bool(r.json().get("success"))
    except Exception:
        return False


def ensure_e2e_admin(base_url: str = "http://127.0.0.1:8000") -> Tuple[str, str]:
    """返回 (username, password)，保证这份凭据当前可登录且不受强制改密影响。"""
    cached = _load()
    if cached and _can_login(base_url, *cached):
        return cached

    password = _random_password()
    _upsert_admin(E2E_ADMIN_USERNAME, password)
    _save(E2E_ADMIN_USERNAME, password)
    return E2E_ADMIN_USERNAME, password


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print(ensure_e2e_admin())
