"""变异检验：批次 45（B5 第五步）——报告正文也是检索源 + 强制标注来源。

风险是「报告源加上了，其实没生效」：报告不进语料、去重失效、章节名/偏移/标签缺失、
指令不提标注来源 —— 离线评测照样 21/21 也发现不了这些细节。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_chat_context.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/chat_context.py"

MUTATIONS = [
    ("报告正文不进检索语料", SERVICE,
     "    report = values.get(\"final_report\") or \"\"\n"
     "    if isinstance(report, str) and report.strip():",
     "    report = values.get(\"final_report\") or \"\"\n    if False:"),
    ("跨源去重失效（同一段占两个名额）", SERVICE,
     "        if any(key and (key in prev or prev in key) for prev in seen):",
     "        if False:"),
    ("报告片段不带章节名", SERVICE,
     "                heading = first.lstrip(\"#\").strip()[:24]",
     "                heading = \"\""),
    ("报告片段 offset 归零", SERVICE,
     '            meta[chunk_id] = {"step": None, "chapter": heading or "报告正文", "offset": offset,',
     '            meta[chunk_id] = {"step": None, "chapter": heading or "报告正文", "offset": 0,'),
    ("报告片段标签不带「报告·」", SERVICE,
     '                              "label": f"报告·{heading or \'正文\'}·第{index + 1}段"}',
     '                              "label": f"第{index + 1}段"}'),
    ("指令不提「标注来源」", SERVICE,
     '        "回答报告相关问题时，每条结论后用（编号）标注来源，例如（S7P1）、（P1-3-1）、"\n'
     '        "（INDICATOR_MISSING#1）；没有编号支撑的话不要写成结论。",',
     '        "",'),
]

originals = {}
for entry in MUTATIONS:
    path = entry[1]
    if path not in originals:
        originals[path] = Path(path).read_text(encoding="utf-8")

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
        Path(path).write_text(mutated, encoding="utf-8")
        result = subprocess.run(TESTS, capture_output=True, text=True)
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
