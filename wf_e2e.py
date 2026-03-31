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

