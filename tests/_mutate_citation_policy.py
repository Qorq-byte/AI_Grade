"""变异检验：批次 52——缺失类结论的编号清理。

风险：不清理 / 只清一遍（多编号漏网）/ 跨句误删正向引用。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_citation_policy.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/citation_policy.py"

MUTATIONS = [
    ("完全不清理", SERVICE,
     "        return ABSENCE_WITH_ID.sub(_replace, source), removed",
     "        return source, 0"),
    ("只清一遍（多个编号漏网）", SERVICE,
     "    for _ in range(10):",
     "    for _ in range(1):"),
    ("跨句也删（会误伤正向引用）", SERVICE,
     '    r"[^。\\n]{0,60}(?:缺|未提供|未取得|未见|未附)[^。\\n]{0,60}?\\[(P\\d+-\\d+-\\d+)\\]"',
     '    r"(?:缺|未提供|未取得|未见|未附).{0,200}?\\[(P\\d+-\\d+-\\d+)\\]"'),
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
