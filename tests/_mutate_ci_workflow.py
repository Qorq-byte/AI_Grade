"""变异检验：改坏 CI 配置，确认 tests/test_ci_workflow.py 真的会失败。

这条脚本守的不是代码逻辑，而是**决定**：哪些检查在 CI 里、哪些必须留在人手里。
四条变异对应四个决定的失效方式。
"""
import subprocess
import sys

PY = ".venv/Scripts/python.exe"
TARGET = [PY, "-m", "pytest", "tests/test_ci_workflow.py", "-q", "--no-header",
          "-p", "no:cacheprovider"]
WORKFLOW = ".github/workflows/ci.yml"

MUTATIONS = [
    ("少了 pytest 这一步", "      - name: Python 单测\n        run: python -m pytest tests -q --ignore=tests/rag_eval\n",
     "      - name: Python 单测\n        run: python -c \"print('skipped')\"\n"),
    ("把需要真服务的 wf_e2e 塞进 CI", "      - name: 前端共享层单测",
     "      - name: 真实工作流\n        run: python wf_e2e.py\n\n      - name: 前端共享层单测"),
    ("MySQL 指回 3306（测试会去连真库）", '      MYSQL_PORT: "1"', '      MYSQL_PORT: "3306"'),
    ("装完整 requirements（拉进 2 GB 的 torch）", "-r requirements-dev.txt", "-r requirements.txt"),
]

original = open(WORKFLOW, encoding="utf-8").read()
failures = []
try:
    for name, old, new in MUTATIONS:
        if old not in original:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        open(WORKFLOW, "w", encoding="utf-8").write(original.replace(old, new, 1))
        result = subprocess.run(TARGET, capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "fail" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        open(WORKFLOW, "w", encoding="utf-8").write(original)
finally:
    open(WORKFLOW, "w", encoding="utf-8").write(original)

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
