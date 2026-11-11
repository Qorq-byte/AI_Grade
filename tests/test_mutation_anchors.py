"""路由层变异锚点守卫（批次 35）。

变异脚本靠「源码里的那段字符串」定位。代码一搬家，锚点就失效，而脚本的处理是
打印「变异点未找到」然后**跳过** —— 它还在跑、还在输出「抓住 N 条」，
但守的那条逻辑早就没人管了。

这不是假设：
- 批次 33 把问卷列表搬进 `questionnaire_routes.py`，`_mutate_backup_race.py`
  的第 4 条锚点留在了 `agent_routes.py`；
- 批次 34 把导出域整体搬进 `export_routes.py`，`_mutate_b7_enablement.py` 三条、
  `_mutate_consistency_gate.py` 三条锚点跟着失效。

两次都没人发现，因为「跑变异脚本」是人工步骤。所以把「锚点还在不在」变成测试。

**范围**：只检查目标文件在 `backend/routes/` 下的变异体 —— 拆分的风险就在这里。
锚在 `performance_agent.py` / `services/` 的脚本不受搬家影响，且有几个用别名
（`"agent"`、隐式单目标）指向文件，静态解析不可靠；对它们做「猜路径」比不做更糟。
这个边界是刻意的，不是漏掉。
"""
import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
ROUTES_PREFIX = "backend/routes/"


def _module_strings(tree: ast.Module) -> dict:
    """模块级字符串常量（`AGENT = "backend/..."` 这种）。"""
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            consts[target.id] = value
    return consts


def _resolve(node: ast.AST, consts: dict) -> str | None:
    """能静态求值就返回字符串，否则 None（交给调用方跳过）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _resolve(node.left, consts), _resolve(node.right, consts)
        return None if left is None or right is None else left + right
    return None


def _mutations(script: Path) -> list[tuple]:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    consts = _module_strings(tree)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not (isinstance(node.targets[0], ast.Name) and node.targets[0].id == "MUTATIONS"):
            continue
        assert isinstance(node.value, (ast.List, ast.Tuple)), f"{script.name}: MUTATIONS 不是字面量列表"
        rows = []
        for element in node.value.elts:
            values = [_resolve(item, consts) for item in element.elts]
            rows.append(tuple(values))
        return rows
    raise AssertionError(f"{script.name}: 没找到 MUTATIONS")


def _route_mutations(script: Path) -> list[tuple[str, str, str]]:
    """只挑出目标在 backend/routes/ 下的变异体： (说明, 路径, 原文)。"""
    picked = []
    for entry in _mutations(script):
        if len(entry) < 4:
            continue  # 三元组：路径是脚本里的隐式单目标，无法静态判定，跳过
        name, path, old = entry[0], entry[1], entry[2]
        if isinstance(path, str) and path.startswith(ROUTES_PREFIX) and isinstance(old, str):
            picked.append((name, path, old))
    return picked


SCRIPTS = sorted(TESTS_DIR.glob("_mutate_*.py"))


def test_at_least_one_route_anchor_is_guarded():
    """守卫自己要有覆盖对象 —— 否则「零脚本、零失败」也报绿。"""
    covered = [script.name for script in SCRIPTS if _route_mutations(script)]
    assert len(covered) >= 3, covered


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_route_mutation_anchors_still_exist(script):
    missing = []
    for name, path, old in _route_mutations(script):
        target = REPO_ROOT / path
        if not target.exists():
            missing.append(f"{name}: 目标文件不存在 {path}")
        elif old not in target.read_text(encoding="utf-8"):
            missing.append(f"{name}: 锚点在 {path} 里找不到（代码可能已搬走）")

    assert not missing, f"{script.name} 有 {len(missing)} 条变异已成空包弹：\n" + "\n".join(missing)
