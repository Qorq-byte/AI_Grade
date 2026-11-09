"""批次 22 的变异测试：报告正文去掉裸 JSON。

覆盖四件事：拆分判据（合法性、围栏、空行）、步骤 7 落盘、三条组装路径、
人工修改后的重算与备份白名单。每个变异体先离线 compile()，避免「语法错误被当成抓到」。

用法：NO_PROXY='*' .venv/Scripts/python.exe tests/_mutate_report_json.py
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = "tests/test_report_no_raw_json.py"

AGENT = "backend/agents/performance_agent.py"
STRUCT = "backend/agents/tools/structured_output.py"
ROUTES = "backend/routes/agent_routes.py"

# (说明, 文件, 原文, 替换)
MUTATIONS = [
    ("拆 JSON 前不再校验是否合法（半截也剥）",
     STRUCT,
     """    try:
        json.loads(blob)
    except (ValueError, TypeError):
        return "", raw.strip()""",
     """    if not blob:
        return "", raw.strip()"""),
    ("不剥代码块围栏（正文里留下 ```）",
     STRUCT,
     """    fence_open = re.search(r"```(?:json)?\\s*$", raw[:start])
    if fence_open:
        start = fence_open.start()
    fence_close = re.match(r"\\s*```", raw[end:])
    if fence_close:
        end += fence_close.end()""",
     """    fence_open = None
    fence_close = None"""),
    ("不压多余空行（正文带着一串空行进报告）",
     STRUCT,
     """    prose = re.sub(r"\\n{3,}", "\\n\\n", raw[:start] + raw[end:]).strip()""",
     """    prose = (raw[:start] + raw[end:]).strip()"""),
    ("返回值忘了去掉围栏（JSON 带着 ``` 落盘）",
     STRUCT,
     """    return blob.strip(), prose""",
     """    return raw.strip(), prose"""),
    ("节点不拆，prose 直接等于原文",
     AGENT,
     """        scores_json, prose = structured_output.split_step7_output(content)
        state["step7_scores_json"] = scores_json
        state["step7_prose"] = prose""",
     """        state["step7_scores_json"] = ""
        state["step7_prose"] = content"""),
    ("组装第四章退回原始全文（报告里又是裸 JSON）",
     AGENT,
     """        if key == "indicator_analysis":
            prose = (state.get("step7_prose") or "").strip()
            if prose:
                return prose
        return state.get(key)""",
     """        return state.get(key)"""),
    ("没有正文时不清空章节（回退被删）",
     AGENT,
     """        if key == "indicator_analysis":
            prose = (state.get("step7_prose") or "").strip()
            if prose:
                return prose""",
     """        if key == "indicator_analysis":
            prose = (state.get("step7_prose") or "").strip()
            return prose"""),
    ("路由组装不用正文（values 路径）",
     ROUTES,
     """        ("四、指标得分及分析", _chapter_from_values(values, "indicator_analysis")),""",
     """        ("四、指标得分及分析", values.get("indicator_analysis")),"""),
    ("备份组装忽略 step7_prose",
     ROUTES,
     """        if step_num == "7":
            # 第四章只拼正文：不带这一步，报告里就会出现整段评分 JSON（批次 22）
            content = (backup.get("step7_prose") or "").strip() or content""",
     """        if False:
            content = content"""),
    ("人工改完步骤 7 不重算正文（报告还是旧内容）",
     ROUTES,
     """        if step == 7:
            # 报告第四章取的是 step7_prose：人工改完必须跟着重算，
            # 否则报告里还是修改前那段正文（批次 22）
            scores_json, prose = structured_output.split_step7_output(content)
            updates["step7_scores_json"] = scores_json
            updates["step7_prose"] = prose""",
     """        if False:
            pass"""),
    ("备份白名单漏掉拆分后的两个字段",
     AGENT,
     '''                "step7_scores_json", "step7_prose",''',
     '''                "step7_scores_json_", "step7_prose_",'''),
]

paths = sorted({path for _, path, _, _ in MUTATIONS})
originals = {path: Path(path).read_text(encoding="utf-8") for path in paths}
failures = []


def compile_ok(path):
    try:
        compile(Path(path).read_text(encoding="utf-8"), path, "exec")
        return True
    except SyntaxError as exc:
        print(f"  变异体语法错误，跳过：{exc}")
        return False


try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(path).write_text(originals[path].replace(old, new, 1), encoding="utf-8")
        if not compile_ok(path):
            Path(path).write_text(originals[path], encoding="utf-8")
            continue
        result = subprocess.run(
            [PY, "-m", "pytest", *TARGET_TESTS.split(), "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True)
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
