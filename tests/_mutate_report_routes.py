"""变异检验：批次 35 搬走的「报告」域，行为必须还是原来的。

搬代码最容易出的不是写错逻辑，而是**搬完少一行**：少一个 409 闸门、
少一次落盘、少一条重复检查。每条只改一处，串行跑。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_report_routes.py", "tests/test_route_split_35.py",
         "-q", "-x", "--no-header", "-p", "no:cacheprovider"]

REPORT = "backend/routes/report_routes.py"
EXPORT = "backend/routes/export_routes.py"

MUTATIONS = [
    ("读报告不再优先用数据库（页面上的修改被旧备份盖掉）", REPORT,
     "    if record and record.final_report is not None:",
     "    if False:"),
    ("运行中也允许保存报告（覆盖正在跑的步骤结果）", REPORT,
     "    if session_id in _shared_state._WORKFLOW_PROGRESS:",
     "    if False:"),
    ("保存报告不写库（只改内存）", REPORT,
     "        record.final_report = content",
     "        pass"),
    ("组装报告不落盘（刷新又变回旧报告）", REPORT,
     "        _save_report(req.session_id, full_report, db)",
     "        pass"),
    ("同名模块可以重复添加", REPORT,
     "        if req.module_key in modules:",
     "        if False:"),
    ("删除不存在的模块也返回成功", REPORT,
     "        if req.module_key not in modules:",
     "        if False:"),
    ("章节顺序不持久化", REPORT,
     '        _persist_state(req.session_id, {"report_section_order": req.sections})',
     "        pass"),
    ("读取章节顺序时忽略自定义顺序", REPORT,
     "        if saved_order:",
     "        if False:"),
    ("导出样式不持久化", EXPORT,
     '        _persist_state(req.session_id, {"export_style": style})',
     "        pass"),
]

originals = {
    path: Path(path).read_text(encoding="utf-8") for path in {m[1] for m in MUTATIONS}
}

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
        open(path, "w", encoding="utf-8").write(mutated)
        result = subprocess.run(TESTS, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
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
