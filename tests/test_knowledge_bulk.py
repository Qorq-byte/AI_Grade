"""知识库批量导入（批次 11 / P4-7）。

原来只能一条一条手录。批量导入的关键不是「能导」，而是**脏数据不要整批回滚** ——
从 Excel 导出来的数据总有一两行缺列，让人回去手改 200 行是不可接受的。

所以后端逐条校验：合格的入库、重复的跳过、不合格的返回行号和原因。
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
from backend.models.user_models import KnowledgeBase, OperationLog, User, UserRole


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'kb.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SESSION", None, raising=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(id=2, username="alice", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        db.add(KnowledgeBase(name="已有条目", content="原有内容", step_number=3))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


def _import(client, headers, items, skip=True):
    return client.post("/api/admin/knowledge/bulk", headers=headers,
                       json={"items": items, "skip_existing": skip})


def test_bulk_import_creates_all_items(api):
    client, headers, factory = api
    resp = _import(client, headers, [
        {"name": "条目A", "content": "内容A", "category": "绩效", "step_number": 3},
        {"name": "条目B", "content": "内容B", "indicator_level": 2},
    ])

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["created"] == 2

    with factory() as db:
        rows = {k.name: k for k in db.query(KnowledgeBase).all()}
        assert rows["条目A"].category == "绩效"
        assert rows["条目A"].step_number == 3
        assert rows["条目B"].indicator_level == 2
        assert rows["条目B"].step_number is None


def test_duplicate_names_are_skipped_not_duplicated(api):
    client, headers, factory = api
    resp = _import(client, headers, [
        {"name": "已有条目", "content": "新的内容"},
        {"name": "全新条目", "content": "内容"},
    ])

    assert resp.json()["data"]["created"] == 1
    assert resp.json()["data"]["skipped"] == 1
    with factory() as db:
        assert db.query(KnowledgeBase).filter(KnowledgeBase.name == "已有条目").count() == 1


def test_skip_existing_off_allows_same_name(api):
    """同一条知识在不同步骤下可能都要用，允许重复时不能硬拦。"""
    client, headers, factory = api
    resp = _import(client, headers, [{"name": "已有条目", "content": "另一份"}], skip=False)

    assert resp.json()["data"]["created"] == 1
    with factory() as db:
        assert db.query(KnowledgeBase).filter(KnowledgeBase.name == "已有条目").count() == 2


def test_bad_rows_are_reported_without_blocking_good_ones(api):
    """一行脏数据不该让另外两行白导。"""
    client, headers, factory = api
    resp = _import(client, headers, [
        {"name": "", "content": "没名字"},
        {"name": "缺内容", "content": "   "},
        {"name": "步骤越界", "content": "x", "step_number": 99},
        {"name": "层级越界", "content": "x", "indicator_level": 5},
        {"name": "正常条目", "content": "正常内容"},
    ])

    data = resp.json()["data"]
    assert data["created"] == 1
    assert len(data["failed"]) == 4
    assert [f["row"] for f in data["failed"]] == [1, 2, 3, 4]
    assert "名称为空" in data["failed"][0]["error"]
    assert "1-14" in data["failed"][2]["error"]
    assert "1-3" in data["failed"][3]["error"]

    with factory() as db:
        assert db.query(KnowledgeBase).filter(KnowledgeBase.name == "正常条目").count() == 1


def test_empty_and_oversized_batches_are_rejected(api):
    client, headers, _ = api
    assert _import(client, headers, []).status_code == 400

    too_many = [{"name": f"n{i}", "content": "c"} for i in range(501)]
    resp = _import(client, headers, too_many)
    assert resp.status_code == 400
    assert "500" in resp.json()["detail"]


def test_bulk_import_is_audited(api):
    client, headers, factory = api
    _import(client, headers, [{"name": "条目C", "content": "内容C"}])

    with factory() as db:
        log = db.query(OperationLog).filter(
            OperationLog.action == "admin_bulk_import_knowledge").first()
        assert log is not None
        assert "新增 1" in (log.detail or "")


def test_bulk_import_requires_admin(api):
    client, _, _ = api
    token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    resp = client.post("/api/admin/knowledge/bulk",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"items": [{"name": "x", "content": "y"}]})
    assert resp.status_code == 403
