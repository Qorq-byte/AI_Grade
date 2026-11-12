"""变异检验：批次 43（B6 第二步）——候选差异与选择审计。

风险是「差异看起来有了，其实没对齐」：整段替换都算"改过"、行数不封顶、
不校验候选、审计没写时间 —— 界面照样能用。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_candidates.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/candidates.py"
ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("整段替换也算改过（差异变噪音）", SERVICE,
     "                    if ratio >= SIMILARITY_THRESHOLD:",
     "                    if True:"),
    ("差异行数不封顶", SERVICE,
     "    truncated = len(rows) > max_rows\n    return {\"rows\": rows[:max_rows],",
     "    truncated = False\n    return {\"rows\": rows,"),
    ("无空行时不回退按行切", SERVICE,
     "    if len(parts) <= 1:",
     "    if False:"),
    ("差异端点不校验未知候选", ROUTES,
     "        if model not in outputs:",
     "        if False:"),
    ("同一模型也返回差异", ROUTES,
     "    if left == right:",
     "    if False:"),
    ("采用不写审计时间", ROUTES,
     '    entry["selected_at"] = datetime.now(timezone.utc).isoformat()',
     '    entry["selected_at"] = ""'),
    ("status 不回审计字段", ROUTES,
     '                "selected_by": entry.get("selected_by"),',
     '                "selected_by": None,'),
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
