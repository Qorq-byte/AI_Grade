"""通知批量撤回（批次 11 / P4-6）。

原来只能一条一条撤。后端其实早就有「按标题全删」的接口，界面上没接；而且
「按标题全删」对「只撤其中几条」的场景太粗暴 —— 所以补了按 id 批量。

这里的测试重点不是「删掉了没有」，而是**别删多了**：勾了 3 条不能连带把别的
用户的同名通知一起撤掉。
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
from backend.models.user_models import Notification, OperationLog, User, UserRole


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'notif.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(id=2, username="alice", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        db.add(User(id=3, username="bob", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        # 同一个标题发给两个人 —— 用于验证「按 id 撤」不会波及另一个
        for uid in (2, 3):
            db.add(Notification(id=uid, user_id=uid, title="系统维护", content="今晚停机"))
        db.add(Notification(id=10, user_id=2, title="配额提醒", content="Token 快用完了"))
        db.add(Notification(id=11, user_id=3, title="配额提醒", content="Token 快用完了"))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


def test_batch_withdraw_only_touches_selected_ids(api):
    client, headers, factory = api
    resp = client.post("/api/admin/notifications/batch-withdraw",
                       headers=headers, json={"ids": [2, 10]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] == 2

    with factory() as db:
        left = sorted(n.id for n in db.query(Notification).all())
    # 同标题的 3（发给 bob 的「系统维护」）和 11 必须还在
    assert left == [3, 11]


def test_batch_withdraw_by_title_still_works(api):
    client, headers, factory = api
    resp = client.post("/api/admin/notifications/batch-withdraw",
                       headers=headers, json={"title": "配额提醒"})

    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 2
    with factory() as db:
        assert sorted(n.id for n in db.query(Notification).all()) == [2, 3]


def test_empty_request_is_rejected(api):
    """既不给 id 也不给标题，等于「删除全部」—— 必须挡住。"""
    client, headers, factory = api
    resp = client.post("/api/admin/notifications/batch-withdraw",
                       headers=headers, json={})

    assert resp.status_code == 400
    with factory() as db:
        assert db.query(Notification).count() == 4


def test_batch_withdraw_is_audited(api):
    client, headers, factory = api
    client.post("/api/admin/notifications/batch-withdraw",
                headers=headers, json={"ids": [2]})

    with factory() as db:
        log = db.query(OperationLog).filter(
            OperationLog.action == "admin_batch_withdraw_notifications").first()
        assert log is not None
        assert "撤回通知" in (log.detail or "")


def test_batch_withdraw_requires_admin(api):
    client, _, _ = api
    token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    resp = client.post("/api/admin/notifications/batch-withdraw",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"ids": [2]})
    assert resp.status_code == 403
