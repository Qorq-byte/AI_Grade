"""变异检验：批次 46（B6 第三步）——全量差异 + 采用后再编辑。

风险是「三个候选看起来都对比了，其实基准错了 / 校验没了 / 标记没写」。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_candidates.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("忽略显式指定的基准", ROUTES,
     "    if base is not None:",
     "    if False:"),
    ("基准不存在也照算", ROUTES,
     "        if base not in outputs:",
     "        if False:"),
    ("单候选也返回全量差异", ROUTES,
     "    if len(outputs) < 2:",
     "    if False:"),
    ("默认基准不用推荐（改用最后一个）", ROUTES,
     '        base = next((item["model"] for item in ranked if item.get("recommended")),\n'
     '                    ranked[0]["model"] if ranked else None)',
     '        base = ranked[-1]["model"] if ranked else None'),
    ("采用后再编辑不写标记", ROUTES,
     '    if source != "compare_select" and entry and entry.get("selected_best"):',
     "    if False:"),
    ("重新采用不清除编辑标记", ROUTES,
     '    entry.pop("edited_after_select", None)   # 又回到模型原文，标记作废',
     "    pass"),
    ("status 不暴露编辑标记", ROUTES,
     '                "edited_after_select": entry.get("edited_after_select"),',
     '                "edited_after_select": None,'),
]

originals = {}
for entry in MUTATIONS:
    path = entry[1]
    if path not in originals:
        originals[path] = Path(path).read_text(encoding="utf-8")

failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        mutated = originals[path].replace(old, new, 1)
        try:
            compile(mutated, path, "exec")
        except SyntaxError as exc:
            failures.append(f"{name}: 变异体语法错误 {exc}")
            print(f"跳过  {name}   （变异体语法错误）")
            continue
        Path(path).write_text(mutated, encoding="utf-8")
        result = subprocess.run(TESTS, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        Path(path).write_text(originals[path], encoding="utf-8")
finally:
    for path, text in originals.items():
        Path(path).write_text(text, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
