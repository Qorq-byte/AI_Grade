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


def _dep_names(deps) -> list:
    """依赖比对只看函数名，不看定义模块。

    拆分 `agent_routes.py` 会把 `_require_*` 这类依赖搬到 `_workflow_deps`，
    它们的 `__module__` 随之改变 —— 但**接口契约没变**（还是同一个函数、同样的副作用）。
    把模块名算进契约，每次搬依赖都会假红，快照就会被人为无视，那才是真的失守。
    丢依赖、换成另一个函数，仍然按名字抓得住。
    """
    return sorted({str(item).rsplit(".", 1)[-1] for item in deps})


def diff(before: list[dict], after: list[dict]) -> list[str]:
    issues = []
    before_map = {_key(item): item for item in before}
    after_map = {_key(item): item for item in after}
    for key in before_map.keys() - after_map.keys():
        issues.append(f"少了路由：{key[1]} {key[0]}")
    for key in after_map.keys() - before_map.keys():
        issues.append(f"多了路由：{key[1]} {key[0]}")
    for key in before_map.keys() & after_map.keys():
        old, new = before_map[key], after_map[key]
        for field in ("name", "body", "response_model", "status_code"):
            if old[field] != new[field]:
                issues.append(f"{key[1]} {key[0]} 的 {field} 变了：{old[field]!r} → {new[field]!r}")
        if _dep_names(old["deps"]) != _dep_names(new["deps"]):
            issues.append(f"{key[1]} {key[0]} 的 deps 变了：{old['deps']!r} → {new['deps']!r}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="把当前路由快照写到这个文件")
    parser.add_argument("--compare", help="与这个快照文件比对")
    args = parser.parse_args()

    rows = snapshot()
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {path}（{len(rows)} 条路由）")
        return 0

    if args.compare:
        before = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        issues = diff(before, rows)
        if issues:
            print(f"FAIL 路由快照有 {len(issues)} 处差异：")
            for line in issues:
                print("  -", line)
            return 1
        print(f"PASS 路由快照零差异（{len(rows)} 条）")
        return 0

    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
