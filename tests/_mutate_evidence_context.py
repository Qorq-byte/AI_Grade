"""变异检验：批次 50——证据块邻行扩展。

风险：扩展没接上 / 半径为 0 / 改了缓存对象 / 边界不裁剪。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_evidence_context.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/workflow_evidence.py"

MUTATIONS = [
    ("扩展没接上（返回原始单行）", SERVICE,
     "    return _expand_context(ranked, state)",
     "    return ranked"),
    ("扩展半径改为 0", SERVICE,
     "CONTEXT_RADIUS = 2",
     "CONTEXT_RADIUS = 0"),
    ("原地改 hit（污染缓存对象）", SERVICE,
     '        expanded.append({**hit, "text": "\\n".join(lines[low:high])})',
     '        hit["text"] = "\\n".join(lines[low:high]); expanded.append(hit)'),
    ("边界不裁剪", SERVICE,
     "        low = max(0, index - CONTEXT_RADIUS)",
     "        low = index - CONTEXT_RADIUS"),
]

originals = Path(SERVICE).read_text(encoding="utf-8")
failures = []
try:
    for name, _, old, new in MUTATIONS:
        if old not in originals:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(SERVICE).write_text(originals.replace(old, new, 1), encoding="utf-8")
        result = subprocess.run(TESTS, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        Path(SERVICE).write_text(originals, encoding="utf-8")
finally:
    Path(SERVICE).write_text(originals, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
