"""审计日志导出（批次 11 / P4-5）。

合规场景要的是「把这段时间的操作记录给我一份」，一页 50 条地翻是没法交差的。

导出接口看着简单，实际有三个坑：CSV 转义、Excel 公式注入、以及「导出行为本身
要不要留痕」。
"""
import json

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


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(id=2, username="alice", password_hash="x:y",
                    role=UserRole.USER, is_active=True))
        db.add(OperationLog(user_id=2, username="alice", action="login",
                            resource_type="user", resource_id="2", detail="正常登录"))
        db.add(OperationLog(user_id=2, username="alice", action="workflow_start",
                            resource_type="workflow", resource_id="s1",
                            detail='带逗号, 带"引号" 的详情'))
        db.add(OperationLog(user_id=ADMIN_ID, username="ops", action="=SUM(A1)",
                            resource_type="user", resource_id="1",
                            detail="@危险公式"))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


def test_export_returns_csv_with_bom(api):
    client, headers, _ = api
    resp = client.get("/api/admin/audit-logs/export", headers=headers)

    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    # BOM 是 Excel 正确识别 UTF-8 的唯一办法
    assert resp.content.startswith("﻿".encode("utf-8"))


def test_export_contains_all_rows_and_header(api):
    client, headers, _ = api
    text = client.get("/api/admin/audit-logs/export", headers=headers).content.decode("utf-8")

    assert "时间,操作,用户,用户ID,资源类型,资源ID,详情,IP" in text
    assert "login" in text
    assert "workflow_start" in text
    # 表头 + 3 条数据
    assert len([l for l in text.strip().split("\r\n") if l]) == 4


def test_export_respects_filters(api):
    client, headers, _ = api
    resp = client.get("/api/admin/audit-logs/export?action=login", headers=headers)
    text = resp.content.decode("utf-8")

    assert "login" in text
    assert "workflow_start" not in text
    assert resp.headers["X-Export-Rows"] == "1"
    assert resp.headers["X-Export-Total"] == "1"


def test_special_characters_are_quoted(api):
    """详情里有逗号和引号，不转义就会把一行撑成好几列。"""
    client, headers, _ = api
    text = client.get("/api/admin/audit-logs/export", headers=headers).content.decode("utf-8")

    assert '"带逗号, 带""引号"" 的详情"' in text


def test_formula_injection_is_neutralised(api):
    """Excel 会把 = + - @ 开头的单元格当公式执行。"""
    client, headers, _ = api
    text = client.get("/api/admin/audit-logs/export", headers=headers).content.decode("utf-8")

    assert "'=SUM(A1)" in text
    assert "'@危险公式" in text


def test_export_itself_is_audited(api):
    """导出了一份操作记录，这个动作本身也要能被查到。"""
    client, headers, factory = api
    client.get("/api/admin/audit-logs/export", headers=headers)

    with factory() as db:
        log = db.query(OperationLog).filter(
            OperationLog.action == "admin_export_audit_logs").first()
        assert log is not None
        assert "导出审计日志" in (log.detail or "")


def test_export_requires_admin(api):
    client, _, _ = api
    token = auth.create_access_token({"user_id": 2, "role": "user", "ver": 0})
    resp = client.get("/api/admin/audit-logs/export",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
