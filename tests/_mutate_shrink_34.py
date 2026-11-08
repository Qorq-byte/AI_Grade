"""变异检验（批次 17）：故意改坏步骤 3/4 的缩水判据，确认新用例真的会失败。

每个变异体先离线 compile()，再只跑**能观察它**的用例文件。
新文件覆盖形状适配与缩水判定；回显上限的用例在 `test_structured_output.py`。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
NEW = "tests/test_structured_output_steps34.py"
OLD = "tests/test_structured_output.py"
SO = "backend/agents/tools/structured_output.py"

# (说明, 原文, 替换, 用例文件)
MUTATIONS = [
    ("步骤 3 不走树形口径（退回 scores 分支，恒为 0 条）", """
    if step == 3 or (step is None and isinstance(payload.get(_TREE_KEY), list)):
        return _tree_coverage(payload) or _EMPTY_COVERAGE
""", "\n", NEW),
    ("叶子按 level==3 计数（模型标错层级就漏）", """        children = _as_children(node)
        if children:
            yield from _iter_leaves(children)
        else:
            yield node""", """        children = _as_children(node)
        if children:
            yield from _iter_leaves(children)
        elif node.get("level") == 3:
            yield node""", NEW),
    ("步骤 3 的 payload 顶层混着 total_score 时退回扁平分值（3c6070b9 的假条目）", """    if step == 3 or (step is None and isinstance(payload.get(_TREE_KEY), list)):
        return _tree_coverage(payload) or _EMPTY_COVERAGE
""", """    if step == 3 and _coerce_number(payload.get("total_score")) is None:
        return _tree_coverage(payload) or _EMPTY_COVERAGE
    if step == 3:
        return Coverage(len(payload), 0, "json", "scores")
""", NEW),
    ("缩水判定退回「只对步骤 7」", "    if step not in SHRINK_CHECKED_STEPS:",
     "    if step != 7:", NEW),
    ("不比可计分数值（分值被清零也放行）",
     "    if before.numeric and after.numeric < before.numeric:", "    if False:", NEW),
    ("不支持的步骤静默放行（不说「不做校验」）",
     '            f"步骤 {step} 不产出条目型结构，不做缩水校验", _EMPTY_COVERAGE, after,',
     '            "", _EMPTY_COVERAGE, after,', NEW),
    ("步骤 4 只认 actual_score（数组形态的 score 被当成无数值）",
     '    aliases = ("actual_score", "score") if step == 4 else ("actual_score",)',
     '    aliases = ("actual_score",)', NEW),
    ("步骤 3 回显上限退回 6000（修复只能看半截）",
     "MAX_ECHO_CHARS_BY_STEP = {3: 24000, 4: 12000, 7: 24000}",
     "MAX_ECHO_CHARS_BY_STEP = {3: 6000, 4: 12000, 7: 24000}", f"{NEW} {OLD}"),
]

originals = Path(SO).read_text(encoding="utf-8")
failures = []


def compile_ok():
    try:
        compile(Path(SO).read_text(encoding="utf-8"), SO, "exec")
        return True
    except SyntaxError as exc:
        print(f"  变异体语法错误，跳过：{exc}")
        return False


try:
    for name, old, new, tests in MUTATIONS:
        if old not in originals:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(SO).write_text(originals.replace(old, new, 1), encoding="utf-8")
        if not compile_ok():
            Path(SO).write_text(originals, encoding="utf-8")
            continue
        result = subprocess.run(
            [PY, "-m", "pytest", *tests.split(), "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        Path(SO).write_text(originals, encoding="utf-8")
finally:
    Path(SO).write_text(originals, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
