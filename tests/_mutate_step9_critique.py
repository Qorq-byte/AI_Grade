"""变异检验：改坏「步骤 9 漏项补全」，确认 tests/test_step9_critique.py 真的会失败。

串行跑（会临时改写源文件），跑完必须 `git status --short` + `grep` 关键行复核。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TARGET = [PY, "-m", "pytest", "tests/test_step9_critique.py", "-q", "--no-header",
          "-p", "no:cacheprovider"]

AGENT = "backend/agents/performance_agent.py"
ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("没漏项也照样调一次模型", AGENT,
     "        if not missing:\n            return content, ledger",
     "        if False:\n            return content, ledger"),
    ("修订把原本点名的指标改丢也不管", AGENT,
     "        elif lost:\n            ledger[\"rejected\"] = f\"改丢了原本点名的指标：{'、'.join(lost[:3])}\"",
     "        elif False:\n            ledger[\"rejected\"] = \"\""),
    ("长度塌了也接受（下界去掉）", AGENT,
     "        elif not (0.8 <= ratio <= 2.0):",
     "        elif not (0.0 <= ratio <= 2.0):"),
    ("一个字没补也接受", AGENT,
     "        elif not any(name in revised for name in missing):\n            ledger[\"rejected\"] = \"一个都没补进去\"",
     "        elif False:\n            ledger[\"rejected\"] = \"\""),
    ("调用失败直接抛（拖垮步骤 9）", AGENT,
     "        except Exception as exc:  # 补全是增强项，失败不能拖垮主流程\n"
     "            logger.warning(f\"[Step 9] 指标补全调用失败：{exc}\")\n"
     "            ledger[\"rejected\"] = f\"调用失败：{exc}\"\n"
     "            return content, ledger",
     "        except ValueError as exc:\n"
     "            ledger[\"rejected\"] = f\"调用失败：{exc}\"\n"
     "            return content, ledger"),
    ("自己算一份低分名单（不走共享口径）", AGENT,
     "        names = consistency.low_score_indicator_names(state)",
     "        names = [item.get(\"name\") for item in\n"
     "                 (state.get(\"indicator_system\") or {}).get(\"level1_indicators\") or []\n"
     "                 if item.get(\"name\")]"),
    ("状态层默认开（恢复旧会话也会多花一次调用）", AGENT,
     '            "critique_step9": False,',
     '            "critique_step9": True,'),
    ("台账与原文不进备份白名单", AGENT,
     '                "step9_critique", "problems_analysis_pre_critique",',
     '                # "step9_critique", "problems_analysis_pre_critique",'),
    ("轮询接口把补全前全文也带出去", ROUTES,
     '        "step9_critique": values.get("step9_critique", {}) or {},',
     '        "step9_critique": values.get("step9_critique", {}) or {},\n'
     '        "problems_analysis_pre_critique": values.get("problems_analysis_pre_critique", ""),'),
    ("启动默认关（这项改动对真实报告等于没做）", ROUTES,
     "    critique_step9: bool = True",
     "    critique_step9: bool = False"),
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
