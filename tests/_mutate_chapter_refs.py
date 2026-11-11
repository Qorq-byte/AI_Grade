"""变异检验：批次 39（B5 第二步）——章节级引用。

这批的风险是「片段检索接上了，其实没按问题命中」：不检索、不封顶、
offset 永远 0、引用标签不可定位、路由忘了传问题 —— 聊天照样有回答。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_chat_context.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/chat_context.py"
ROUTES = "backend/routes/client_routes.py"

MUTATIONS = [
    ("不检索章节（上下文只剩问题清单）", SERVICE,
     "    snippets = retrieve_chapter_snippets(values, query) if query else []",
     "    snippets = []"),
    ("片段不封顶（命中多少给多少）", SERVICE,
     """        if len(picked) >= limit:
            break""",
     """        if False:
            break"""),
    ("offset 永远是 0（点不开到第几段）", SERVICE,
     '            "locator": info["locator"], "offset": info["offset"],',
     '            "locator": info["locator"], "offset": 0,'),
    ("引用标签不可定位（只剩一个「引用」）", SERVICE,
     '                "label": f"步骤{snippet[\'step\']}·{snippet[\'locator\']}",',
     '                "label": "引用",'),
    ("路由不把用户问题传给检索", ROUTES,
     '    return chat_context.build_report_context(values, record.project_name or "",\n'
     '                                               query=req.message or "")',
     '    return chat_context.build_report_context(values, record.project_name or "", query="")'),
    ("章节名不填（片段没有出处）", SERVICE,
     '        chapter = STEP_META.get(step, {}).get("name", f"步骤{step}")',
     '        chapter = ""'),
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
