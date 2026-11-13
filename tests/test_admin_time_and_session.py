"""第二批「数据正确性」的回归锁：时区、注销残留、会话失效。

三件事都是「不报错、但结果不对」的类型，靠肉眼很难发现，所以必须用测试钉住：

1. 库里存的是 naive UTC，序列化出来却没带 `Z` → 浏览器按本地时间解析 → 全部早 8 小时；
   而且「今日活跃」的边界取的是 UTC 零点（= 北京 8 点）。
2. 注销用户只删库不删盘 → `uploads/` 和 `backend/exports/` 里留一堆没人认领的文件。
3. 改密后旧 JWT 仍然能用（现在是禁用即时生效，但改密不会踢掉已登录的会话）。
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole
from backend.services import auth_service
from backend.services.cleanup_service import CleanupService
from backend.utils.time_utils import local_day_start_utc, utc_iso, utcnow


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'b2.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    hashed, salt = auth_service.hash_password("initpass123")
    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash=f"{salt}:{hashed}",
                    role=UserRole.ADMIN, is_active=True, must_change_password=False))
        db.add(User(id=2, username="alice", password_hash=f"{salt}:{hashed}",
                    role=UserRole.USER, is_active=True))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


# ═══════════════════════════════════════════════════════
#  一、时间口径
# ═══════════════════════════════════════════════════════

def test_utc_iso_always_carries_z():
    """没有 Z 的串在浏览器里会被当成本地时间 —— 这是 8 小时偏差的根因。"""
    assert utc_iso(utcnow()).endswith("Z")
    assert utc_iso(utcnow()).endswith("+00:00") is False
    assert utc_iso(None) is None


def test_utc_iso_normalises_aware_and_naive_to_the_same_instant():
    from datetime import datetime, timezone
    naive = datetime(2026, 11, 13, 11, 0, 0)
    aware = datetime(2026, 11, 13, 11, 0, 0, tzinfo=timezone.utc)
    assert utc_iso(naive) == utc_iso(aware) == "2026-11-13T11:00:00Z"


def test_local_day_start_is_beijing_midnight_in_utc():
    """「今天」的边界是北京 0 点 = 前一天 16:00 UTC，不是 UTC 0 点。"""
    start = local_day_start_utc()
    assert start.hour == 16 and start.minute == 0 and start.second == 0
    assert start < utcnow()

    week = local_day_start_utc(7)
    assert (start - week) == timedelta(days=7)


def test_dashboard_today_uses_beijing_boundary(api):
    """北京 0:30 登录的用户属于「今天」；北京昨天 23:00 的不属于。

    按原来的 UTC 零点边界，第一个会被漏掉、第二个会被多算 —— 两个方向都错。
    """
    client, headers, factory = api
    today_start = local_day_start_utc()

    with factory.begin() as db:
        # 北京今天 00:30（= UTC 前一天 16:30）
        db.add(User(id=10, username="early_bird", password_hash="x:y", role=UserRole.USER,
                    is_active=True, last_login_at=today_start + timedelta(minutes=30)))
        # 北京昨天 23:00（= UTC 前一天 15:00）
        db.add(User(id=11, username="night_owl", password_hash="x:y", role=UserRole.USER,
                    is_active=True, last_login_at=today_start - timedelta(hours=1)))

    body = client.get("/api/admin/dashboard", headers=headers).json()
    assert body["data"]["overview"]["active_users_today"] == 1


def test_admin_api_emits_z_suffixed_timestamps(api):
    """所有管理端时间字段都必须是带 Z 的串，否则前端还得靠猜。"""
    client, headers, _ = api
    users = client.get("/api/admin/users", headers=headers).json()["data"]["users"]
    assert users
    for u in users:
        if u["created_at"]:
            assert u["created_at"].endswith("Z"), u["created_at"]


# ═══════════════════════════════════════════════════════
#  二、注销必须在磁盘上也删干净
# ═══════════════════════════════════════════════════════

def _make_artifacts(root, uid):
    uploads = root / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"u{uid}_aaa_report.txt").write_text("mine", encoding="utf-8")
    (uploads / f"u{uid}_bbb_report.txt").write_text("mine too", encoding="utf-8")
    export_dir = root / "backend" / "exports" / str(uid) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "报告.docx").write_text("doc", encoding="utf-8")


def test_purge_removes_only_this_users_files(tmp_path):
    _make_artifacts(tmp_path, 7)
    _make_artifacts(tmp_path, 8)

    stats = CleanupService.purge_user_disk_artifacts(7, root=tmp_path)

    assert stats["files"] == 2
    assert stats["bytes"] == len("mine") + len("mine too")
    assert stats["dirs"] == 1
    assert stats["errors"] == []

    left = sorted(p.name for p in (tmp_path / "uploads").iterdir())
    assert left == ["u8_aaa_report.txt", "u8_bbb_report.txt"]
    assert not (tmp_path / "backend" / "exports" / "7").exists()
    assert (tmp_path / "backend" / "exports" / "8" / "exports" / "报告.docx").exists()


def test_purge_is_idempotent_and_survives_missing_dirs(tmp_path):
    assert CleanupService.purge_user_disk_artifacts(99, root=tmp_path)["files"] == 0
    _make_artifacts(tmp_path, 99)
    assert CleanupService.purge_user_disk_artifacts(99, root=tmp_path)["files"] == 2
    assert CleanupService.purge_user_disk_artifacts(99, root=tmp_path)["files"] == 0


def test_deactivate_calls_disk_purge(api, monkeypatch):
    """接口层必须真的调到这一步，否则前面写得再对也没用。"""
    client, headers, _ = api
    called = {}

    def fake_purge(user_id, root=None):
        called["user_id"] = user_id
        return {"files": 3, "bytes": 2048, "dirs": 1, "errors": []}

    monkeypatch.setattr(CleanupService, "purge_user_disk_artifacts", staticmethod(fake_purge))

    resp = client.post("/api/admin/users/2/deactivate", headers=headers)
    assert resp.status_code == 200, resp.text
    assert called["user_id"] == 2
    assert resp.json()["data"]["deleted_files"] == 3
    assert "磁盘文件均已删除" in resp.json()["message"]


# ═══════════════════════════════════════════════════════
#  三、改密后旧会话立即失效
# ═══════════════════════════════════════════════════════

def test_change_password_kills_old_tokens_but_returns_a_working_one(api):
    client, _, factory = api
    old = auth.create_access_token({"user_id": 2, "username": "alice", "role": "user", "ver": 0})
    old_h = {"Authorization": f"Bearer {old}"}

    assert client.get("/api/auth/me", headers=old_h).status_code == 200

    resp = client.put("/api/client/password", headers=old_h,
                      json={"old_password": "initpass123", "new_password": "brandnew123"})
    assert resp.status_code == 200, resp.text

    # 旧令牌立刻失效
    assert client.get("/api/auth/me", headers=old_h).status_code == 401

    # 但改动的那台设备能无缝续用（服务端补发了新令牌）
    new = resp.json()["data"]["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200

    with factory() as db:
        assert db.query(User).filter(User.id == 2).first().token_version == 1


def test_old_refresh_token_dies_with_the_password(api):
    """只废 access token 不够：偷到 refresh token 的人还能续命。"""
    client, _, _ = api
    stale_refresh = auth.create_refresh_token({"user_id": 2, "ver": 0})

    old = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    assert client.put("/api/client/password",
                      headers={"Authorization": f"Bearer {old}"},
                      json={"old_password": "initpass123", "new_password": "brandnew123"}).status_code == 200

    resp = client.post(f"/api/auth/refresh?refresh_token_str={stale_refresh}")
    assert resp.status_code == 401
    assert "失效" in resp.json()["detail"]


def test_admin_reset_forces_target_sessions_out(api):
    client, headers, factory = api
    target_token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    target_h = {"Authorization": f"Bearer {target_token}"}
    assert client.get("/api/auth/me", headers=target_h).status_code == 200

    resp = client.put("/api/admin/users/2", headers=headers, json={"password": "resetsbyadmin1"})
    assert resp.status_code == 200, resp.text

    # 管理员重置了别人的口令，那人已登录的会话必须立刻断掉
    assert client.get("/api/auth/me", headers=target_h).status_code == 401

    with factory() as db:
        u = db.query(User).filter(User.id == 2).first()
        assert u.token_version == 1
        assert u.must_change_password is True


def test_tokens_without_ver_still_work_for_untouched_accounts(api):
    """升级不能把所有人踢下线：老令牌没有 ver，而老账号的 token_version 是 0。"""
    client, _, _ = api
    legacy = auth.create_access_token({"user_id": 2, "role": "user"})  # 没有 ver
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 200
