"""变异检验：批次 53——句子级清理。

风险：不清理 / 不切句（跨句误删正向引用）/ 无差别删所有编号。
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
     "        if clause and ABSENCE.search(clause):",
     "        if False:"),
    ("不切句（跨句误删）", SERVICE,
     'SENTENCE_SPLIT = re.compile(r"([。；！？\\n])")',
     'SENTENCE_SPLIT = re.compile(r"(§)")'),
    ("无差别删所有编号", SERVICE,
     "        if clause and ABSENCE.search(clause):",
     "        if clause:"),
]

originals = Path(SERVICE).read_text(encoding="utf-8")
failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in originals:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(path).write_text(originals.replace(old, new, 1), encoding="utf-8")
        result = subprocess.run(TESTS, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        Path(path).write_text(originals, encoding="utf-8")
finally:
    Path(SERVICE).write_text(originals, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
