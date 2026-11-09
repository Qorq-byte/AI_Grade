"""变异检验（批次 21）：故意改坏步骤 5 / 6 的瘦身与覆盖度，确认用例真的会失败。

只跑本批相关的两个用例文件——预算表被批次 20 的用例一起守着，所以两个都跑。
每个变异体先离线 compile()，跑完还原并复核工作树。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = "tests/test_step56_output_budget.py tests/test_step7_output_length.py"

MUTATIONS = [
    ("步骤 6 又把 JSON 副本要回来",
     "backend/agents/prompts/default_prompts.py",
     "**不要输出 JSON 副本。**",
     "同时附带结构化的JSON数据以便后续导出 Word/PDF。"),
    ("步骤 5 每指标行数上限放宽十倍",
     "backend/agents/prompts/default_prompts.py",
     "【长度纪律 — 每个指标不超过 6 行】",
     "【长度纪律 — 每个指标不超过 60 行】"),
    ("步骤 5 允许写「使用说明」这类尾巴",
     "backend/agents/prompts/default_prompts.py",
     "3. 不要写「使用说明」「总体说明」「注意事项」这类与打分无关的尾巴；",
     "3. 可以在末尾附一段使用说明；"),
    ("预算表退回只管步骤 7",
     "backend/agents/performance_agent.py",
     "STEP_OUTPUT_BUDGETS = {5: 8000, 6: 6000, 7: 6000}",
     "STEP_OUTPUT_BUDGETS = {7: 6000}"),
    ("步骤 6 的预算压到 4000（低于实测输出）",
     "backend/agents/performance_agent.py",
     "STEP_OUTPUT_BUDGETS = {5: 8000, 6: 6000, 7: 6000}",
     "STEP_OUTPUT_BUDGETS = {5: 8000, 6: 4000, 7: 6000}"),
    ("步骤 5 覆盖度恒报全绿",
     "backend/agents/performance_agent.py",
     '    leaves, covered, missing = _leaf_coverage(system, text)\n'
     '    return {"leaves": leaves, "covered": covered, "missing_ids": missing}\n'
     '\n'
     '\n'
     'def _markdown_table_rows',
     '    leaves, covered, missing = _leaf_coverage(system, text)\n'
     '    return {"leaves": leaves, "covered": leaves, "missing_ids": []}\n'
     '\n'
     '\n'
     'def _markdown_table_rows'),
    ("表格行数把表头也算进去",
     "backend/agents/performance_agent.py",
     '        if "三级指标" in stripped and "评分标准" in stripped:\n            continue',
     '        if False:\n            continue'),
    ("节点不落步骤 5 覆盖度",
     "backend/agents/performance_agent.py",
     '        state["step5_coverage"] = coverage',
     '        state["step5_coverage"] = {}'),
    ("节点不落步骤 6 表格审计",
     "backend/agents/performance_agent.py",
     '        state["step6_table"] = table',
     '        state["step6_table"] = {}'),
    ("步骤 5 覆盖不足时不再告警",
     "backend/agents/performance_agent.py",
     '        if coverage["missing_ids"]:',
     '        if False:'),
    ("覆盖度判据改成按 id 匹配（永远匹配不上）",
     "backend/agents/performance_agent.py",
     '    missing = [leaf.get("id") for leaf in leaves\n'
     '               if str(leaf.get("name") or "").strip() and str(leaf.get("name")) not in body]',
     '    missing = [leaf.get("id") for leaf in leaves\n'
     '               if str(leaf.get("id") or "") not in body]'),
    ("备份白名单漏掉步骤 5/6 的覆盖度",
     "backend/agents/performance_agent.py",
     '                "step5_coverage", "step6_table",',
     '                "step5_coverage_", "step6_table_",'),
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
            [PY, "-m", "pytest", *TARGET_TESTS.split(), "-q", "--no-header",
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
