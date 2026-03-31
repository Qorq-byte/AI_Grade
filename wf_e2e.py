# -*- coding: utf-8 -*-
"""
工作流端到端测试：上传 → start-stream(SSE) → 14步完成 → 导出/问卷/评分/报告页端点
用法: python wf_e2e.py
"""
import json
import sys
import time
import uuid

# Windows 控制台默认GBK，强制UTF-8输出避免emoji打印崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

BASE = "http://127.0.0.1:8000"
RESULT_FILE = "wf_e2e_result.json"
LOG_FILE = "wf_e2e_log.txt"

log_lines = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log_lines.append(line)


def req(method, path, token=None, json_body=None, files=None, timeout=60, stream=False):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    url = BASE + path
    if method == "GET":
        return requests.get(url, headers=h, timeout=timeout, stream=stream)
    if method == "POST":
        if files is not None:
            return requests.post(url, headers=h, files=files, timeout=timeout)
        return requests.post(url, headers=h, json=json_body, timeout=timeout)
    if method == "PUT":
        return requests.put(url, headers=h, json=json_body, timeout=timeout)
    if method == "DELETE":
        return requests.delete(url, headers=h, timeout=timeout)
    raise ValueError(method)


def bof(r):
    try:
        return r.json()
    except Exception:
        return {}


