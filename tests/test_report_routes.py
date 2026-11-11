"""报告域搬走之后的行为回归（批次 35）。

搬代码不改逻辑 —— 但"没改"必须用行为证明，不能只看 diff。这里挑的是
搬走后最容易「少一行」的四条链路：

1. 读报告的三级回退：数据库 → checkpoint → 磁盘备份；
2. 保存报告：写库 + 运行中拒绝（409 在 report_routes 里，靠 `_shared_state` 判断）；
3. 组装报告：拼完要落盘，否则下一次读还是旧报告；
4. 模块增删：加进去能列出来，删掉就查不到，重复删要 404。

`format-check` 要调真模型，不在单测里覆盖（由 `wf_e2e.py` 守）。
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.agents import performance_agent
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole, WorkflowSession


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext(element, compiler, **kwargs):
    return "TEXT"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'report.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    with factory.begin() as db:
        db.add(User(id=7, username="u7", password_hash="unused",
                    role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id="s1", user_id=7, project_name="某某项目",
                               status="completed", final_report="数据库里的旧报告"))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 7})}"}
    yield TestClient(app), headers, tmp_path / "backups"
    engine.dispose()


def _seed(backup_dir, **state):
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": "s1", "project_name": "某某项目", **state}
    (backup_dir / "s1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_report_prefers_database_over_backup(api):
    """用户在页面上改过报告后，读到的必须是数据库那一版，不是磁盘备份。"""
    client, headers, backup_dir = api
    _seed(backup_dir, final_report="磁盘备份里的旧报告")

    body = client.get("/api/agent/workflow/s1/report", headers=headers).json()

    assert body["data"]["content"] == "数据库里的旧报告"


def test_save_report_writes_through_to_report_endpoint(api):
    client, headers, backup_dir = api

    saved = client.post("/api/agent/workflow/save-report", headers=headers,
                        json={"session_id": "s1", "content": "# 人工改过的报告"})
    assert saved.status_code == 200

    body = client.get("/api/agent/workflow/s1/report", headers=headers).json()
    assert body["data"]["content"] == "# 人工改过的报告"


def test_assemble_report_persists_the_assembled_text(api):
    """组装完必须落盘：否则前端拿着组装结果、下一次刷新又变回旧报告。"""
    client, headers, backup_dir = api
    _seed(backup_dir, basic_info="基本情况正文", completed_steps=list(range(1, 15)))

    result = client.post("/api/agent/workflow/assemble-report", headers=headers,
                         json={"session_id": "s1"})

    assert result.status_code == 200, result.text
    assembled = result.json()["data"]["report"]
    assert "基本情况正文" in assembled
    assert client.get("/api/agent/workflow/s1/report", headers=headers).json()["data"][
        "content"] == assembled


def test_custom_module_add_list_delete_round_trip(api):
    client, headers, backup_dir = api
    _seed(backup_dir, basic_info_modules={})

    added = client.post("/api/agent/workflow/add-custom-module", headers=headers,
                        json={"session_id": "s1", "module_key": "custom_x", "module_name": "自定义模块"})
    assert added.status_code == 200, added.text
    keys = [item["key"] for item in client.get(
        "/api/agent/workflow/s1/modules", headers=headers).json()["data"]["modules"]]
    assert "custom_x" in keys

    assert client.post("/api/agent/workflow/delete-module", headers=headers,
                       json={"session_id": "s1", "module_key": "custom_x"}).status_code == 200
    keys = [item["key"] for item in client.get(
        "/api/agent/workflow/s1/modules", headers=headers).json()["data"]["modules"]]
    assert "custom_x" not in keys

    assert client.post("/api/agent/workflow/delete-module", headers=headers,
                       json={"session_id": "s1", "module_key": "custom_x"}).status_code == 404


def test_add_module_rejects_duplicate_key(api):
    """同名模块再加一次要 400 —— 否则前端会看到两条同名卡片，改一条另一条不动。"""
    client, headers, backup_dir = api
    _seed(backup_dir, basic_info_modules={})
    body = {"session_id": "s1", "module_key": "dup", "module_name": "重复模块"}

    assert client.post("/api/agent/workflow/add-custom-module", headers=headers, json=body).status_code == 200
    assert client.post("/api/agent/workflow/add-custom-module", headers=headers, json=body).status_code == 400


def test_export_style_round_trip(api):
    """导出样式跟着导出域搬走了，存进去要读得出来。"""
    client, headers, backup_dir = api
    _seed(backup_dir, basic_info="正文")
    style = {"font_family": "SimHei", "font_size": 14, "line_spacing": 2.0, "page_margin": 3.0}

    saved = client.post("/api/agent/workflow/export-style", headers=headers,
                        json={"session_id": "s1", **style})
    assert saved.status_code == 200, saved.text

    read_back = client.get("/api/agent/workflow/s1/export-style", headers=headers).json()
    assert read_back["data"] == style


def test_report_order_round_trip(api):
    client, headers, backup_dir = api
    _seed(backup_dir, basic_info="正文")

    default = client.get("/api/agent/workflow/s1/report-order", headers=headers).json()
    assert default["is_custom"] is False
    assert len(default["data"]) >= 5

    custom = default["data"][::-1]
    assert client.post("/api/agent/workflow/report-order", headers=headers,
                       json={"session_id": "s1", "sections": custom}).status_code == 200

    saved = client.get("/api/agent/workflow/s1/report-order", headers=headers).json()
    assert saved["is_custom"] is True
    assert [item["key"] for item in saved["data"]] == [item["key"] for item in custom]


def test_format_check_does_not_block_the_event_loop(monkeypatch):
    """慢的模型调用必须在线程池里跑，否则一个 format-check 会把整个服务卡住。

    批次 36 验证时踩到：`wf_e2e` 的 format-check 客户端超时之后，**第二个请求连服务都
    连不上** —— 因为 `async def` 里同步调 `model.invoke`，事件循环被占住，所有请求排队。
    """
    import asyncio
    import threading

    from backend.routes import report_routes

    release = threading.Event()

    class SlowModel:
        def __init__(self, *args, **kwargs):
            pass

        def invoke(self, prompt):
            release.wait(5)
            return type("R", (), {"content": "校对结果"})()

    monkeypatch.setattr(report_routes, "ChatOpenAI", SlowModel)
    monkeypatch.setattr(report_routes, "_get_workflow_values",
                        lambda sid: {"final_report": "# 报告"})
    monkeypatch.setattr("backend.utils.llm_helper.get_model_credentials",
                        lambda name: {"api_key": "k", "base_url": ""})

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        timer = threading.Timer(0.4, release.set)
        timer.start()
        try:
            await report_routes.check_report_format("s1")
        finally:
            task.cancel()
        return ticks

    # 阻塞版：0.4 秒里 ticker 一次都跑不到（事件循环被占住）
    assert asyncio.run(scenario()) >= 3
