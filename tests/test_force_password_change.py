"""首登强制改密（批次 24 = C6 安全收尾）

改前的样子：

  * `create_admin_if_not_exists()` 里硬编码 `Admin@123456`，**还会把这个口令打进日志**；
    实测本机库里 admin 至今仍是这个口令 —— 谁看过这个仓库就能登进管理员后台。
  * 没有任何「是否改过初始密码」的状态；管理员给用户重置密码后也没人知道。
  * 分支二里 `password_hash=hash_password(...)` 把 `(hash, salt)` 元组塞进 String 列，
    一旦走到那条分支，之后所有登录都会因为 `":" not in stored_value` 失败。

口径：
  * 新增 `must_change_password` 列，老库靠幂等 ALTER 补上，老行默认 False（不误伤）；
  * 升级时只给「仍在用初始口令」的账号打标（逐个试算，不搞一刀切）；
  * 打标用户除改密/读自身信息外一律 403，且 **403 要能被前端识别**，
    否则前端会当成登录失效把人踢回登录页，形成「改不了密也进不去」的死循环。
"""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from backend.middleware import auth_middleware
from backend.models.user_models import User, UserRole
from backend.services import auth_service


# ═══════════════════════════════════════════════════════
#  一、列与模型
# ═══════════════════════════════════════════════════════

def test_user_model_has_must_change_password_column():
    cols = {c.name: c for c in User.__table__.columns}
    assert "must_change_password" in cols, "缺少 must_change_password 列"
    col = cols["must_change_password"]
    assert col.nullable is False, "必须 NOT NULL，否则老行会是 NULL 无法判定"
    assert col.server_default is not None, "必须有 server_default，ALTER TABLE 加列时老行才有着落"


def test_schema_patch_is_idempotent(monkeypatch):
    """补丁必须可重复执行：每次启动都会跑一遍。"""
    from backend.utils import schema_patches

    calls = []

    def fake_exists(conn, table, column):
        calls.append((table, column))
        return True  # 假装已经有了

    monkeypatch.setattr(schema_patches, "_column_exists", fake_exists)
    applied = schema_patches.apply_schema_patches(SimpleNamespace(begin=lambda: _Ctx()))
    assert applied == []
    assert calls, "补丁逻辑没有检查列是否存在"


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise AssertionError("列已存在时不该执行 ALTER")


# ═══════════════════════════════════════════════════════
#  二、账号创建/重置时置位
# ═══════════════════════════════════════════════════════

def test_create_user_defaults_to_no_flag():
    """注册（用户自己设密码）不需要强制改密。"""
    import inspect
    sig = inspect.signature(auth_service.create_user)
    assert sig.parameters["must_change_password"].default is False


def test_default_admin_password_is_documented_constant():
    """初始口令收进常量，方便迁移时逐个试算识别。"""
    assert auth_service.DEFAULT_ADMIN_PASSWORD == "Admin@123456"
    assert auth_service.DEFAULT_ADMIN_PASSWORD in auth_service.KNOWN_DEFAULT_PASSWORDS


def test_admin_creation_sets_flag():
    """默认管理员的密码是公开的，创建完必须要求改（走真实创建流程，不靠 grep 源码）。"""
    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class _DB:
        def __init__(self):
            self.added = []

        def query(self, *a, **k):
            return _Q()

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            pass

        def refresh(self, obj):
            pass

    db = _DB()
    admin = auth_service.create_admin_if_not_exists(db)
    assert admin.must_change_password is True, "默认管理员创建后未要求改密"
    assert len(db.added) == 1


def test_admin_creation_does_not_log_password():
    """口令不能进日志：日志会被收集、转发、贴进工单。"""
    import inspect
    src = inspect.getsource(auth_service.create_admin_if_not_exists)
    log_lines = [ln for ln in src.splitlines() if "logger." in ln]
    assert log_lines, "找不到日志语句，用例失效"
    for ln in log_lines:
        assert "Admin@123456" not in ln, f"日志里仍有明文口令: {ln.strip()}"


def test_admin_creation_fallback_stores_salt_colon_hash():
    """分支二必须拼成 'salt:hash'，否则那条账号以后永远登不上。"""
    import inspect
    src = inspect.getsource(auth_service.create_admin_if_not_exists)
    assert 'f"{raw_salt}:{raw_hash}"' in src or "f'{raw_salt}:{raw_hash}'" in src, \
        "替代分支仍把 (hash, salt) 元组塞进 String 列"


