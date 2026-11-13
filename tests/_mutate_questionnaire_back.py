"""变异检验：批次 57——问卷区不再复制一份标记。

守三件事：
1. 返回列表时不恢复快照（表单留在原地，列表再也回不来）；
2. 返回列表后不作废列表缓存（提交过的问卷仍显示"待填写"）；
3. 硬编码的整段标记又抄回 JS 里（与静态标记两份，必然再次漂移）。

前两条由真浏览器探针抓（`_probe_questionnaire_fill.py`，不进 CI），
第三条由契约测试抓（`id="statQPending"` 只许出现一次）。

跑完请 `git status --short` + grep 关键行验伤（B54 踩过"脚本被打断把变异体留在盘上"）。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python.exe"
TESTS = [
    [PY, "-m", "pytest", "tests/test_frontend_contract.py", "-q", "--no-header",
     "-p", "no:cacheprovider"],
    [PY, "-m", "tests._probe_questionnaire_fill"],
]

PAGE = "frontend/client/index.html"

MUTATIONS = [
    ("返回列表不恢复快照", PAGE,
     "    if(sec&&snapshot)sec.innerHTML=snapshot;\n", ""),
    ("返回列表不作废缓存", PAGE,
     "    WBStore.invalidate('questionnaires');\n    loadQuestionnaireList();",
     "    loadQuestionnaireList();"),
    ("硬编码标记抄回 JS", PAGE,
     "    var snapshot=questionnaireSectionSnapshot();\n"
     "    if(sec&&snapshot)sec.innerHTML=snapshot;",
     "    if(sec)sec.innerHTML='<div class=\"dashboard-grid\" id=\"questionnaireStatsGrid\">"
     "<div class=\"stat-card\"><div class=\"stat-value\" id=\"statQPending\">--</div></div>"
     "</div><div class=\"dashboard-grid\" id=\"questionnaireGrid\"></div>';"),
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
            caught = False
            for command in TESTS:
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode != 0:
                    caught = True
                    break
            print(f"{'抓住' if caught else '漏掉'}  {name}")
            if not caught:
                failures.append(name)
        finally:
            Path(path).write_text(original, encoding="utf-8")
finally:
    Path(PAGE).write_text(original, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
