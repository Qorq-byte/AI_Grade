"""变异检验：批次 36（B4 第一步）——refine 预览 / 证据注入 / 回滚。

每条只改一处。这批的风险不是"写错逻辑"，而是**改回旧行为也照样能跑**：
静默截断、不注入证据、preview 顺手落库、回滚拿错版本 —— 界面看起来都正常。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_refine_preview.py", "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"]

SERVICE = "backend/services/refine.py"
ROUTES = "backend/routes/agent_routes.py"

MUTATIONS = [
    ("截断只留开头（结论/合计全丢）", SERVICE,
     '    return (f"{text[:head_len]}\\n……（中间省略 {omitted} 字）……\\n{text[-tail_len:]}", True)',
     "    return (text[:head_len], True)"),
    ("截断失效（整篇塞进提示词）", SERVICE,
     "    if len(text) <= limit:\n        return text, False",
     "    if True:\n        return text, False"),
    ("不注入本步证据", SERVICE,
     '    hits = (values.get("step_evidence") or {}).get(str(step)) or []',
     "    hits = []"),
    ("上一步摘要取错步骤", SERVICE,
     '    previous = (values.get("step_results") or {}).get(step - 1)',
     '    previous = (values.get("step_results") or {}).get(step - 2)'),
    ("没有证据时不说明，模型只能编", SERVICE,
     '        parts.append("【本步证据】\\n资料不足：本步骤没有检索到可定位的证据。"',
     '        parts.append("【本步证据】"'),
    ("preview 不生效（预览也落库）", ROUTES,
     "    if req.preview:",
     "    if False:"),
    ("preview 返回的版本号多 1（采用必撞 409）", ROUTES,
     '            "version": current.get("version", 1),',
     '            "version": current.get("version", 1) + 1,'),
    ("回滚拿当前正文（等于没回滚）", ROUTES,
     '    data = _apply_step_edit(session_id, step, target.get("content") or "", "rollback",',
     '    data = _apply_step_edit(session_id, step, current.get("content") or "", "rollback",'),
    ("历史列表不标当前版本", ROUTES,
     '        "is_current": True,\n    }]',
     '        "is_current": False,\n    }]'),
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