def test_mark_default_password_users_only_flags_default_ones():
    """升级时逐个试算：只有还在用初始口令的账号被打标。"""
    hashed, salt = auth_service.hash_password(auth_service.DEFAULT_ADMIN_PASSWORD)
    other_hash, other_salt = auth_service.hash_password("MyOwn#Passw0rd")

    default_user = User(id=1, username="admin", password_hash=f"{salt}:{hashed}",
                        role=UserRole.ADMIN, must_change_password=False)
    own_user = User(id=2, username="someone", password_hash=f"{other_salt}:{other_hash}",
                    role=UserRole.USER, must_change_password=False)

    class _Q:
        def all(self_inner):
            return [default_user, own_user]

    class _DB:
        committed = 0

        def query(self, *a, **k):
            return _Q()

        def commit(self):
            _DB.committed += 1

    marked = auth_service.mark_default_password_users(_DB())
    assert marked == ["admin"], f"误伤了改过密码的账号: {marked}"
    assert default_user.must_change_password is True
    assert own_user.must_change_password is False


def test_mark_default_password_users_skips_already_flagged():
    hashed, salt = auth_service.hash_password(auth_service.DEFAULT_ADMIN_PASSWORD)
    flagged = User(id=1, username="admin", password_hash=f"{salt}:{hashed}",
                   role=UserRole.ADMIN, must_change_password=True)

    class _DB:
        def query(self, *a, **k):
            return SimpleNamespace(all=lambda: [flagged])

        def commit(self):
            pass

    assert auth_service.mark_default_password_users(_DB()) == []


# ═══════════════════════════════════════════════════════
#  三、请求拦截
# ═══════════════════════════════════════════════════════

def test_password_change_exempt_paths():
    assert auth_middleware.is_password_change_exempt("/api/client/password") is True
    assert auth_middleware.is_password_change_exempt("/api/auth/me") is True
    assert auth_middleware.is_password_change_exempt("/api/client/reports") is False
    assert auth_middleware.is_password_change_exempt("/api/admin/users") is False


def test_exempt_path_does_not_match_by_prefix_accident():
    """`/api/client/password` 不该顺带放过 `/api/client/passwordXXX` 这种拼接路径。"""
    assert auth_middleware.is_password_change_exempt("/api/client/passwordreset") is False


def _request(path):
    return Request({
        "type": "http", "method": "GET", "path": path,
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "root_path": "",
    })


def _run_current_user(monkeypatch, user, path):
    monkeypatch.setattr(auth_middleware, "SECRET_KEY", "test-secret-key")
    token = auth_middleware.create_access_token(
        {"user_id": user.id, "username": user.username, "role": str(user.role)})

    class _DB:
        def query(self, *a, **k):
            return SimpleNamespace(filter=lambda *a, **k: SimpleNamespace(first=lambda: user))

    creds = SimpleNamespace(credentials=token)
    return asyncio.run(auth_middleware.get_current_user(_request(path), creds, _DB()))


def _pending_user():
    return User(id=5, username="pending", password_hash="a:b", role=UserRole.USER,
                is_active=True, must_change_password=True)


def _normal_user():
    return User(id=6, username="normal", password_hash="a:b", role=UserRole.USER,
                is_active=True, must_change_password=False)


def test_pending_user_blocked_on_normal_endpoint(monkeypatch):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run_current_user(monkeypatch, _pending_user(), "/api/client/reports")
    assert exc.value.status_code == 403
    assert "初始密码" in str(exc.value.detail)


