"""管理端查询预算（批次 4 · 性能）。

这一批改的都是「结果一样、但绕了远路」的地方，所以测试锁的不是返回值，而是
**一次请求打了多少次库**。断言给了一点余量（鉴权、count 之类会浮动），
但足够在 N+1 或 42 连发回来时立刻变红。

被锁的三处：
- `/step-config`：原本 14 步 × 3 张表 = 42 条 COUNT
- `/dashboard`：原本 8 条独立聚合
- `/notifications`：原本每行懒加载一次 `user`（N+1）
"""
import pytest
from contextlib import contextmanager
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import (
    KnowledgeBase, Notification, PromptTemplate, ReportModuleSequence,
    User, UserRole, WorkflowSession,
)


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'perf.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True, tokens_used=1234))
        # 10 个用户 + 每人一条通知：N+1 会在这里现形
        for i in range(2, 12):
            db.add(User(id=i, username=f"u{i}", password_hash="x:y",
                        role=UserRole.USER, is_active=True, tokens_used=i * 10))
            db.add(Notification(user_id=i, title=f"通知{i}", content="正文"))
        # 14 步都有 prompt + kb + 模块，逼出满额的 COUNT
        for step in range(1, 15):
            db.add(PromptTemplate(step_number=step, step_name=f"S{step}",
                                  prompt_content="p", is_system=True))
            db.add(KnowledgeBase(name=f"k{step}", content="c", step_number=step))
            db.add(ReportModuleSequence(step_number=step, module_key=f"m{step}",
                                        module_name=f"M{step}", sort_order=step))
        for status, n in (("running", 2), ("created", 1), ("completed", 3), ("failed", 1)):
            for _ in range(n):
                db.add(WorkflowSession(session_id=f"{status}-{_}-{n}",
                                       project_name="p", status=status))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, engine, factory
    engine.dispose()


@contextmanager
def count_queries(engine):
    """上下文管理器：统计块内真正打到库上的 SELECT 次数。"""
    calls = []

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            calls.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield calls
    finally:
        event.remove(engine, "before_cursor_execute", _before)


# ═══════════════════════════════════════════════════════
#  步骤配置：42 条 COUNT → 3 条 GROUP BY
# ═══════════════════════════════════════════════════════

def test_step_config_uses_group_by_not_42_counts(api):
    client, headers, engine, _ = api

    with count_queries(engine) as calls:
        resp = client.get("/api/admin/step-config", headers=headers)

    assert resp.status_code == 200
    steps = resp.json()["data"]
    assert len(steps) == 14
    # 14 步每步都有数据，说明 GROUP BY 的结果被正确摊回 14 行
    assert all(s["prompt_count"] == 1 and s["kb_count"] == 1 and s["module_count"] == 1
               for s in steps)
    # 鉴权 1 + 三条分组统计。留点余量给未来的 schema 变化，但远小于 42。
    assert len(calls) <= 6, f"打了 {len(calls)} 次库，GROUP BY 没生效？\n" + "\n".join(calls[:5])


# ═══════════════════════════════════════════════════════
#  仪表盘：8 条聚合 → 3 条
# ═══════════════════════════════════════════════════════

def test_dashboard_query_budget_and_numbers(api):
    client, headers, engine, _ = api

    with count_queries(engine) as calls:
        resp = client.get("/api/admin/dashboard", headers=headers)

    assert resp.status_code == 200
    ov = resp.json()["data"]["overview"]
    # 合并查询不能把口径改错：7 个工作流里 3 running-ish / 3 completed / 1 failed
    assert ov["total_projects"] == 7
    assert ov["active_workflows"] == 3
    assert ov["completed_workflows"] == 3
    assert ov["failed_workflows"] == 1
    assert ov["total_users"] == 11
    assert ov["total_tokens_used"] == 1234 + sum(i * 10 for i in range(2, 12))
    assert len(calls) <= 7, f"打了 {len(calls)} 次库\n" + "\n".join(calls[:5])


# ═══════════════════════════════════════════════════════
#  通知列表：N+1 → joinedload
# ═══════════════════════════════════════════════════════

def test_notification_list_does_not_scale_with_row_count(api):
    client, headers, engine, _ = api

    with count_queries(engine) as calls:
        resp = client.get("/api/admin/notifications?page=1&page_size=100", headers=headers)

    assert resp.status_code == 200
    rows = resp.json()["data"]["notifications"]
    assert len(rows) == 10
    # joinedload 生效时行数再多也就「鉴权 + count + 一次 join」
    assert len(calls) <= 5, f"打了 {len(calls)} 次库，N+1 回来了？\n" + "\n".join(calls[:6])
    assert all(r["username"].startswith("u") for r in rows)
