"""变异检验（批次 20）：故意改坏步骤 7 的长度纪律，确认新用例真的会失败。

覆盖两件事：**顺序/字数**（提示词）与**预算/记账**（代码）。只跑本批相关的两个用例文件
——提示词的改动会连带影响批次 16 的处置口径用例，所以两个都跑。

每个变异体先离线 compile()，避免「语法错误被当成抓到」这个老坑。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = ("tests/test_step7_output_length.py tests/test_step7_prompt_discipline.py")

# (说明, 文件, 原文, 替换)
MUTATIONS = [
    ("DeepSeek 又不认预算（去掉 extra_body，只剩 max_completion_tokens）",
     "backend/agents/performance_agent.py",
     '        if get_model_provider(model_name) == "deepseek":\n'
     '            kwargs["extra_body"] = {"max_tokens": budget}',
     '        if False:\n            kwargs["extra_body"] = {"max_tokens": budget}'),
    ("步骤 7 的预算退回全局 4000（等于没给这步留空间）",
     "backend/agents/performance_agent.py",
     "STEP_OUTPUT_BUDGETS = {7: 6000}",
     "STEP_OUTPUT_BUDGETS = {7: 4000}"),
    ("预算表被顺手扩到别的步骤（会截断 9 个步骤里的正文）",
     "backend/agents/performance_agent.py",
     "STEP_OUTPUT_BUDGETS = {7: 6000}",
     "STEP_OUTPUT_BUDGETS = {7: 6000, 8: 6000}"),
    ("调用时不用带预算的实例（前面的 bind 白做）",
     "backend/agents/performance_agent.py",
     "        llm = self._llm_for_step(step_number)",
     "        llm = self.base_agent.llm"),
    ("截断永不记账（finish_reason 判定反转）",
     "backend/agents/performance_agent.py",
     "        if finish_reason not in TRUNCATION_FINISH_REASONS:",
     "        if True:"),
    ("截断不写 warning（只剩日志，用户看不到）",
     "backend/agents/performance_agent.py",
     "            if warning not in warnings:\n                warnings.append(warning)",
     "            if False:\n                warnings.append(warning)"),
    ("步骤 7 节点不落长度画像",
     "backend/agents/performance_agent.py",
     '        state["step7_length"] = length.as_dict()',
     '        state["step7_length"] = {}'),
    ("系统提示词退回「先正文后 JSON」",
     "backend/agents/prompts/default_prompts.py",
     "1. **先输出评分 JSON，再写正文**。",
     "1. **先写正文，最后输出评分 JSON**。"),
    ("Human 契约退回「先正文后 JSON」",
     "backend/agents/prompts/default_prompts.py",
     "1. **先输出 JSON，再写正文**：回答的第一段就是 ```json 代码块，形状固定为：",
     "1. **先写正文，最后输出 JSON**：正文之后附一个 JSON 对象，形状固定为："),
    ("理由字数上限放宽十倍",
     "backend/agents/prompts/default_prompts.py",
     "3. JSON 的 reason 每条不超过 60 字：",
     "3. JSON 的 reason 每条不超过 600 字："),
    ("审计把「JSON 在前」判成恒真",
     "backend/agents/tools/structured_output.py",
     "        result.json_first = 0 <= position <= STEP7_JSON_LEAD_CHARS",
     "        result.json_first = True"),
    ("审计不再统计超字数理由",
     "backend/agents/tools/structured_output.py",
     "                    result.reason_over_limit = sum(\n"
     "                        1 for value in lengths if value > STEP7_REASON_CHAR_LIMIT\n"
     "                    )",
     "                    result.reason_over_limit = 0"),
    ("备份白名单漏掉长度画像与截断记账（重启后查不到原因）",
     "backend/agents/performance_agent.py",
     '                "step7_length", "truncated_steps",',
     '                "step7_length_", "truncated_steps_",'),
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