def test_blocked_response_carries_marker_header(monkeypatch):
    """前端靠这个头区分「要改密」和「登录失效」，缺了就会清 token 踢人 → 死循环。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run_current_user(monkeypatch, _pending_user(), "/api/client/reports")
    headers = exc.value.headers or {}
    assert headers.get("X-Password-Change-Required") == "1"


def test_pending_user_allowed_on_password_endpoint(monkeypatch):
    user = _run_current_user(monkeypatch, _pending_user(), "/api/client/password")
    assert user.username == "pending"


def test_pending_user_allowed_on_me(monkeypatch):
    user = _run_current_user(monkeypatch, _pending_user(), "/api/auth/me")
    assert user.username == "pending"


def test_normal_user_unaffected(monkeypatch):
    user = _run_current_user(monkeypatch, _normal_user(), "/api/client/reports")
    assert user.username == "normal"


def test_disabled_user_still_gets_403_before_password_check(monkeypatch):
    from fastapi import HTTPException
    user = _pending_user()
    user.is_active = False
    with pytest.raises(HTTPException) as exc:
        _run_current_user(monkeypatch, user, "/api/client/password")
    assert "禁用" in str(exc.value.detail), "禁用账号应优先报禁用，而不是被放行到改密接口"


# ═══════════════════════════════════════════════════════
#  四、改密后解锁 / 响应里带标志
# ═══════════════════════════════════════════════════════

def test_change_password_clears_flag():
    """改完必须解除，否则用户改完还是被自己的 403 拦着。"""
    import inspect
    from backend.routes import client_routes
    src = inspect.getsource(client_routes.change_password)
    assert "must_change_password = False" in src


def test_login_response_includes_flag(monkeypatch):
    """登录响应要带标志，前端才能直接把人送到改密面板。走真实 login 函数验返回值。"""
    from backend.routes import auth_routes

    pending = SimpleNamespace(id=9, username="u", role=UserRole.USER, tokens_used=0,
                              must_change_password=True)
    monkeypatch.setattr(auth_routes, "authenticate_user", lambda db, u, p: pending)
    monkeypatch.setattr(auth_routes, "create_access_token", lambda data: "access-token")
    monkeypatch.setattr(auth_routes, "create_refresh_token", lambda data: "refresh-token")
    monkeypatch.setattr(auth_routes, "_log_operation", lambda *a, **k: None)

    resp = auth_routes.login(auth_routes.LoginRequest(username="u", password="p"), object())
    assert resp["data"]["must_change_password"] is True
    assert resp["data"]["access_token"] == "access-token"


def test_login_response_flag_false_for_normal_user(monkeypatch):
    from backend.routes import auth_routes

    normal = SimpleNamespace(id=10, username="n", role=UserRole.USER, tokens_used=0,
                             must_change_password=False)
    monkeypatch.setattr(auth_routes, "authenticate_user", lambda db, u, p: normal)
    monkeypatch.setattr(auth_routes, "create_access_token", lambda data: "t")
    monkeypatch.setattr(auth_routes, "create_refresh_token", lambda data: "r")
    monkeypatch.setattr(auth_routes, "_log_operation", lambda *a, **k: None)

    resp = auth_routes.login(auth_routes.LoginRequest(username="n", password="p"), object())
    assert resp["data"]["must_change_password"] is False


def test_me_response_includes_flag():
    import inspect
    from backend.routes import auth_routes
    src = inspect.getsource(auth_routes.get_me)
    assert "must_change_password" in src


def test_admin_reset_password_sets_flag():
    """管理员重置密码后本人并不知道这串口令，必须强制其再改一次。"""
    import inspect
    from backend.routes import admin_routes
    src = inspect.getsource(admin_routes.admin_update_user)
    assert "must_change_password = True" in src


def test_admin_create_user_sets_flag():
    import inspect
    from backend.routes import auth_routes
    src = inspect.getsource(auth_routes.admin_create_user)
    assert "must_change_password=True" in src


def test_register_does_not_set_flag():
    """自助注册是用户自己设的密码，不该被强制改。"""
    import inspect
    from backend.routes import auth_routes
    src = inspect.getsource(auth_routes.register)
    assert "must_change_password" not in src


# ═══════════════════════════════════════════════════════
#  五、前端不对 403 误判（死循环回归）
#
#  真正跑 JS 的用例在 tests/test_shared_frontend.cjs（node --test）：
#  那里会构造 403 + X-Password-Change-Required 的响应，断言 token 不被清、
#  跳的是设置页而不是登录页。这里只留一条接线检查，防止两处口径漂移。
# ═══════════════════════════════════════════════════════

def test_frontend_header_name_matches_backend():
    from pathlib import Path
    api_src = (Path(__file__).resolve().parents[1] / "frontend/shared/api.js").read_text(encoding="utf-8")
    assert auth_middleware.PASSWORD_CHANGE_REQUIRED_CODE in api_src or \
        "X-Password-Change-Required" in api_src
    assert "X-Password-Change-Required" in api_src, \
        "前后端用的响应头名字不一致，前端识别不到"
