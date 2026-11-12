"""批次 41 的验收：搬「指标与评分」域到 `scoring_routes.py`。

这一批要证明三件事：

1. **端点换了模块，接口没变** —— `_route_snapshot` 证路径/依赖/模型不变（153 条零差异），
   这里证「归属真的换了」；
2. **不回头 import** —— `scoring_routes` 不依赖 `agent_routes`，`backend/routes/` 下
   没有任何反向导入；
3. **跨域规则只有一份** —— `_review_after` 同时被步骤编辑（还在 agent_routes）与
   评分保存（已搬走）使用，两边必须指向同一个函数对象；
4. **拆分目标达成** —— `agent_routes` ≤2000 行（判定标准）。
"""
import ast
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from backend.main import app
from backend.routes import _workflow_deps, agent_routes, scoring_routes

ROUTES_DIR = Path(__file__).resolve().parent.parent / "backend" / "routes"
AGENT_MODULE = "backend.routes.agent_routes"


def _imports(path: Path, module: str) -> list:
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


def test_no_module_imports_agent_routes():
    sites = [
        f"{path.name}:{lineno}"
        for path in sorted(ROUTES_DIR.glob("*.py"))
        for lineno in _imports(path, AGENT_MODULE)
    ]
    assert sites == [], sites


def test_scoring_routes_does_not_import_agent_routes():
    assert _imports(ROUTES_DIR / "scoring_routes.py", AGENT_MODULE) == []


@pytest.mark.parametrize("path,method", [
    ("/api/agent/workflow/{session_id}/indicators-for-scoring", "GET"),
    ("/api/agent/workflow/generate-indicator-analysis", "POST"),
    ("/api/agent/workflow/save-indicator-scores", "POST"),
    ("/api/agent/workflow/{session_id}/extract-deductions", "GET"),
    ("/api/agent/workflow/confirm-deductions", "POST"),
    ("/api/agent/workflow/{session_id}/verify-logic", "POST"),
    ("/api/agent/workflow/{session_id}/score-summary", "GET"),
    ("/api/agent/workflow/reorder-indicators", "POST"),
])
def test_scoring_endpoints_are_served_by_scoring_routes(path, method):
    assert _endpoint(path, method).endpoint.__module__ == "backend.routes.scoring_routes"


def test_review_after_is_one_object_shared_by_two_modules():
    """步骤编辑与评分保存必须看同一份依赖表，否则"改完指标谁该复核"会给出不同答案。"""
    assert agent_routes._review_after is _workflow_deps._review_after
    assert scoring_routes._review_after is _workflow_deps._review_after


def test_agent_routes_met_the_2000_line_target():
    lines = len(Path(ROUTES_DIR / "agent_routes.py").read_text(encoding="utf-8").splitlines())

    assert lines <= 2000, f"agent_routes 还有 {lines} 行"
