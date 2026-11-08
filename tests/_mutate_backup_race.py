"""变异检验：故意改坏备份文件的并发保护，确认 tests/test_state_backup_race.py 真的会失败。"""
import subprocess
import sys

PY = ".venv/Scripts/python"
TARGET = ["tests/test_state_backup_race.py"]

MUTATIONS = [
    ("发布不重试（目标被占用就直接抛）", "backend/agents/performance_agent.py",
     "    _retry_on_transient_permission(lambda: temp_path.replace(path), budget)",
     "    temp_path.replace(path)"),
    ("发布后不清临时文件", "backend/agents/performance_agent.py",
     """            finally:
                temp_path.unlink(missing_ok=True)""",
     """            finally:
                pass"""),
    ("删除假装成功（不真删文件）", "backend/agents/performance_agent.py",
     "    return _retry_on_transient_permission(remove, budget)",
     "    return True"),
    ("列表接口退回裸 open（绕过锁）", "backend/routes/agent_routes.py",
     """                        backup = PerformanceAgent.load_state_backup(sid)
                        if not backup:
                            continue""",
     """                        with open(os.path.join(backup_dir, fname), "r", encoding="utf-8") as f:
                            backup = json.load(f)
                        if not backup:
                            continue"""),
]

files = {path: open(path, encoding="utf-8").read() for _, path, _, _ in MUTATIONS}
originals = dict(files)
failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        open(path, "w", encoding="utf-8").write(originals[path].replace(old, new, 1))
        result = subprocess.run(
            [PY, "-m", "pytest", *TARGET, "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True, text=True)
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
