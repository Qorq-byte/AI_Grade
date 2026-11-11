"""变异检验：批次 38（B5 第一步）——聊天引用报告 / 历史预算 / 流式。

这批的风险是「看着接上了，其实没生效」：上下文没注入、历史没截断、
流式没落库、错误帧吞掉、引用别人的报告也能读 —— 界面都正常。
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
    ("上下文不列一致性问题（模型只能编）", SERVICE,
     "    if issues:",
     "    if False:"),
    ("上下文不封顶（整份报告塞进 system）", SERVICE,
     '    return {"text": _clip("\\n".join(lines), MAX_CONTEXT_CHARS), "references": references}',
     '    return {"text": "\\n".join(lines), "references": references}'),
    ("历史不按轮次截断", SERVICE,
     "    if max_turns > 0:\n        rest = rest[-max_turns:]",
     "    if False:\n        rest = rest[-max_turns:]"),
    ("按字符截断时把最后一条也丢（当前问题没了）", SERVICE,
     "    while len(rest) > 1 and size(system + rest) > max_chars:",
     "    while rest and size(system + rest) > max_chars:"),
    ("忽略显式 workflow id（总是取最近一份）", ROUTES,
     "    if req.workflow_session_id:",
     "    if False:"),
    ("非流式忘了注入上下文", ROUTES,
     '            conversation_id, db, req.model, context_text=context["text"])',
     '            conversation_id, db, req.model, context_text="")'),
    ("流式结束帧改名（前端收不到 done）", ROUTES,
     '        yield _sse("done", {"conversation_id": conversation_id, "reply": reply,',
     '        yield _sse("end", {"conversation_id": conversation_id, "reply": reply,'),
    ("流式失败不告诉前端（只有半截回答）", ROUTES,
     '            yield _sse("error", {"status": status, "detail": detail})\n            return',
     "            return"),
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
