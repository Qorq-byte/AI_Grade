"""变异检验：批次 49——前端 DOM 契约检查本身是否有效。

做法是故意把 `index.html` / `app.js` 改坏三处，确认契约检查会红：
引用不存在的 id / 事件函数未定义 / 标记里重复 id。
脚本会原样还原文件。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_frontend_contract.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

HTML = "frontend/client/index.html"
JS = "frontend/client/app.js"

MUTATIONS = [
    ("引用了不存在的 id", JS,
     "document.getElementById('chatMessages')",
     "document.getElementById('chatMessagesX')"),
    ("事件函数未定义", JS,
     "function selectCandidate(sn,model){",
     "function selectCandidateX(sn,model){"),
    ("标记里出现重复 id", HTML,
     'id="wfFileZone"',
     'id="wfLayout"'),
]

# 脚本抽离后，一部分变异点落在 app.js 上 —— 按目标文件分别保存原文再还原
ORIGINALS = {path: Path(path).read_text(encoding="utf-8") for path in (HTML, JS)}
failures = []
try:
    for name, path, old, new in MUTATIONS:
        source = ORIGINALS[path]
        if old not in source:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(path).write_text(source.replace(old, new, 1), encoding="utf-8")
        try:
            result = subprocess.run(TESTS, capture_output=True, text=True)
            line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
            caught = result.returncode != 0
            print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
            if not caught:
                failures.append(name)
        finally:
            Path(path).write_text(source, encoding="utf-8")
finally:
    for path, text in ORIGINALS.items():
        Path(path).write_text(text, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
