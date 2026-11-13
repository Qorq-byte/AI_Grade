"""变异检验：批次 55——工作流步骤状态与实时输出。

守四件事：
1. 轮询规则退回「只认 completed/pending」—— 这就是用户报的那个 bug；
2. `mergeStatus` 不再保护 completed 终态（完成的行会闪回 running）；
3. `shouldAutoFollow` 恒为真（用户往上翻也被拽回底部）；
4. 页面不再加载 `workflow-steps.js`（`WorkflowSteps` 未定义，整页失效）。

JS 没有编译期检查，所以每个变异体先 `node --check`：语法错误的变异体会被
pytest/node 当成"抓到"，那是假绿。
锚点找不到时打印「跳过」并入失败清单，不静默。

**跑它要给足超时**（B54 踩过：脚本被 SIGTERM 打断会把变异体留在盘上，
下一轮把污染版当基线）。跑完请 `git status --short` + grep 关键行验伤。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
NODE = "node"
TESTS = [
    [NODE, "--test", "tests/test_workflow_steps.cjs"],
    [PY, "-m", "pytest", "tests/test_frontend_contract.py", "-q", "--no-header",
     "-p", "no:cacheprovider"],
]

STEPS = "frontend/shared/workflow-steps.js"
PAGE = "frontend/workflow/index.html"

MUTATIONS = [
    ("列表退回「只认 completed/pending」", PAGE,
     "            const status = stepStates[n] || 'pending';",
     "            const status = completedSteps.has(n) ? 'completed' : 'pending';"),
    ("completed 不再是终态", STEPS,
     "        if (current === 'completed') return 'completed';",
     "        if (false) return 'completed';"),
    ("失败步骤被画成正在运行", STEPS,
     "                if (sessionStatus === 'failed') out[n] = 'error';",
     "                if (false) out[n] = 'error';"),
    ("恒自动跟随（抢用户滚动）", STEPS,
     "        return gap <= limit;",
     "        return true;"),
    ("页面不加载步骤模块", PAGE,
     '    <script src="/static/shared/workflow-steps.js"></script>\n',
     ""),
]

originals = {path: Path(path).read_text(encoding="utf-8") for path in (STEPS, PAGE)}
failures = []


def syntax_ok(path: str, text: str) -> bool:
    if not path.endswith(".js"):
        return True   # HTML 没有便宜的语法检查；它的变异由契约用例兜
    probe = Path("_mutation_probe.js")
    probe.write_text(text, encoding="utf-8")
    try:
        return subprocess.run([NODE, "--check", str(probe)],
                              capture_output=True).returncode == 0
    finally:
        probe.unlink(missing_ok=True)


try:
    for name, path, old, new in MUTATIONS:
        base = originals[path]
        if old not in base:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到，锚点可能已随代码改动失效）")
            continue
        mutated = base.replace(old, new, 1)
        if not syntax_ok(path, mutated):
            failures.append(f"{name}: 变异体语法错误")
            print(f"跳过  {name}   （变异体语法错误，避免假绿）")
            continue
        Path(path).write_text(mutated, encoding="utf-8")
        try:
            caught = False
            for command in TESTS:
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode != 0:
                    caught = True
                    break
            print(f"{'抓住' if caught else '漏掉'}  {name}")
            if not caught:
                failures.append(name)
        finally:
            Path(path).write_text(base, encoding="utf-8")
finally:
    for path, text in originals.items():
        Path(path).write_text(text, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
