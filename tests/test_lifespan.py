"""启动/关闭改用 lifespan，后台清理任务必须持引用并在关闭时取消（批次 23 = A3）。

改前的样子（`backend/main.py`）：

  * `@app.on_event("startup")` / `@app.on_event("shutdown")` 各一处——FastAPI 已经弃用，
    测试与启动日志里一直在报 deprecation；
  * `_start_cleanup_scheduler()` 直接 `asyncio.create_task(_cleanup_loop())`，**不持有引用**：
    Python 只保留对任务的弱引用，事件循环之外没人拿住它，随时可能被 GC；
  * `shutdown_event()` 只打一行日志，任务不会被取消——进程退出靠操作系统收尸。

口径：
  * `app.router.on_startup` / `on_shutdown` 必须是空的（不再用 on_event）；
  * 任务要有引用、要命名，重复调用不会起第二个；
  * 关闭时 **cancel + await**（只 cancel 不 await 等于把清理逻辑留在半路），
    且异常路径也要执行。
"""
import asyncio

import pytest

from backend import main


# ═══════════════════════════════════════════════════════
#  一、不再用 on_event
# ═══════════════════════════════════════════════════════

def test_no_deprecated_on_event_handlers():
    assert main.app.router.on_startup == [], "on_event(startup) 尚未迁移到 lifespan"
    assert main.app.router.on_shutdown == []


def test_app_runs_lifespan_on_startup_and_shutdown(monkeypatch):
    """行为验证：进入应用上下文就跑启动、退出就跑关闭（on_event 时代这里会是空的）。"""
    from fastapi.testclient import TestClient

    order = []

    async def fake_startup():
        order.append("startup")

    async def fake_shutdown():
        order.append("shutdown")

    monkeypatch.setattr(main, "_on_startup", fake_startup)
    monkeypatch.setattr(main, "_on_shutdown", fake_shutdown)

    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "healthy"
        assert order == ["startup"]

    assert order == ["startup", "shutdown"]


def test_lifespan_is_async_context_manager():
    cm = main.lifespan(main.app)
    assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")


# ═══════════════════════════════════════════════════════
#  二、任务要被拿住，且只有一份
# ═══════════════════════════════════════════════════════

def test_start_cleanup_scheduler_keeps_reference():
    async def scenario():
        task = main._start_cleanup_scheduler()
        try:
            assert task is not None
            assert main._CLEANUP_TASK is task, "任务没有存到模块级变量，随时可能被 GC"
            assert not task.done()
            assert task.get_name() == "file-cleanup"
        finally:
            await main._stop_cleanup_scheduler()

    asyncio.run(scenario())


def test_start_cleanup_scheduler_is_idempotent():
    async def scenario():
        first = main._start_cleanup_scheduler()
        second = main._start_cleanup_scheduler()
        try:
            assert first is second, "重复启动会多跑一条清理循环"
        finally:
            await main._stop_cleanup_scheduler()

    asyncio.run(scenario())


def test_stop_cleanup_scheduler_cancels_task():
    async def scenario():
        task = main._start_cleanup_scheduler()
        stopped = await main._stop_cleanup_scheduler()
        assert stopped is True
        assert task.cancelled()
        assert main._CLEANUP_TASK is None

    asyncio.run(scenario())


def test_stop_cleanup_scheduler_awaits_task():
    """只 cancel 不 await 的话，清理逻辑会停在半路；这里用 finally 标记验证真的等到了。"""
    async def scenario():
        done = []

        async def worker():
            try:
                await asyncio.sleep(3600)
            finally:
                done.append(True)

        main._CLEANUP_TASK = asyncio.create_task(worker(), name="file-cleanup")
        await asyncio.sleep(0)  # 让它真的进到 try 里，否则 cancel 连 finally 都不会跑
        await main._stop_cleanup_scheduler()
        assert done == [True], "取消之后没有等任务收尾"

    asyncio.run(scenario())


def test_stop_cleanup_scheduler_safe_when_not_started():
    async def scenario():
        main._CLEANUP_TASK = None
        assert await main._stop_cleanup_scheduler() is False

    asyncio.run(scenario())


def test_stop_cleanup_scheduler_safe_when_already_done():
    async def scenario():
        async def instant():
            return None

        main._CLEANUP_TASK = asyncio.create_task(instant(), name="file-cleanup")
        await asyncio.sleep(0)
        assert await main._stop_cleanup_scheduler() is False

    asyncio.run(scenario())


