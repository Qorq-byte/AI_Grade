"""批次 35 的验收：搬「报告」域 + 把「导出样式」并进导出域。

这一批要证明三件事：

1. **端点换了模块，但接口没变** —— `_route_snapshot` 证路径/依赖/模型不变，
   这里证「归属真的换了」（快照对"还挂在 agent_routes 上"也是零差异）。
2. **反向依赖彻底断掉** —— `export_routes` 原先按模块调用 `agent_routes` 的报告读取，
   现在指向 `report_routes`；`report_routes` 自己不回头 import `agent_routes`。
3. **跨域常量只有一份** —— `STEP_CONTENT_FIELDS` 同时被步骤编辑（还在 agent_routes）
   和报告组装（已搬走）使用。两边各抄一份就会「改得了、拼不进去」。
"""
import ast
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend.main import app
from backend.routes import _shared_state, _workflow_deps, agent_routes, report_routes

ROUTES_DIR = Path(__file__).resolve().parent.parent / "backend" / "routes"
AGENT_MODULE = "backend.routes.agent_routes"


def _imports(path: Path, module: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(alias.name == module for alias in node.names):
            lines.append(node.lineno)
    return lines


def _endpoint(path: str, method: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"路由不存在：{method} {path}")


def test_no_module_imports_agent_routes_any_more():
    """`rg "from backend.routes.agent_routes import" backend/routes/` 应为 0 处。"""
    sites = [
        f"{path.name}:{lineno}"
        for path in sorted(ROUTES_DIR.glob("*.py"))
        for lineno in _imports(path, AGENT_MODULE)
    ]
    assert sites == [], sites


@pytest.mark.parametrize("module", ["report_routes", "export_routes"])
def test_domain_modules_do_not_import_agent_routes_module(module):
    assert _imports(ROUTES_DIR / f"{module}.py", AGENT_MODULE) == []


@pytest.mark.parametrize("path,method", [
    ("/api/agent/workflow/{session_id}/report", "GET"),
    ("/api/agent/workflow/save-report", "POST"),
    ("/api/agent/workflow/{session_id}/report-order", "GET"),
    ("/api/agent/workflow/report-order", "POST"),
    ("/api/agent/workflow/assemble-report", "POST"),
    ("/api/agent/workflow/{session_id}/format-check", "POST"),
    ("/api/agent/workflow/{session_id}/modules", "GET"),
    ("/api/agent/workflow/generate-module", "POST"),
    ("/api/agent/workflow/add-custom-module", "POST"),
    ("/api/agent/workflow/delete-module", "POST"),
    ("/api/agent/workflow/update-module-content", "POST"),
])
def test_report_endpoints_are_served_by_report_routes(path, method):
    assert _endpoint(path, method).endpoint.__module__ == "backend.routes.report_routes"


@pytest.mark.parametrize("path,method", [
    ("/api/agent/workflow/export-style", "POST"),
    ("/api/agent/workflow/{session_id}/export-style", "GET"),
])
def test_export_style_lives_with_the_export_domain(path, method):
    assert _endpoint(path, method).endpoint.__module__ == "backend.routes.export_routes"


@pytest.mark.parametrize("name", [
    "_read_report", "_save_report", "_report_review_steps",
    "_assemble_report_from_steps", "_assemble_report_from_backup_steps",
])
def test_report_helpers_are_gone_from_agent_routes(name):
    """搬走就是搬走：留个同名转发会让人以为它还在原文件里维护。"""
    assert not hasattr(agent_routes, name)


def test_openapi_schema_builds_for_every_route():
    """搬请求模型时最容易漏的是「注解里的类型没导入」。

    这种错静态看不出来、路径快照也看不出来（body 模型名没变），
    直到 Pydantic 在**请求时**重建模型才炸成 500。批次 35 就漏了一个 `List`。
    `app.openapi()` 会把每个模型的注解求值一遍，能当场抓住。
    """
    schema = app.openapi()

    assert len(schema["paths"]) >= 100
    assert "ReportOrderRequest" in schema["components"]["schemas"]


def test_step_content_fields_is_one_object_shared_by_two_modules():
    """步骤编辑与报告组装必须看同一份映射，否则新增步骤会出现「改得了、拼不进去」。"""
    assert agent_routes.STEP_CONTENT_FIELDS is _workflow_deps.STEP_CONTENT_FIELDS
    assert report_routes.STEP_CONTENT_FIELDS is _workflow_deps.STEP_CONTENT_FIELDS


def test_save_report_refuses_while_running(monkeypatch):
    """`_save_report` 的运行中闸门走 `_shared_state`，不是导入时的快照。"""
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {"busy": {"status": "running"}})

    with pytest.raises(HTTPException) as exc:
        report_routes._save_report("busy", "# 正文", db=None)

    assert exc.value.status_code == 409
