"""变异检验：批次 56——问卷卡片可点 + 打开只读不生成。

守四件事：
1. 网格不绑点击委托（卡片又变成"只有 30px 可点"）；
2. 卡片不带 `data-sid`（委托拿不到会话号，点了还是没反应）；
3. 打开流程改回"先 POST 生成"（会按 max_questions 重新出一版，题号全换）；
4. 内联 `onclick="openQuestionnaire(...)"` 回来（项目名带单引号就把处理器打断）。

四条都由 `tests/test_frontend_contract.py` 抓 —— 它们是静态接线，
真行为由 `tests/_probe_questionnaire_fill.py` 在真浏览器里验（不进 CI）。

跑完请 `git status --short` + grep 关键行验伤（B54 踩过"脚本被打断把变异体留在盘上"）。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [PY, "-m", "pytest", "tests/test_frontend_contract.py", "-q", "--no-header",
         "-p", "no:cacheprovider"]

PAGE = "frontend/client/index.html"

MUTATIONS = [
    ("卡片不绑点击委托", PAGE,
     "    grid.innerHTML=h;\n    bindQuestionnaireGrid();",
     "    grid.innerHTML=h;"),
    ("卡片不带 data-sid", PAGE,
     '<div class="stat-card" id="qcard_\'+sid+\'" data-sid="\'+sid+\'"',
     '<div class="stat-card" id="qcard_\'+sid+\'"'),
    ("打开改回先 POST 生成", PAGE,
     "    fetchPublishedQuestionnaire(sid).catch(function(err){",
     "    api('POST','/api/agent/workflow/questionnaire',{session_id:sid,max_questions:20})"
     ".catch(function(err){"),
    ("内联 onclick 回来", PAGE,
     '<div class="stat-label" style="font-size:14px;font-weight:600;flex:1;min-width:0">',
     '<div class="stat-label" onclick="openQuestionnaire(\'x\',\'y\')" '
     'style="font-size:14px;font-weight:600;flex:1;min-width:0">'),
]

original = Path(PAGE).read_text(encoding="utf-8")
failures = []
try:
    for name, path, old, new in MUTATIONS:
        if old not in original:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到，锚点可能已随代码改动失效）")
            continue
        Path(path).write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            result = subprocess.run(TESTS, capture_output=True, text=True)
            line = [item for item in result.stdout.splitlines()
                    if "passed" in item or "failed" in item]
            caught = result.returncode != 0
            print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
            if not caught:
                failures.append(name)
        finally:
            Path(path).write_text(original, encoding="utf-8")
finally:
    Path(PAGE).write_text(original, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
