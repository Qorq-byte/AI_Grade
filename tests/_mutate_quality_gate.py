"""变异检验：故意改坏「质量门禁」，确认 tests/test_quality_gate.py 真的会失败。

每条只改一处。抓不住的变异体 = 对应用例是「测了相邻的东西」。
串行跑（会临时改写源文件），跑完必须 `git status --short` + `grep` 关键行复核。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
BACKEND = [PY, "-m", "pytest", "tests/test_quality_gate.py", "-q", "--no-header",
           "-p", "no:cacheprovider"]

QUALITY = "backend/services/quality.py"
ROUTES = "backend/routes/client_routes.py"
AGENT = "backend/agents/performance_agent.py"

MUTATIONS = [
    ("阈值改成全局一个数（1,000 字）", QUALITY,
     "    elif median and chars < median * THIN_RATIO:",
     "    elif median and chars < 1000:"),
    ("偏少永不触发", QUALITY,
     "THIN_RATIO = 0.20",
     "THIN_RATIO = 0.0"),
    ("偏少阈值放到 0.9（正常篇幅也判偏少）", QUALITY,
     "THIN_RATIO = 0.20",
     "THIN_RATIO = 0.90"),
    ("step_results 只认 int 键（备份里是字符串）", QUALITY,
     "        try:\n            step = int(key)\n        except (TypeError, ValueError):\n            continue",
     "        step = key\n        if not isinstance(step, int):\n            continue"),
    ("不看 completed_steps，一律检查 14 步", QUALITY,
     "    steps = set()\n    for item in state.get(\"completed_steps\") or []:",
     "    return list(range(1, 15))\n    steps = set()\n    for item in state.get(\"completed_steps\") or []:"),
    ("空正文被判成「偏少」而不是「缺失」", QUALITY,
     '    if not chars:\n        status, note = "missing", "这一步还没有内容"',
     '    if not chars:\n        status, note = "thin", "这一步还没有内容"'),
    ("台账缺失时当作 0 处理（没数据即通过）", QUALITY,
     "    if not total:\n        return None",
     "    if not total:\n        return {\"total\": 0, \"accepted\": 0, \"ratio\": 0.0,"
     " \"no_evidence\": 0, \"unmatched\": 0, \"reasons\": {}}"),
    ("落地率阈值关掉", QUALITY,
     "MIN_LANDING_RATIO = 0.5",
     "MIN_LANDING_RATIO = 0.0"),
    ("小样本也判落地率", QUALITY,
     "MIN_LANDING_SAMPLE = 5",
     "MIN_LANDING_SAMPLE = 1"),
    ("缺台账不进 unavailable", QUALITY,
     "            unavailable.append({\n                \"code\": \"SCORE_NOT_LANDED\", \"label\": \"评分落地\",",
     "            pass\n            _unused = ({\n                \"code\": \"SCORE_NOT_LANDED\", \"label\": \"评分落地\","),
    ("verified 把没跑过的判据也算进去", QUALITY,
     '    verified = ["STEP_CONTENT", "STEP_TRUNCATED"]',
     '    verified = ["STEP_CONTENT", "STEP_TRUNCATED", "SCORE_NOT_LANDED",'
     ' "STANDARD_CARRY_VERDICT"]'),
    ("verdict 永远是 pass", QUALITY,
     '    if review:\n        verdict = "review"',
     '    if False:\n        verdict = "review"'),
    ("partial 退化成 unknown", QUALITY,
     '        verdict = "partial"',
     '        verdict = "unknown"'),
    ("标准纯净度命中不报", QUALITY,
     "    elif purity.get(\"with_verdict\"):",
     "    elif False:"),
    ("不搬 consistency 的 blocking 结论", QUALITY,
     "        if blocking:\n            detail",
     "        if False:\n            detail"),
    ("把 consistency 的 warning 级也搬上来（两套口径打架）", QUALITY,
     "                    if item.get(\"blocking\")]",
     "                    if True]"),
    ("报告列表不算质量结论", ROUTES,
     "            review = quality.review_brief(quality.check_session(r.session_id))",
     "            review = quality.review_brief(quality.UNCHECKED)"),
    ("报告列表把未验证当通过", ROUTES,
     "            review = {\"checked\": False, \"verdict\": \"unknown\", \"required\": False, \"reasons\": []}",
     "            review = {\"checked\": True, \"verdict\": \"pass\", \"required\": False, \"reasons\": []}"),
    ("standard_purity 又不在白名单里", AGENT,
     '                "standard_purity",',
     '                # "standard_purity",'),
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
        result = subprocess.run(BACKEND, capture_output=True, text=True)
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
