"""管理端「止血」批次的回归锁。

这一批修的全是「页面已经上线、但按下去什么也不会发生 / 管理员被锁在门外」的问题，
所以测试也只锁这些**用户可感知的契约**，不去测内部实现：

1. 被标记「待改初始口令」的管理员，必须有一条能走通的自助改密通道
   （否则除改密外的接口全 403，改密表单又挂在 403 的页面里 → 死锁）；
2. `/users/options` 必须早于 `/users/{user_id}` 命中，不能被 int 路径参数吃掉变 422；
3. 注销用户这种高危操作，审计日志必须落在**操作者**名下且真的写进去
   （原实现用刚被删除的用户 id 写 FK，必然失败还被静默吞掉）。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import OperationLog, User, UserRole
from backend.services import auth_service


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1
# 未被标记的管理员：用来验证「正常状态」下的业务接口，
# 否则所有请求都会被强制改密的护栏挡在 403（这正是本批要修的那个死锁）。
NORMAL_ADMIN_ID = 4


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'admin.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    hashed, salt = auth_service.hash_password("initpass123")
    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="admin", password_hash=f"{salt}:{hashed}",
                    role=UserRole.ADMIN, is_active=True, must_change_password=True))
        db.add(User(id=NORMAL_ADMIN_ID, username="ops", password_hash=f"{salt}:{hashed}",
                    role=UserRole.ADMIN, is_active=True, must_change_password=False,
                    tokens_used=900))
        # tokens_used 给不同的值：排序测试要能区分「全量排序」和「只排当前页」
        db.add(User(id=2, username="alice", password_hash="x:y", tokens_used=500,
                    role=UserRole.USER, is_active=True))
        db.add(User(id=3, username="bob", password_hash="x:y", tokens_used=100,
                    role=UserRole.USER, is_active=True))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID})}"}
    ok_headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': NORMAL_ADMIN_ID})}"}
    yield TestClient(app), headers, ok_headers, factory
    engine.dispose()


# ═══════════════════════════════════════════════════════
#  一、逃生通道本身
# ═══════════════════════════════════════════════════════

def test_admin_self_service_paths_are_exempt():
    """`/api/admin/me` 前缀必须放行，否则被强制改密的 admin 无路可走。"""
    assert auth.is_password_change_exempt("/api/admin/me") is True
    assert auth.is_password_change_exempt("/api/admin/me/password") is True


def test_exemption_does_not_leak_to_business_endpoints():
    """放行 `/api/admin/me` 不能顺带放过 `/api/admin/users` 或 `/api/admin/meX`。"""
    assert auth.is_password_change_exempt("/api/admin/users") is False
    assert auth.is_password_change_exempt("/api/admin/settings") is False
    assert auth.is_password_change_exempt("/api/admin/meX") is False


# ═══════════════════════════════════════════════════════
#  二、死锁场景端到端
# ═══════════════════════════════════════════════════════

def test_must_change_admin_is_blocked_with_marker_header(api):
    client, headers, _, _ = api
    resp = client.get("/api/admin/dashboard", headers=headers)
    assert resp.status_code == 403
    # 前端靠这个头区分「要改密」和「登录失效」，缺了它就会把人踢回登录页
    assert resp.headers.get("X-Password-Change-Required") == "1"


def test_must_change_admin_can_read_own_profile(api):
    client, headers, _, _ = api
    resp = client.get("/api/admin/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["must_change_password"] is True


def test_must_change_admin_can_change_own_password(api):
    client, headers, _, factory = api

    # 旧口令错 → 400，且不改变状态
    bad = client.put("/api/admin/me/password", headers=headers,
                     json={"old_password": "wrong-pass", "new_password": "newpass123"})
    assert bad.status_code == 400
    assert "旧密码" in bad.json()["detail"]

    ok = client.put("/api/admin/me/password", headers=headers,
                    json={"old_password": "initpass123", "new_password": "newpass123"})
    assert ok.status_code == 200

    # 改密会让旧令牌立刻作废（token_version 自增），
    # 服务端同时补发一枚新令牌，让当前这台设备无缝续用。
    old_token_still_works = client.get("/api/admin/dashboard", headers=headers)
    assert old_token_still_works.status_code == 401

    fresh = {"Authorization": f"Bearer {ok.json()['data']['access_token']}"}
    assert client.get("/api/admin/dashboard", headers=fresh).status_code == 200
    assert client.get("/api/admin/me", headers=fresh).json()["data"]["must_change_password"] is False

    with factory() as db:
        user = db.query(User).filter(User.id == ADMIN_ID).first()
        assert user.must_change_password is False
        salt, stored = user.password_hash.split(":", 1)
        assert auth_service.verify_password("newpass123", stored, salt)


def test_admin_password_change_rejects_weak_or_reused(api):
    client, headers, _, _ = api
    assert client.put("/api/admin/me/password", headers=headers,
                      json={"old_password": "initpass123", "new_password": "123"}).status_code == 400
    # 新旧相同也不行：等于没改，却把 must_change_password 清掉了
    assert client.put("/api/admin/me/password", headers=headers,
                      json={"old_password": "initpass123", "new_password": "initpass123"}).status_code == 400


# ═══════════════════════════════════════════════════════
#  三、用户选项接口（page_size=9999 的替代品）
# ═══════════════════════════════════════════════════════

def test_user_options_is_not_shadowed_by_path_param(api):
    """`/users/options` 若排在 `/users/{user_id}` 后面会被当成 int 解析 → 422。"""
    client, _, ok_headers, _ = api
    resp = client.get("/api/admin/users/options", headers=ok_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert {u["username"] for u in body["data"]["users"]} >= {"admin", "alice", "bob"}
    # 只回下拉需要的字段，不倒全量用户资料
    assert set(body["data"]["users"][0]) == {"id", "username", "role", "is_active"}


def test_user_options_accepts_role_filter(api):
    client, _, ok_headers, _ = api
    resp = client.get("/api/admin/users/options?role=user", headers=ok_headers)
    assert resp.status_code == 200
    names = {u["username"] for u in resp.json()["data"]["users"]}
    assert names == {"alice", "bob"}


def test_user_options_rejects_page_size_over_limit_contract(api):
    """回归：调用方原本传 page_size=9999，必须 422；选项接口不该有这个问题。"""
    client, _, ok_headers, _ = api
    assert client.get("/api/admin/users?page_size=9999", headers=ok_headers).status_code == 422
    assert client.get("/api/admin/users/options", headers=ok_headers).status_code == 200


# ═══════════════════════════════════════════════════════
#  四、注销用户的审计必须真的落库
# ═══════════════════════════════════════════════════════

def test_deactivate_writes_audit_log_under_operator(api):
    client, _, ok_headers, factory = api

    resp = client.post("/api/admin/users/2/deactivate", headers=ok_headers)
    assert resp.status_code == 200, resp.text

    with factory() as db:
        # 目标用户及其日志都没了
        assert db.query(User).filter(User.id == 2).first() is None
        # 但这次高危操作本身必须有记录，且记在操作者名下
        rows = db.query(OperationLog).filter(
            OperationLog.action == "admin_deactivate_user"
        ).all()
        assert len(rows) == 1
        log = rows[0]
        assert log.user_id == NORMAL_ADMIN_ID, "审计必须记在操作者名下（被删用户的 id 已违反外键）"
        assert log.username == "ops"
        assert log.resource_id == "2"
        assert "alice" in (log.detail or "")


# ═══════════════════════════════════════════════════════
#  五、Token 排序必须在 SQL 里做（跨页有效）
# ═══════════════════════════════════════════════════════

def test_user_sort_applies_across_pages_not_just_current_page(api):
    """回归：原来只对当前 20 条做前端排序，翻页后「谁最烧 Token」的答案就变了。"""
    client, _, ok_headers, _ = api

    page1 = client.get("/api/admin/users?page=1&page_size=2&sort=tokens_desc",
                       headers=ok_headers).json()["data"]["users"]
    page2 = client.get("/api/admin/users?page=2&page_size=2&sort=tokens_desc",
                       headers=ok_headers).json()["data"]["users"]

    assert [u["username"] for u in page1] == ["ops", "alice"]
    assert [u["username"] for u in page2] == ["bob", "admin"]


def test_user_sort_asc_and_unknown_key_falls_back(api):
    client, _, ok_headers, _ = api

    asc = client.get("/api/admin/users?page=1&page_size=4&sort=tokens_asc",
                     headers=ok_headers).json()["data"]["users"]
    assert [u["tokens_used"] for u in asc] == sorted(u["tokens_used"] for u in asc)

    # 未知排序键不能 500，退回默认（注册时间倒序）
    bad = client.get("/api/admin/users?sort=drop_table", headers=ok_headers)
    assert bad.status_code == 200
