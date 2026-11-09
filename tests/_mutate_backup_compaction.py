"""变异检验：故意改坏紧凑编码，确认 tests/test_state_backup_compaction.py 真的会失败。

每条变异体都只改一处、跑一组目标用例，抓不住就说明对应用例是「grep 源码」式的弱断言。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TARGET = ["tests/test_state_backup_compaction.py"]

SERVICE = "backend/services/state_backup.py"
AGENT = "backend/agents/performance_agent.py"

MUTATIONS = [
    ("正文不再合并（同一段话存两份）", SERVICE,
     "    if field and backup.get(field) == content:",
     "    if False and backup.get(field) == content:"),
    ("raw_outputs 不再丢弃", SERVICE,
     '    if out.get("raw_outputs") == {"default": content}:',
     "    if False:"),
    ("不看是否相同，一律合并", SERVICE,
     "    if field and backup.get(field) == content:",
     "    if field:"),
    ("多模型输出也当重复丢掉", SERVICE,
     '    if out.get("raw_outputs") == {"default": content}:',
     '    if "raw_outputs" in out:'),
    ("读盘不还原正文", SERVICE,
     '            out["content"] = value',
     '            out["content"] = ""'),
    ("读盘不补 raw_outputs", SERVICE,
     '    if "raw_outputs" not in out and isinstance(out.get("content"), str):\n'
     '        out["raw_outputs"] = {"default": out["content"]}',
     "    pass"),
    ("兜底预览不存", SERVICE,
     "        out[PREVIEW_KEY] = content[:CONTENT_PREVIEW_CHARS]",
     '        out[PREVIEW_KEY] = ""'),
    ("写盘退回老格式（不紧凑）", SERVICE,
     '    json.dump(compact(backup), target, ensure_ascii=False, separators=(",", ":"))',
     '    json.dump(backup, target, ensure_ascii=False, indent=2)'),
    ("load 绕过 hydrate", AGENT,
     "                    return state_backup.hydrate(backup) if isinstance(backup, dict) else {}",
     "                    return backup if isinstance(backup, dict) else {}"),
    ("落盘绕过紧凑层", AGENT,
     "                    state_backup.dump(backup, target)",
     '                    json.dump(backup, target, ensure_ascii=False, indent=2)'),
    ("迁移脚本只报告不落盘", "compact_state_backups.py",
     "        performance_agent._publish_backup(temp_path, path)",
     "        pass"),
]

originals = {}
for _, path, _, _ in MUTATIONS:
    if path not in originals:
        originals[path] = open(path, encoding="utf-8").read()

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
