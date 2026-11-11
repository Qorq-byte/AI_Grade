"""变异检验：批次 37（B4 第二步）——refine 的重试 / 降级 / 台账。

这批的风险是「降级链看起来接上了，其实没生效」：不重试、不换模型、
永久错误也换模型、失败映射成 500、预览偷偷落账 —— 界面都正常。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_refine_llm.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/llm_resilience.py"
ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("同一模型不重试（429 一次就换）", SERVICE,
     "        for retry_index in range(1 + retries):",
     "        for retry_index in range(1):"),
    ("永久错误也去降级（400 白烧两次）", SERVICE,
     '                if kind == "permanent":',
     "                if False:"),
    ("降级链只用主模型", SERVICE,
     "    for index, model in enumerate(chain):",
     "    for index, model in enumerate(chain[:1]):"),
    ("降级成功不写 warning（读者不知道换了模型）", SERVICE,
     "                if index and warnings is not None:",
     "                if False:"),
    ("链路不封顶（可以无限降级）", SERVICE,
     "    return chain[:limit]",
     "    return chain"),
    ("成功的尝试也记成 error", SERVICE,
     '"outcome": "ok", "detail": "",',
     '"outcome": "error", "detail": "",'),
    ("预览不返回尝试与降级标记", ROUTES,
     '            "attempts": attempts, "degraded": degraded,',
     '            "attempts": [], "degraded": False,'),
    ("失败映射成 500（前端分不清该不该重试）", ROUTES,
     '        raise HTTPException(status_code=502 if exc.kind == "permanent" else 503,',
     '        raise HTTPException(status_code=500,'),
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
