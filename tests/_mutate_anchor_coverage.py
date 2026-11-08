"""变异检验（批次 19）：故意改坏覆盖与原因分类，确认新用例真的会失败。

三类变异：
  * 覆盖侧（`workflow_evidence.py`）：取消自适应、不补未命中块、预算/条数上限失效、
    放开到其它步骤、覆盖显式限额；
  * 原因侧（`evidence_anchor.py`）：空引用、只到文件、引错文件、行号越界四类判错；
  * 台账侧（`performance_agent.py`）：映射不生效、不记原因。

每个变异体先离线 compile()，再只跑能观察它的用例文件。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
NEW = "tests/test_step7_anchor_coverage.py"
OLD = "tests/test_step7_scoring.py"
WE = "backend/services/workflow_evidence.py"
EA = "backend/services/evidence_anchor.py"
AG = "backend/agents/performance_agent.py"

# (说明, 文件, 原文, 替换, 用例文件)
MUTATIONS = [
    ("取消步骤 7 的自适应覆盖", WE,
     "ADAPTIVE_PROJECT_STEPS = (7,)", "ADAPTIVE_PROJECT_STEPS = ()", NEW),
    ("只调高条数、不补未命中的块（BM25 只返回命中的那几条）", WE,
     "    if adaptive:\n        ranked = _augment_with_unscored(ranked, project, project_limit)\n",
     "", NEW),
    ("字符预算失效（小材料判定恒成立）", WE,
     "PROJECT_CHAR_BUDGET = 6000", "PROJECT_CHAR_BUDGET = 10 ** 9", NEW),
    ("条数上限失效", WE,
     "MAX_ADAPTIVE_PROJECT_BLOCKS = 40", "MAX_ADAPTIVE_PROJECT_BLOCKS = 10 ** 6", NEW),
    ("放开到所有步骤（不限步骤 7）", WE,
     "    if step not in ADAPTIVE_PROJECT_STEPS or limit != PROJECT_LIMIT:",
     "    if limit != PROJECT_LIMIT:", NEW),
    ("覆盖调用方显式指定的限额", WE,
     "    if step not in ADAPTIVE_PROJECT_STEPS or limit != PROJECT_LIMIT:",
     "    if step not in ADAPTIVE_PROJECT_STEPS:", NEW),
    ("空引用不再单列（退回 no_match）", EA,
     '        return _fail(raw, (), "empty")', '        return _fail(raw, (), "no_match")', NEW),
    ("只到文件被判成「编号编造」", EA,
     '        return "too_coarse"', '        return "unknown_id"', NEW),
    ("引错文件被归进「只到文件」", EA,
     '        if _mentions_other_file(text, known):\n            return "unknown_file"\n'
     '        wanted = int(_LINE_NO.search(text).group(1))',
     '        if _mentions_other_file(text, known):\n            return "too_coarse"\n'
     '        wanted = int(_LINE_NO.search(text).group(1))', NEW),
    ("行号越界与无法核验混为一谈", EA,
     '                    return "line_out_of_range" if wanted > int(total) else "line_unverifiable"',
     '                    return "line_unverifiable"', NEW),
    ("原因到状态的映射失效（全部退回「引用无法定位」）", AG,
     'ANCHOR_STATUS_BY_REASON = {\n    "too_coarse": "anchor_too_coarse",\n}',
     "ANCHOR_STATUS_BY_REASON = {}", NEW),
    ("台账不记原因（分桶又变回一个数）", AG,
     '                              "anchor_reason": anchor.reason,\n', "", NEW),
    ("只写文件名的引用被放行（口径分开变成放水）", AG,
     'ANCHOR_STATUS_BY_REASON = {\n    "too_coarse": "anchor_too_coarse",\n}',
     'ANCHOR_STATUS_BY_REASON = {\n    "too_coarse": "accepted",\n}', NEW + " " + OLD),
]

originals = {path: Path(path).read_text(encoding="utf-8") for path in (WE, EA, AG)}
failures = []


def compile_ok(path):
    try:
        compile(Path(path).read_text(encoding="utf-8"), path, "exec")
        return True
    except SyntaxError as exc:
        print(f"  变异体语法错误，跳过：{exc}")
        return False


try:
    for name, path, old, new, tests in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(path).write_text(originals[path].replace(old, new, 1), encoding="utf-8")
        if not compile_ok(path):
            Path(path).write_text(originals[path], encoding="utf-8")
            continue
        result = subprocess.run(
            [PY, "-m", "pytest", *tests.split(), "-q", "--no-header", "-p", "no:cacheprovider"],
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
