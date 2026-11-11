"""变异检验：故意改坏「一致性拦截」，确认 tests/test_consistency_export_gate.py 真的会失败。

每条只改一处；后端跑 pytest，前端那条跑 node --test。
抓不住的变异体 = 对应用例是「测了相邻的东西」。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
BACKEND = [PY, "-m", "pytest", "tests/test_consistency_export_gate.py", "-q", "--no-header",
           "-p", "no:cacheprovider"]
FRONTEND = ["node", "--test", "tests/test_shared_frontend.cjs"]

CONSISTENCY = "backend/services/consistency.py"
ROUTES = "backend/routes/agent_routes.py"
EXPORT = "backend/routes/export_routes.py"
AGENT = "backend/agents/performance_agent.py"
API_JS = "frontend/shared/api.js"

MUTATIONS = [
    ("blocking 永远为 False", CONSISTENCY,
     '        item["blocking"] = item["code"] in BLOCKING_CODES',
     '        item["blocking"] = False'),
    ("exportable 恒为 True", CONSISTENCY,
     '        "exportable": not blocking,',
     '        "exportable": True,'),
    ("issue 没有唯一编号", CONSISTENCY,
     '        item["id"] = f"{item[\'code\']}#{counters[item[\'code\']]}"',
     '        item["id"] = item["code"]'),
    ("缺章不再检测", CONSISTENCY,
     "    count = report.count(EMPTY_CHAPTER_MARKER)",
     "    count = 0"),
    ("只认空章节、漏掉步骤失败那句", CONSISTENCY,
     'EMPTY_CHAPTER_MARKER = "（本部分内容尚未生成"',
     'EMPTY_CHAPTER_MARKER = "（本部分内容尚未生成）"'),
    ("覆盖缺口检查关掉", CONSISTENCY,
     "    names = _low_score_leaves(state)",
     "    names = []"),
    ("步骤没跑也要报覆盖缺口", CONSISTENCY,
     "        if not isinstance(text, str) or not text.strip() or not names:\n            continue",
     "        text = text or \"\"\n        if not names:\n            continue"),
    ("建议无问题不报", CONSISTENCY,
     "    if isinstance(problems, str) and not problems.strip() \\\n"
     "            and isinstance(suggestions, str) and suggestions.strip():",
     "    if False:"),
    ("导出闸门无视 force", EXPORT,
     "        if not request.force:",
     "        if True:"),
    ("状态读不到时直接抛错（不 fail-open）", EXPORT,
     "    except HTTPException:\n        return []\n    except Exception:\n"
     "        logger.exception(f\"导出前一致性检查失败，放行: {session_id}\")\n        return []",
     "    except HTTPException:\n        raise\n    except Exception:\n        raise"),
    ("409 不带 X-Export-Blocked 标记头", EXPORT,
     '                                    headers={"X-Export-Blocked": "1"})',
     "                                    )"),
    ("步骤 11 替换后不记账", AGENT,
     "        if replaced:",
     "        if False:"),
    ("步骤 11 只认带冒号的写法", AGENT,
     '    r"\\s*(?:为|是)?\\s*[：:]?\\s*(?:[0-9]+(?:\\.[0-9]+)?\\s*分|不合格|待核实|优秀|良好|合格)"',
     '    r"\\s*(?:为|是)?\\s*[：:]\\s*(?:[0-9]+(?:\\.[0-9]+)?\\s*分|不合格|待核实|优秀|良好|合格)"'),
    ("步骤 11 把普通叙述也当得分行删掉", AGENT,
     '    r"\\s*(?:为|是)?\\s*[：:]?\\s*(?:[0-9]+(?:\\.[0-9]+)?\\s*分|不合格|待核实|优秀|良好|合格)"',
     '    r"\\s*(?:为|是)?\\s*[：:]?\\s*.*"'),
    ("前端不识别导出标记（每次 409 都弹窗重放）", API_JS,
     "    var EXPORT_BLOCKED_HEADER = 'X-Export-Blocked';",
     "    var EXPORT_BLOCKED_HEADER = 'X-Never-Sent';",
     FRONTEND),
]

originals = {}
for entry in MUTATIONS:
    path = entry[1]
    if path not in originals:
        originals[path] = open(path, encoding="utf-8").read()

failures = []
try:
    for name, path, old, new, *rest in MUTATIONS:
        command = rest[0] if rest else BACKEND
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        open(path, "w", encoding="utf-8").write(originals[path].replace(old, new, 1))
        result = subprocess.run(command, capture_output=True, text=True)
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
