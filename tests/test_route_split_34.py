"""批次 34 的验收：`_workflow_deps` 抽口 + 「导出」域搬迁。

拆分最容易出的两类事故，各写成一条用例：

1. **新模块按名字绑定可变状态** —— 测试 `monkeypatch.setattr(_shared_state, ...)`
   重绑后，新模块手里还是旧 dict（批次 33 踩过：缓存里有数据、接口却 404）。
2. **路由搬走后反向依赖倒灌** —— 每个新域都 `from backend.routes.agent_routes import ...`，
   拆到第三个域时 `agent_routes` 就成了"所有域的公共依赖库"。

另外把「哪些端点在哪个模块里」也钉住：路径快照能证明路由没少，
但它证明不了"导出域真的搬走了"—— 端点还挂在 `agent_routes` 上也照样零差异。
"""
from pathlib import Path
from types import SimpleNamespace
import ast

import pytest
from fastapi.routing import APIRoute

from backend.main import app
from backend.routes import _shared_state
from backend.routes import _workflow_deps

ROUTES_DIR = Path(__file__).resolve().parent.parent / "backend" / "routes"
AGENT_MODULE = "backend.routes.agent_routes"


def _imports_agent_routes(path: Path) -> list[int]:
    """按 AST 查真实 import，而不是按字符串查（文档里提到模块名不算依赖）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == AGENT_MODULE:
            lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name == AGENT_MODULE for alias in node.names):
                lines.append(node.lineno)
    return lines


def _reverse_import_sites() -> list[str]:
    return [
        f"{path.name}:{lineno}"
        for path in sorted(ROUTES_DIR.glob("*.py"))
        for lineno in _imports_agent_routes(path)
    ]


def _endpoint(path: str, method: str):
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"路由不存在：{method} {path}")


def test_reverse_imports_are_gone():
    """判定标准：`rg "from backend.routes.agent_routes import" backend/routes/` ≤ 1 处。"""
    sites = _reverse_import_sites()
    assert len(sites) <= 1, f"反向依赖还剩 {len(sites)} 处：{sites}"


def test_workflow_deps_does_not_depend_on_agent_routes():
    """依赖模块本身不能反过来 import 大文件，否则抽口只是搬了个位置。"""
    assert _imports_agent_routes(ROUTES_DIR / "_workflow_deps.py") == []


@pytest.mark.parametrize("path,method", [
    ("/api/agent/workflow/export", "POST"),
    ("/api/agent/workflow/export-step", "POST"),
    ("/api/agent/download/{file_name}", "GET"),
])
def test_export_endpoints_are_served_by_export_routes(path, method):
    assert _endpoint(path, method).endpoint.__module__ == "backend.routes.export_routes"


def test_step_endpoints_stay_in_agent_routes():
    """搬迁只搬导出域 —— 相邻端点的归属不能被顺手改掉。"""
    assert _endpoint("/api/agent/workflow/{session_id}/status", "GET").endpoint.__module__ == (
        "backend.routes.agent_routes"
    )


def test_rebinding_shared_state_is_seen_by_the_reader(monkeypatch):
    """可变状态必须按模块访问：重绑 `_shared_state.active_workflows` 后读方要立刻看到。

    按名字导入时，读方绑定的是**导入那一刻的 dict**，重绑只改了模块属性，
    接口表现成"工作流明明在内存里，却报 404"。
    """
    class FakeWorkflow:
        def get_state(self, config):
            return SimpleNamespace(values={"step_results": {"1": {"content": "正文"}}})

    monkeypatch.setattr(_shared_state, "active_workflows", {"rebind": FakeWorkflow()})

    values = _workflow_deps._get_workflow_values("rebind")

    assert values["step_results"] == {1: {"content": "正文"}}


def test_persist_state_reads_shared_progress(monkeypatch):
    """`_persist_state` 的运行中判定也要走 `_shared_state`，不能是导入时的快照。"""
    from fastapi import HTTPException

    monkeypatch.setattr(_shared_state, "active_workflows", {})
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {"busy": {"status": "running"}})

    with pytest.raises(HTTPException) as exc:
        _workflow_deps._persist_state("busy", {"indicator_system": {"level1_indicators": []}})

    assert exc.value.status_code == 409
