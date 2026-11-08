"""变异检验：故意改坏一致性检查，确认 tests/test_consistency.py 真的会失败。"""
import subprocess
import sys

PY = ".venv/Scripts/python"

# (说明, 文件, 原文, 替换)
MUTATIONS = [
    ("同一矛盾报两次（去掉去重）", "backend/services/consistency.py", """            token = (kind, value)
            if token in seen:
                continue
            seen.add(token)
""", """            seen.add((kind, value))
"""),
    ("维度4 当成错误", "backend/services/consistency.py",
     '"INDICATOR_ALLOCATION_D{dimension}", "warning" if policy else "error",',
     '"INDICATOR_ALLOCATION_D{dimension}", "error",'),
    ("满分合计误用实际得分", "backend/services/consistency.py",
     "    full_total = _leaf_full_total(state)",
     "    full_total = _leaf_actual_total(state)"),
    ("删掉「待核实」检查", "backend/services/consistency.py",
     "    if summary and not complete and _PENDING_WORD not in text:",
     "    if False:"),
    ("等级正则改用字符类（「不合格」会被截成「合格」）", "backend/services/consistency.py",
     '(?:评价等级|综合等级|评定等级)[：:\\s]*(不合格|待核实|优秀|良好|合格)',
     '(?:评价等级|综合等级|评定等级)[：:\\s]*([不待优良好合]+)'),
    ("warnings 不去重", "backend/agents/performance_agent.py",
     """            if line not in warnings:
                warnings.append(line)""", "            warnings.append(line)"),
    ("Step14 不写 warnings", "backend/agents/performance_agent.py",
     "        if line:\n", "        if False:\n"),
]

files = {path: open(path, encoding="utf-8").read() for _, path, _, _ in MUTATIONS}
originals = dict(files)
failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        open(path, "w", encoding="utf-8").write(originals[path].replace(old, new, 1))
        result = subprocess.run(
            [PY, "-m", "pytest", "tests/test_consistency.py", "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        open(path, "w", encoding="utf-8").write(originals[path])
finally:
    for path, text in originals.items():
        open(path, "w", encoding="utf-8").write(text)

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
