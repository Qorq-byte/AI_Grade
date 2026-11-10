"""变异检验：批次 34 抽出的依赖与搬走的导出域，行为必须还是原来的。

为什么这批也要变异：这批「只搬代码」，最容易出的不是逻辑写错，而是
**搬完以后某条校验没跟过来**（所有权过滤、404 口径、导出闸门、脚注 fail-open）。
每条只改一处，串行跑；跑完必须 `git status --short` + `grep` 关键行复核。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest",
         "tests/test_engineering_regressions.py", "tests/test_consistency_export_gate.py",
         "tests/test_b7_enablement.py", "tests/test_route_split_34.py",
         "-q", "-x", "--no-header", "-p", "no:cacheprovider"]

DEPS = "backend/routes/_workflow_deps.py"
EXPORT = "backend/routes/export_routes.py"

MUTATIONS = [
    ("所有权过滤失效（谁都能看别人的会话）", DEPS,
     "    if user.role != UserRole.ADMIN:\n        query = query.filter(WorkflowSession.user_id == user.id)",
     "    if False:\n        query = query.filter(WorkflowSession.user_id == user.id)"),
    ("管理员的备份兜底失效（只有报告、没有会话行的导出变 404）", DEPS,
     "    if user.role == UserRole.ADMIN and row is None and PerformanceAgent.load_state_backup(session_id):",
     "    if False:"),
    ("状态读不到时不报 404（拿空字典继续跑）", DEPS,
     '        raise HTTPException(status_code=404, detail="工作流状态不存在")',
     "        return {}"),
    ("step_results 的字符串键不转 int（备份里就是 str）", DEPS,
     '        values["step_results"] = {int(key): value for key, value in values["step_results"].items()}',
     '        values["step_results"] = {key: value for key, value in values["step_results"].items()}'),
    ("运行中也允许改指标 / 步骤", DEPS,
     "    if session_id in _shared_state._WORKFLOW_PROGRESS and updates.keys() & {",
     "    if False and updates.keys() & {"),
    ("状态不落盘（只改内存）", DEPS,
     "    PerformanceAgent.update_state_backup(session_id, persisted)",
     "    pass"),
    ("导出闸门关掉（缺章也照导）", EXPORT,
     "        if not request.force:",
     "        if False:"),
    ("force 也拦（用户明确要导也导不出去）", EXPORT,
     "        if not request.force:",
     "        if True:"),
    ("脚注不再 fail-open（读不到状态就抛）", EXPORT,
     '    except HTTPException:\n        return ""\n    except Exception:  # noqa: BLE001',
     '    except HTTPException:\n        raise\n    except Exception:  # noqa: BLE001'),
    ("导出不看幂等缓存（连点三次渲染三次）", EXPORT,
     "    cached_path = export_cache.lookup(key, content_hash)",
     "    cached_path = None"),
    ("下载不做路径穿越检查", EXPORT,
     '    if safe_name != file_name or Path(safe_name).suffix.lower() not in {".pdf", ".docx"}:',
     "    if False:"),
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
        mutated = originals[path].replace(old, new, 1)
        try:
            compile(mutated, path, "exec")
        except SyntaxError as exc:  # 语法错误的变异体会被当成「抓住」
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
