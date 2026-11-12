"""模块卫生：同一个模块里不许有重名的顶层定义。

为什么单独一条：批次 43 用脚本往 `candidates.py` 追加函数时，脚本被执行了两遍，
`paragraphs` / `diff_paragraphs` 各出现两次 —— **后者静默覆盖前者**。
功能看起来正常，但变异打在第一份上完全无效，三条「应该失败」的变异全被漏掉，
差点得出「用例很弱」的错误结论。

这类问题静态一眼可见，没必要等变异才发现。
"""
import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def _modules():
    return sorted(BACKEND.glob("services/*.py")) + sorted(BACKEND.glob("routes/*.py"))


def _duplicates(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    seen, repeated = set(), []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen:
                repeated.append(node.name)
            seen.add(node.name)
    return repeated


@pytest.mark.parametrize("path", _modules(), ids=lambda item: item.name)
def test_no_duplicate_top_level_definitions(path):
    assert _duplicates(path) == [], f"{path.name} 里有重复定义：{_duplicates(path)}"
