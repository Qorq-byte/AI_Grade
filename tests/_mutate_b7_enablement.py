"""变异检验：改坏「让降级可用」的三件事（批次 32）。

守三件事：① 先重试同一模型再换；② 配置入口能存能读、上限 2；③ 导出件带降级标注。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TARGET = [PY, "-m", "pytest", "tests/test_b7_enablement.py", "tests/test_llm_resilience.py",
          "-q", "--no-header", "-p", "no:cacheprovider"]

AGENT = "backend/agents/performance_agent.py"
ROUTES = "backend/routes/agent_routes.py"
EXPORT = "backend/routes/export_routes.py"
ADMIN = "backend/routes/admin_routes.py"

MUTATIONS = [
    ("不重试同一模型（直接换备选）", AGENT,
     "LLM_RETRY_ATTEMPTS = 1\nLLM_RETRY_BACKOFF_SECONDS = 2.0",
     "LLM_RETRY_ATTEMPTS = 0\nLLM_RETRY_BACKOFF_SECONDS = 2.0"),
    ("新会话不读设置的备选模型（能力仍等于没启用）", AGENT,
     '            "fallback_models": fallback_models_from_settings(),',
     '            "fallback_models": [],'),
    ("读设置失败直接抛（工作流起不来）", AGENT,
     '    except Exception:  # noqa: BLE001\n        logger.debug("读取备选模型设置失败，按未配置处理")\n        return []',
     '    except ValueError:  # noqa: BLE001\n        logger.debug("读取备选模型设置失败，按未配置处理")\n        return []'),
    ("备选上限放到 3", ADMIN,
     '    if len(cleaned) > 2:\n        raise HTTPException(status_code=400, detail="备选模型最多 2 个")',
     '    if len(cleaned) > 3:\n        raise HTTPException(status_code=400, detail="备选模型最多 2 个")'),
    ("写备选时覆盖掉其它系统设置", ADMIN,
     '    current = {}\n    if cfg.api_base_url:',
     '    current = {}\n    if False:'),
    ("导出件不追加降级标注", EXPORT,
     "            note = _fallback_note(_get_workflow_values(session_id))",
     '            note = ""'),
    ("标注把主模型也算成降级", EXPORT,
     '        if item.get("outcome") != "ok" or item.get("model") == primary:',
     '        if item.get("outcome") != "ok":'),
    ("标注不去重（同一步重复列）", EXPORT,
     "        if pair not in hits:\n            hits.append(pair)",
     "        hits.append(pair)"),
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
        open(path, "w", encoding="utf-8").write(originals[path].replace(old, new, 1))
        result = subprocess.run(TARGET, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "fail" in item]
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
