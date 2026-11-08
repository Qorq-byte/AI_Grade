"""变异检验（批次 16）：故意改坏步骤 7 的处置口径，确认新用例真的会失败。

只跑 tests/test_step7_prompt_discipline.py —— 这一批改动只影响步骤 7 的提示词与台账。
每个变异体先离线 compile()，避免「语法错误被当成抓到」这个老坑。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = "tests/test_step7_prompt_discipline.py"

# (说明, 文件, 原文, 替换)
MUTATIONS = [
    ("按未取得佐证计分不再打标（和实测得分混成一个口径）",
     "backend/agents/performance_agent.py",
     '"no_evidence": reason_text.startswith(NO_EVIDENCE_MARKER),',
     '"no_evidence": False,'),
    ("汇总行不再披露未取得佐证的条数",
     "backend/agents/performance_agent.py",
     "    if no_evidence_scored:\n        line +=",
     "    if False:\n        line +="),
    ("步骤 7 的收尾语退回通用口径（又允许直接写待核实）",
     "backend/agents/performance_agent.py",
     '"本步骤的「不能核实」按评分规则第 3、4 条处置：未取得佐证按标准计分，"',
     '"不能核实的内容写「待核实」或「资料不足」。"'),
    ("允许把计划值当实际值（为落地而编造）",
     "backend/agents/prompts/default_prompts.py",
     "不得把计划值或目标值当作实际值写进评分理由**。",
     "可把计划值近似作为实际值写进评分理由**。"),
    ("删掉「严禁编造」在步骤 7 的自我声明",
     "backend/agents/prompts/default_prompts.py",
     "7. 【严禁编造 — 与前文同等优先级】",
     "7. 【写作要求】"),
    ("把「未取得佐证」处置规则整条删掉",
     "backend/agents/prompts/default_prompts.py",
     "3. **未取得佐证时的处置**：",
     "3. **资料不足时的处置**："),
]

paths = sorted({path for _, path, _, _ in MUTATIONS})
originals = {path: Path(path).read_text(encoding="utf-8") for path in paths}
failures = []


def compile_ok(path):
    try:
        compile(Path(path).read_text(encoding="utf-8"), path, "exec")
        return True
    except SyntaxError as exc:
        print(f"  变异体语法错误，跳过：{exc}")
        return False


try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(path).write_text(originals[path].replace(old, new, 1), encoding="utf-8")
        if not compile_ok(path):
            Path(path).write_text(originals[path], encoding="utf-8")
            continue
        result = subprocess.run(
            [PY, "-m", "pytest", TARGET_TESTS, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True)
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
