"""变异检验：步骤 10 的补全、开关持久化与按步骤路由（批次 30）。

批次 29 的 `_mutate_step9_critique.py` 守的是「补全机制」本身；
这一份守的是**扩到第二步时最容易串台的三件事**：
① 步骤 10 拿错了提示词或写错了字段；② 开关没落盘（重启静默失效）；
③ 接口忽略 `step` 参数（两步互相覆盖）。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TARGET = [PY, "-m", "pytest", "tests/test_critique_step10.py", "tests/test_step9_critique.py",
          "-q", "--no-header", "-p", "no:cacheprovider"]

AGENT = "backend/agents/performance_agent.py"
ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("步骤 10 用步骤 9 的提示词（串台）", AGENT,
     '''            "你在做一份绩效评价报告的定稿校对。任务：为遗漏的扣分指标补写**改进建议**，"''',
     '''            "你在做一份绩效评价报告的定稿校对。任务：把遗漏的扣分指标补进**已有的**"
            "问题分析里。要求：\\n"
            "1. 只补写这些指标对应的问题与原因，插在语义最合适的段落或汇总表里；\\n"
            "2. x"'''),
    ("步骤 10 的结果写回问题分析字段", AGENT,
     '''        content = self._apply_critique(state, 10, content)
        state["suggestions"] = content''',
     '''        content = self._apply_critique(state, 10, content)
        state["problems_analysis"] = content'''),
    ("步骤 10 的开关名指错（永远读步骤 9 的）", AGENT,
     '        "flag": "critique_step10",',
     '        "flag": "critique_step9",'),
    ("step_control 移出备份白名单（开关重启即失效）", AGENT,
     '                "step_control",',
     '                # "step_control",'),
    ("set-critique 忽略 step 参数", ROUTES,
     '    flag = f"critique_step{req.step}"',
     '    flag = "critique_step9"'),
    ("/critique/{step} 忽略 step（永远返回步骤 9）", ROUTES,
     '        return {"success": True, "data": _critique_view(_get_workflow_values(session_id), step)}',
     '        return {"success": True, "data": _critique_view(_get_workflow_values(session_id), 9)}'),
    ("状态接口不返回开关状态", ROUTES,
     '        "critique_switches": {',
     '        "_unused_switches": {'),
]

originals = {}
for entry in MUTATIONS:
    path = entry[1]
    if path not in originals:
        originals[path] = open(path, encoding="utf-8").read()

failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        open(path, "w", encoding="utf-8").write(originals[path].replace(old, new, 1))
        result = subprocess.run(TARGET, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "fail" in item]
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
