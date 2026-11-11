"""变异检验：批次 40（B6 第一步）——多模型候选的可比较与择优。

这批的风险是「看起来在比较，其实还是取第一个」：候选没留住、并列硬推一个、
引用不校验、接口不校验候选、status 不暴露对比 —— 页面照样能跑。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_candidates.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

AGENT = "backend/agents/performance_agent.py"
SERVICE = "backend/services/candidates.py"
ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("步骤结果不保留候选（raw_outputs 又变回 default）", AGENT,
     "        entry = candidates.entry_for(state, step_number)",
     "        entry = None"),
    ("备份白名单漏掉 comparison_history（重启候选全丢）", AGENT,
     '                "comparison_history",\n',
     ""),
    ("并列也给推荐（等于换个方式取第一个）", SERVICE,
     '    if len(scored) >= 2 and scored[0]["score"] <= scored[1]["score"]:',
     "    if False:"),
    ("引用不校验（任何编号都算可定位）", SERVICE,
     "        hit = sum(1 for ref in refs if ref in valid)",
     "        hit = len(refs)"),
    ("排序按模型名而不是分数", SERVICE,
     '    scored.sort(key=lambda item: (-item["score"], item["model"]))',
     '    scored.sort(key=lambda item: item["model"], reverse=True)'),
    ("人工选择不校验候选是否存在", ROUTES,
     "    if req.model not in outputs:",
     "    if False:"),
    ("status 不暴露对比摘要", ROUTES,
     "        if candidates.entry_for(values, step):",
     "        if False:"),
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
