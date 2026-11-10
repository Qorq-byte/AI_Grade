"""路由快照：拆分 `agent_routes.py` 的验收工具（批次 33）。

讲得再好听也不如一行 diff：**拆之前把全部路由导出来，拆完再导一次，逐项比对**。
快照包含「方法 + 路径 + 端点名 + 依赖名 + 请求模型名」，
少了任何一条、或某条依赖被悄悄去掉，比对都会红。

用法：
    python -m tests._route_snapshot --out .backup/routes_before.json     # 取样
    python -m tests._route_snapshot --compare .backup/routes_before.json # 比对

只导出 FastAPI 的 APIRoute（跳过 /openapi.json 这类内置路由）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.main import app
from fastapi.routing import APIRoute


def _dep_name(dependency) -> str:
    """依赖/函数的可读名字（函数或带 `dependency` 属性的可调用对象）。"""
    target = getattr(dependency, "dependency", dependency) or dependency
    call = getattr(dependency, "call", None)
    if call is not None:
        target = call
    name = getattr(target, "__name__", None) or type(target).__name__
    module = getattr(target, "__module__", "")
    return f"{module}.{name}" if module else name


def snapshot() -> list[dict]:
    rows = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        body_model = getattr(route, "body_field", None)
        rows.append({
            "path": route.path,
            "methods": sorted(route.methods or []),
            "name": route.name,
            "deps": sorted({_dep_name(dep) for dep in (route.dependencies or [])}),
            "body": getattr(body_model, "name", "") if body_model is not None else "",
            "response_model": getattr(route.response_model, "__name__", "") or "",
            "status_code": getattr(route, "status_code", None),
        })
    rows.sort(key=lambda item: (item["path"], item["methods"]))
    return rows


def _key(item: dict) -> tuple:
    return item["path"], tuple(item["methods"])
