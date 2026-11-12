"""变异检验：批次 48（B5 第七步）——同义词查询扩展。

风险是「扩展看起来加了，其实没生效 / 把原查询弄丢 / 只加了一个词」。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_chat_context.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/chat_context.py"

MUTATIONS = [
    ("不做同义词扩展", SERVICE,
     "        if any(term in query for term in group):",
     "        if False:"),
    ("扩展时把原查询丢掉", SERVICE,
     "    return f\"{query} {' '.join(dict.fromkeys(extra))}\"",
     "    return ' '.join(dict.fromkeys(extra))"),
    ("只补同组第一个词（反向命中不了）", SERVICE,
     "            extra.extend(term for term in group if term not in query)",
     "            extra.append(group[0])"),
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