# ═══════════════════════════════════════════════════════
#  三、lifespan 的进入与退出
# ═══════════════════════════════════════════════════════

def test_lifespan_runs_startup_then_shutdown(monkeypatch):
    order = []

    async def fake_startup():
        order.append("startup")
        main._start_cleanup_scheduler()

    async def fake_shutdown():
        order.append("shutdown")
        await main._stop_cleanup_scheduler()

    monkeypatch.setattr(main, "_on_startup", fake_startup)
    monkeypatch.setattr(main, "_on_shutdown", fake_shutdown)

    async def scenario():
        async with main.lifespan(main.app):
            order.append("body")
            assert main._CLEANUP_TASK is not None
        assert main._CLEANUP_TASK is None

    asyncio.run(scenario())
    assert order == ["startup", "body", "shutdown"]


def test_lifespan_stops_task_even_when_body_raises(monkeypatch):
    async def fake_startup():
        main._start_cleanup_scheduler()

    async def fake_shutdown():
        await main._stop_cleanup_scheduler()

    monkeypatch.setattr(main, "_on_startup", fake_startup)
    monkeypatch.setattr(main, "_on_shutdown", fake_shutdown)

    async def scenario():
        with pytest.raises(RuntimeError):
            async with main.lifespan(main.app):
                assert main._CLEANUP_TASK is not None
                raise RuntimeError("工作流在这一刻崩了")
        assert main._CLEANUP_TASK is None, "异常退出时任务没被取消"

    asyncio.run(scenario())


def test_lifespan_shutdown_survives_stop_errors(monkeypatch):
    """关闭阶段的异常不能吞掉启动阶段的成果，也不能让进程起不来。"""
    async def fake_startup():
        return None

    async def boom():
        raise RuntimeError("清理任务取消时炸了")

    monkeypatch.setattr(main, "_on_startup", fake_startup)
    monkeypatch.setattr(main, "_stop_cleanup_scheduler", boom)

    async def scenario():
        async with main.lifespan(main.app):
            pass

    asyncio.run(scenario())  # 不抛


# ═══════════════════════════════════════════════════════
#  四、启动内容没有被搬丢
# ═══════════════════════════════════════════════════════

def _fake_settings():
    """settings 是 pydantic 的 BaseSettings，直接 setattr 会在校验阶段炸；整对象替换最省事。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        APP_NAME="测试服务", APP_VERSION="0.0.0", LANGCHAIN_TRACING_V2=False,
        DEFAULT_MODEL="fixture", validate_required_settings=lambda: [],
    )


def test_startup_still_initializes_db(monkeypatch):
    """启动时会依次做 init_db → 建管理员 → 起清理任务。

    注意 `SessionLocal()` 也得替掉：它藏在 `from backend.database import SessionLocal`
    里面，不替的话这条用例会去**真连 MySQL**——本机有库时它照样通过，
    在 CI（把 MYSQL_PORT 指到 1）上就报 AssertionError。批次 28 加 CI 时被抓出来的，
    属于"用例靠环境才绿"的典型：它测的是"我机器上有库"。
    """
    calls = []

    class _DummySession:
        def close(self):
            calls.append("db_close")

    monkeypatch.setattr("backend.database.init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr("backend.database.SessionLocal", lambda: _DummySession())
    monkeypatch.setattr("backend.services.auth_service.create_admin_if_not_exists",
                        lambda db: calls.append("admin"))
    monkeypatch.setattr("backend.services.auth_service.mark_default_password_users",
                        lambda db: calls.append("mark"))
    monkeypatch.setattr(main, "_start_cleanup_scheduler", lambda: calls.append("scheduler"))
    monkeypatch.setattr(main, "settings", _fake_settings())

    asyncio.run(main._on_startup())

    assert "init_db" in calls
    assert "admin" in calls
    assert "scheduler" in calls
    # 顺序：先建表、再建管理员、最后起清理任务
    assert calls.index("init_db") < calls.index("admin") < calls.index("scheduler")


def test_startup_skips_scheduler_when_db_fails(monkeypatch):
    """沿用改前的行为：DB 初始化失败时不起清理任务（它一样连不上库）。"""
    calls = []

    def boom():
        raise RuntimeError("数据库连不上")

    monkeypatch.setattr("backend.database.init_db", boom)
    monkeypatch.setattr(main, "_start_cleanup_scheduler", lambda: calls.append("scheduler"))
    monkeypatch.setattr(main, "settings", _fake_settings())

    asyncio.run(main._on_startup())  # 不抛：启动失败只记日志

    assert calls == []