def main():
    r = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "Admin@123456"})
    d = bof(r)
    token = d.get("data", {}).get("access_token", "") if d.get("success") else ""
    if not token:
        log(f"FAIL 管理员登录失败: {r.status_code} {str(d)[:200]}")
        return 1
    log("管理员登录成功")

    # 1. 上传示例文件
    sample = ("云海市政道路建设工程项目\n一、项目概况\n本项目为城市主干道改造工程，总投资5000万元，"
              "建设周期2年（2024年1月至2025年12月），由市住建局牵头实施。\n"
              "二、建设内容\n1. 道路工程：改造主路3.2公里、辅路1.8公里；\n2. 排水工程：新建雨水管网4.5公里；\n"
              "3. 照明工程：安装路灯220盏；\n4. 绿化工程：种植行道树1800株。\n"
              "三、绩效目标\n目标1：2025年底前完成全部工程并通车，通车率达到100%；\n"
              "目标2：项目资金使用规范，资金到位率100%，支出进度不低于90%；\n"
              "目标3：群众满意度不低于85%；\n"
              "四、资金安排\n财政拨款4000万元，专项债1000万元。截至2025年6月已支出3200万元，支出进度64%。\n"
              "五、存在问题\n部分路段施工进度滞后约1个月；个别标段监理记录不完整。")
    with open("fe_wf_sample.txt", "w", encoding="utf-8") as f:
        f.write(sample)
    with open("fe_wf_sample.txt", "rb") as f:
        r = req("POST", "/api/agent/upload", token=token, files={"file": ("fe_wf_sample.txt", f, "text/plain")})
    d = bof(r)
    if not (d.get("success") and d.get("data", {}).get("file_path")):
        log(f"FAIL 上传失败: {str(d)[:200]}")
        return 1
    file_path = d["data"]["file_path"]
    log(f"上传成功: {file_path}")

    # 2. 启动工作流（SSE）
    project_name = f"前端联通性测试项目-{uuid.uuid4().hex[:6]}"
    body = {
        "project_name": project_name,
        "file_paths": [file_path],
        "model_name": "deepseek-chat",
        "comparison_mode": "single",
        "temperature_profile": "normal",
        "top_p": 0.9,
    }
    r = req("POST", "/api/agent/workflow/start-stream", token=token, json_body=body, timeout=None, stream=True)
    if r.status_code != 200:
        log(f"FAIL start-stream 状态码 {r.status_code}: {r.text[:300]}")
        return 1
    session_id = ""
    events = []
    completed = False
    errored = False
    buf = ""
    start = time.time()
    log("开始读取SSE流...")
    for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
        if not chunk:
            continue
        buf += chunk
        while "\n\n" in buf:
            raw, buf = buf.split("\n\n", 1)
            for ln in raw.split("\n"):
                ln = ln.strip()
                if not ln.startswith("data:"):
                    continue
                try:
                    ev = json.loads(ln[5:].strip())
                except Exception:
                    continue
                etype = ev.get("event")
                events.append(ev)
                if etype == "step_start":
                    log(f"  步骤{ev.get('step_number')} 开始: {ev.get('step_name')}")
                elif etype == "step_complete":
                    c = ev.get("content") or ""
                    log(f"  步骤{ev.get('step_number')} 完成: {ev.get('step_name')} (内容{len(c)}字)")
                elif etype == "token_update":
                    pass
                elif etype == "questionnaire_ready":
                    log(f"  问卷可生成事件已触发")
                elif etype == "complete":
                    session_id = ev.get("session_id", session_id)
                    completed = True
                    log(f"  工作流完成! 步骤数={ev.get('completed_steps')} tokens={ev.get('tokens_used')}")
                elif etype == "error":
                    errored = True
                    log(f"  错误事件: {ev.get('message', '')[:300]}")
                if not session_id:
                    session_id = ev.get("session_id") or ""
            if completed or errored:
                break
        if completed or errored:
            break
        if time.time() - start > 2400:
            log("超时(40分钟)，中止")
            break
    elapsed = time.time() - start
    log(f"SSE流结束: {elapsed:.0f}s completed={completed} errored={errored} session={session_id}")

    results = {"session_id": session_id, "project_name": project_name, "completed": completed,
               "elapsed_s": round(elapsed), "checks": []}

    def check(name, ok, extra=""):
        results["checks"].append({"name": name, "ok": bool(ok), "extra": extra[:200]})
        log(f"  [{'PASS' if ok else 'FAIL'}] {name} {extra[:150]}")

    check("SSE流完成事件", completed)
    check("SSE无错误事件", not errored)
    if not session_id:
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        return 1

    # 3. 状态端点（报告页/工作流页）
    r = req("GET", f"/api/agent/workflow/{session_id}/status", token=token)
    d = bof(r)
    ok = d.get("success") and d.get("data")
    check("GET workflow/status", ok, str(d)[:150])
    if ok:
        data = d["data"]
        summaries = data.get("step_summaries") or []
        check("status含step_summaries", isinstance(summaries, list) and len(summaries) > 0, f"n={len(summaries)}")
        check("status状态completed", data.get("status") == "completed", f"status={data.get('status')}")

    # 4. 报告获取（报告页）
    r = req("GET", f"/api/agent/workflow/{session_id}/report", token=token)
    d = bof(r)
    ok = d.get("success") and d.get("data", {}).get("content")
    check("GET workflow/report", ok, f"content_len={len(d.get('data', {}).get('content', ''))}")

    # 5. 保存报告（报告页“保存”按钮）
    r = req("POST", "/api/agent/workflow/save-report", token=token,
            json_body={"session_id": session_id, "content": "【测试】报告内容已由前端保存。\n\n一、概述\n这是联通性测试。"})
    d = bof(r)
    check("POST workflow/save-report", d.get("success"), str(d)[:150])

    # 6. 导出Word（客户端导出按钮 → /api/agent/download）
    r = req("POST", "/api/agent/workflow/export", token=token,
            json_body={"session_id": session_id, "format": "word", "project_name": project_name})
    d = bof(r)
    ok = d.get("success") and d.get("data", {}).get("file_name")
    check("POST export word", ok, str(d)[:200])
    if ok:
        fn = d["data"]["file_name"]
        r = req("GET", f"/api/agent/download/{fn}", token=token)
        check("GET download word文件", r.status_code == 200 and len(r.content) > 1000,
              f"status={r.status_code} bytes={len(r.content)}")

    # 7. 导出PDF（应返回真实 .pdf 文件）
    r = req("POST", "/api/agent/workflow/export", token=token,
            json_body={"session_id": session_id, "format": "pdf", "project_name": project_name})
    d = bof(r)
    pdf_name = d.get("data", {}).get("file_name", "") if d.get("success") else ""
    ok = bool(pdf_name) and pdf_name.lower().endswith(".pdf")
    check("POST export pdf (真实PDF)", ok, f"file={pdf_name}")
    if ok:
        r = req("GET", f"/api/agent/download/{pdf_name}", token=token)
        is_pdf = r.status_code == 200 and r.content[:5] == b"%PDF-"
        check("GET download pdf文件为真实PDF", is_pdf,
              f"status={r.status_code} bytes={len(r.content)} head={r.content[:8]!r}")

    # 8. 问卷生成（客户端/问卷页）
    r = req("POST", "/api/agent/workflow/questionnaire", token=token,
            json_body={"session_id": session_id, "max_questions": 10})
    d = bof(r)
    ok = d.get("success") and (d.get("data") or {}).get("questions") or (d.get("data") or {}).get("questionnaire")
    qdata = d.get("data") or {}
    questions = qdata.get("questions") or qdata.get("questionnaire") or []
    check("POST 问卷生成", ok, f"n={len(questions) if isinstance(questions, list) else '?'}")
    qlist = questions if isinstance(questions, list) else []

    # 9. 问卷列表（客户端问卷Tab）
    r = req("GET", "/api/agent/workflow/questionnaire/list", token=token)
    d = bof(r)
    lst = d.get("data") or []
    check("GET 问卷列表含本session", any((x.get("session_id") == session_id) for x in lst if isinstance(x, dict)),
          f"n={len(lst)}")

    # 10. 问卷提交（问卷页submit）
    answers = []
    for i, q in enumerate(qlist[:5]):
        qid = q.get("id") if isinstance(q, dict) else f"q{i}"
        answers.append({"question_id": str(qid), "answer": 4})
    r = req("POST", "/api/agent/workflow/questionnaire/submit", token=token,
            json_body={"session_id": session_id, "answers": answers})
    d = bof(r)
    check("POST 问卷提交", d.get("success") or r.status_code == 200, f"status={r.status_code} {str(d)[:150]}")

    # 11. 问卷答案查询
    r = req("GET", f"/api/agent/workflow/{session_id}/questionnaire/answers", token=token)
    d = bof(r)
    check("GET 问卷答案", r.status_code == 200, f"status={r.status_code} {str(d)[:100]}")

