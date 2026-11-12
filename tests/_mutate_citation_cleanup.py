"""变异检验：批次 54——引用清理台账 + 扩词表。

守四件事：
1. 清完不记账（正文改了，却查不到「原本引了什么」）；
2. 词表回退成旧五词（暂无/未能获取/空白 不覆盖）；
3. 台账不进备份白名单（重启后只剩正文被动过，没有原因）；
4. 台账只留最后一条（LangGraph 节点副本式的覆盖，B31 踩过）。

外加一条步骤 14 的分支：报告侧的清理发生在 `_record_step_result` 之前，
它自己那一笔要是没了，台账里就只有 8–13 步，而真正被改过的是报告。

每个变异体先离线 `compile()`：语法错误的变异体会被 pytest 当成"抓到"，
那是假绿（坑表里写过）。锚点找不到时打印「跳过」并入失败清单，不静默。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_citation_policy.py",
         "tests/test_citation_cleanup_ledger.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

AGENT = "backend/agents/performance_agent.py"
SERVICE = "backend/services/citation_policy.py"

MUTATIONS = [
    ("步骤落库不记账", AGENT,
     "            self._note_citation_cleanup(state, step_number, removals)",
     "            pass"),
    ("步骤 14 不记账", AGENT,
     "            self._note_citation_cleanup(state, 14, removals)",
     "            pass"),
    ("词表漏词（回退成旧五词）", SERVICE,
     'ABSENCE = re.compile(r"(?:缺|未提供|未取得|未见|未附|暂无|未能获取|空白)")',
     'ABSENCE = re.compile(r"(?:缺|未提供|未取得|未见|未附)")'),
    ("台账不进备份白名单", AGENT,
     '                "citation_cleanup",',
     '                "citation_cleanup_typo",'),
    ("台账只留最后一条", AGENT,
     '        self._citation_cleanup = entries\n'
     '        state["citation_cleanup"] = list(entries)',
     '        self._citation_cleanup = entries[-1:]\n'
     '        state["citation_cleanup"] = list(entries[-1:])'),
]

originals = {path: Path(path).read_text(encoding="utf-8") for path in (AGENT, SERVICE)}
failures = []
try:
    for name, path, old, new in MUTATIONS:
        base = originals[path]
        if old not in base:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到，锚点可能已随代码改动失效）")
            continue
        mutated = base.replace(old, new, 1)
        try:
            compile(mutated, path, "exec")
        except SyntaxError as exc:
            failures.append(f"{name}: 变异体语法错误 {exc}")
            print(f"跳过  {name}   （变异体语法错误，避免假绿）")
            continue
        Path(path).write_text(mutated, encoding="utf-8")
        try:
            result = subprocess.run(TESTS, capture_output=True, text=True)
            line = [item for item in result.stdout.splitlines()
                    if "passed" in item or "failed" in item or "error" in item]
            caught = result.returncode != 0
            print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
            if not caught:
                failures.append(name)
        finally:
            Path(path).write_text(base, encoding="utf-8")
finally:
    for path, text in originals.items():
        Path(path).write_text(text, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
