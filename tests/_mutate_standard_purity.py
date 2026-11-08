"""变异检验（批次 18）：故意改坏「标准里不许夹结论」的两侧，确认新用例真的会失败。

两类变异：
  * 提示词侧（`default_prompts.py`）：删条文、删例子、放宽禁令；
  * 审计侧（`performance_agent.py`）：词表收窄、顺手清洗原文、分母混入非叶子、
    对空字段崩溃、节点不记账、把命中刷进用户可见告警、失败分支写假的 0。

每个变异体先离线 compile()，再只跑能观察它的用例文件。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
NEW = "tests/test_step3_standard_purity.py"
PROMPTS = "backend/agents/prompts/default_prompts.py"
AGENT = "backend/agents/performance_agent.py"

# (说明, 文件, 原文, 替换)
MUTATIONS = [
    ("放宽成「可以把结论写进 scoring_standard」", PROMPTS,
     "2. **严禁**把本项目的资料核对结论写进 `scoring_standard`。出现下列任一字样即属违规：",
     "2. 可以把本项目的资料核对结论写进 `scoring_standard`："),
    ("删掉反例（模型会把禁令读成「少写几个字」）", PROMPTS,
     "   - 反例（禁止）：「到位率100%得满分；每低1个百分点扣0.2分。⚠️ 现有资料中未提供实际到位金额，无法判断。」\n",
     ""),
    ("删掉「资料核对提示」这个落点（结论无处可去，只能回字段）", PROMPTS,
     "以「## 资料核对提示」为标题，", ""),
    ("放开「资料不足就删指标」", PROMPTS,
     "4. 资料不足**不得成为删减指标的理由**", "4. 资料不足时可以成为删减指标的理由"),
    ("词表收窄成只剩「待核实」", AGENT,
     'STANDARD_VERDICT_MARKERS = ("待核实", "无法判断", "资料不足", "现有资料未", "本项资料未")',
     'STANDARD_VERDICT_MARKERS = ("待核实",)'),
    ("审计顺手把命中条目「清洗」掉（把模型行为藏起来）", AGENT,
     "                   for marker in STANDARD_VERDICT_MARKERS)]",
     "                   for marker in STANDARD_VERDICT_MARKERS)]\n"
     "    for leaf in leaves:\n"
     "        if leaf.get('id') in hits:\n"
     "            leaf['scoring_standard'] = '已清洗'"),
    ("分母混进一级节点", AGENT,
     '    leaves = list(indicator_leaves((system or {}).get("level1_indicators") or []))',
     '    leaves = list(indicator_leaves((system or {}).get("level1_indicators") or []))\n'
     '    leaves += list((system or {}).get("level1_indicators") or [])'),
    ("字段缺失时直接取值（真实备份里有 null / 缺键）", AGENT,
     'if any(marker in str(leaf.get("scoring_standard") or "")',
     'if any(marker in str(leaf["scoring_standard"])'),
    ("节点不把审计写进 state", AGENT,
     '            state["standard_purity"] = purity', "            _ = purity"),
    ("把命中刷进用户可见告警（74% 的历史会话都会命中）", AGENT,
     '            state["standard_purity"] = purity',
     '            state["standard_purity"] = purity\n'
     '            state.setdefault("warnings", []).append("评分标准夹带资料结论")'),
    ("树没建起来也写一个假的 0 条命中", AGENT,
     '            state.setdefault("warnings", []).append("步骤 3 待人工复核：" + issue)',
     '            state.setdefault("warnings", []).append("步骤 3 待人工复核：" + issue)\n'
     '            state["standard_purity"] = {"leaves": 0, "with_verdict": 0, "ids": []}'),
]

originals = {path: Path(path).read_text(encoding="utf-8") for path in (PROMPTS, AGENT)}
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
            [PY, "-m", "pytest", NEW, "-q", "--no-header", "-p", "no:cacheprovider"],
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
