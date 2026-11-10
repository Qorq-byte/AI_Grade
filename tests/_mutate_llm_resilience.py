"""变异检验：改坏「分级超时 + 降级链」，确认 tests/test_llm_resilience.py 会失败。

串行跑（会临时改写源文件），跑完必须 `git status --short` + `grep` 关键行复核。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TARGET = [PY, "-m", "pytest", "tests/test_llm_resilience.py", "-q", "--no-header",
          "-p", "no:cacheprovider"]

AGENT = "backend/agents/performance_agent.py"

MUTATIONS = [
    ("429 当成 permanent（永不降级）", AGENT,
     '        if status in TRANSIENT_HTTP_STATUS or status >= 500:\n            return "transient"',
     '        if False:\n            return "transient"'),
    ("认不出来的异常也当 transient（拿 bug 烧两遍钱）", AGENT,
     '    if any(hint in name for hint in ("Timeout", "Connection", "RateLimit", "Overloaded")):\n'
     '        return "transient"\n    return "permanent"',
     '    return "transient"'),
    ("备选链不去重（主模型会被试两次）", AGENT,
     "            if name and name not in chain:\n                chain.append(name)",
     "            if name:\n                chain.append(name)"),
    ("备选链不封顶（能一路降下去）", AGENT,
     "        return chain[:3]",
     "        return chain"),
    ("分级超时退化成恒 300", AGENT,
     "STEP_TIMEOUTS = {5: 300, 6: 300, 7: 300, 9: 300, 10: 300}\nDEFAULT_STEP_TIMEOUT = 120",
     "STEP_TIMEOUTS = {}\nDEFAULT_STEP_TIMEOUT = 300"),
    ("显式 timeout 被忽略", AGENT,
     "        timeout = timeout or step_timeout(step_number)",
     "        timeout = step_timeout(step_number)"),
    ("permanent 错误也换模型（白烧一次）", AGENT,
     '                if kind == "permanent" or index == len(chain) - 1:\n                    raise',
     '                if index == len(chain) - 1:\n                    raise'),
    ("降级成功不写警告（读者不知道换了模型）", AGENT,
     "                    warnings = state.setdefault(\"warnings\", [])\n"
     "                    if hint not in warnings:\n                        warnings.append(hint)",
     "                    pass"),
    ("成功的那次不记账", AGENT,
     '                self._note_llm_attempt(state, attempts, step_number, model, index + 1,\n'
     '                                       started, "ok")',
     '                pass'),
    ("台账不封顶", AGENT,
     "        if len(attempts) > 200:\n            del attempts[:-200]",
     "        if False:\n            del attempts[:-200]"),
    ("llm_attempts 不落盘", AGENT,
     '                "llm_attempts",',
     '                # "llm_attempts",'),
]

original = open(AGENT, encoding="utf-8").read()
failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in original:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        open(path, "w", encoding="utf-8").write(original.replace(old, new, 1))
        result = subprocess.run(TARGET, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "fail" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        open(path, "w", encoding="utf-8").write(original)
finally:
    open(AGENT, "w", encoding="utf-8").write(original)

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
