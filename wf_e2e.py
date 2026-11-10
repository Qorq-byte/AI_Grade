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
    # E2E 专用管理员：admin 已被首登强制改密打标，管理端接口对它 403（见 _e2e_auth.py）
    from _e2e_auth import ensure_e2e_admin
    _user, _pwd = ensure_e2e_admin(BASE)
    r = req("POST", "/api/auth/login", json_body={"username": _user, "password": _pwd})
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
    start = time.perf_counter()
    r = req("POST", "/api/agent/workflow/start-stream", token=token, json_body=body, timeout=None, stream=True)
    if r.status_code != 200:
        log(f"FAIL start-stream 状态码 {r.status_code}: {r.text[:300]}")
        return 1
    session_id = ""
    events = []
    completed = False
    errored = False
    buf = ""
    estimated_tokens = 0
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
                    estimated_tokens = ev.get("tokens_used", 0)
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
        if time.perf_counter() - start > 2400:
            log("超时(40分钟)，中止")
            break
    elapsed = time.perf_counter() - start
    log(f"SSE流结束: {elapsed:.3f}s completed={completed} errored={errored} session={session_id}")

    results = {"session_id": session_id, "project_name": project_name, "completed": completed,
               "elapsed_s": round(elapsed, 3), "estimated_output_tokens": estimated_tokens, "checks": []}

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
    # 上一步刻意存了一份 30 字短桩报告，导出闸门会以「报告过短」拦下（批次 26）。
    # 这里验的是导出→下载链路本身，所以显式 force；闸门由
    # tests/test_consistency_export_gate.py 单独覆盖。
    r = req("POST", "/api/agent/workflow/export", token=token,
            json_body={"session_id": session_id, "format": "word", "project_name": project_name,
                       "force": True})
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
            json_body={"session_id": session_id, "format": "pdf", "project_name": project_name,
                       "force": True})
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
        answers.append({
            "question_id": q.get("question_id") or q["id"],
            "selected_level_index": 4,
        })
    r = req("POST", "/api/agent/workflow/questionnaire/submit", token=token,
            json_body={"session_id": session_id, "answers": answers})
    d = bof(r)
    check("POST 问卷提交", d.get("success") or r.status_code == 200, f"status={r.status_code} {str(d)[:150]}")

    # 11. 问卷答案查询
    r = req("GET", f"/api/agent/workflow/{session_id}/questionnaire/answers", token=token)
    d = bof(r)
    check("GET 问卷答案", r.status_code == 200, f"status={r.status_code} {str(d)[:100]}")

    # 12. 评分相关（工作流页各按钮）
    r = req("GET", f"/api/agent/workflow/{session_id}/score-summary", token=token)
    d = bof(r)
    check("GET score-summary", d.get("success"), str(d)[:150])

    # 12.1 步骤间一致性检查：14 步跑完后必须已经落进状态，且能按需重跑
    r = req("GET", f"/api/agent/workflow/{session_id}/status", token=token)
    d = bof(r)
    report = (d.get("data") or {}).get("consistency") or {}
    check("GET status 带 steps一致性检查结果",
          bool(report.get("checked")), f"codes={[i.get('code') for i in report.get('issues', [])]}")
    check("一致性检查覆盖六类检查项",
          report.get("checked") == ["indicator_allocation", "score_summary",
                                    "report_text", "chapters",
                                    "problem_suggestion_coverage", "steps_and_coverage"],
          str(report.get("checked")))
    log(f"一致性结论：错误 {report.get('error_count')} 项 / 提示 {report.get('warning_count')} 项")

    r = req("POST", f"/api/agent/workflow/{session_id}/consistency-check", token=token, json_body={})
    d = bof(r)
    rerun = (d.get("data") or {}).get("consistency") or {}
    check("POST 重跑一致性检查", d.get("success") and bool(rerun.get("checked")), str(d)[:150])
    check("重跑结论与自动检查一致",
          len(rerun.get("issues", [])) == len(report.get("issues", [])),
          f"{len(rerun.get('issues', []))} vs {len(report.get('issues', []))}")

    r = req("GET", f"/api/agent/workflow/{session_id}/indicators-for-scoring", token=token)
    d = bof(r)
    check("GET indicators-for-scoring", d.get("success"), str(d)[:150])

    # 12.2 步骤7评分落地台账：区分「模型判定资料不足」与「系统把评分丢了」
    r = req("GET", f"/api/agent/workflow/{session_id}/status", token=token)
    d = bof(r)
    dispo = (d.get("data") or {}).get("score_disposition") or {}
    check("GET status 带评分落地台账", bool(dispo.get("total")), str(dispo)[:160])
    counts = dispo.get("counts") or {}
    check("台账分项计数之和等于指标总数",
          sum(counts.values()) == dispo.get("total") and dispo.get("total", 0) > 0,
          f"{counts} total={dispo.get('total')}")

    r = req("GET", f"/api/agent/workflow/{session_id}/score-summary", token=token)
    d = bof(r)
    summary = d.get("data") or {}
    check("GET score-summary 带同一份台账",
          (summary.get("disposition") or {}).get("total") == dispo.get("total"),
          f"summary={((summary.get('disposition') or {}).get('total'))} status={dispo.get('total')}")
    check("台账的已落地分值与 score-summary 合计一致",
          abs(float(summary.get("total_score") or 0) - float(dispo.get("accepted_score") or 0)) < 0.01,
          f"summary={summary.get('total_score')} dispo={dispo.get('accepted_score')}")
    check("已落地项数与 missing_count 互补",
          int(counts.get("accepted") or 0) + int(summary.get("missing_count") or 0) == int(dispo.get("total") or 0),
          f"accepted={counts.get('accepted')} missing={summary.get('missing_count')} total={dispo.get('total')}")

    line = dispo.get("summary_line") or ""
    check("台账结论可直接展示",
          line.startswith("步骤 7 评分落地") and f"{counts.get('accepted')}/{dispo.get('total')}" in line,
          line[:160])
    # 模型可能给出体系里不存在的指标 id（实测有 4/6 条），必须单独记录而不是静默丢弃
    check("台账单列体系外指标 ID（可为 0，但字段必须在）",
          isinstance(dispo.get("unmatched"), list), str(type(dispo.get("unmatched"))))
    if dispo.get("unmatched"):
        log(f"体系外指标 ID：{[it.get('indicator_id') for it in dispo['unmatched']]}")
    # 弱定位（引用只落到原文行号）**不得计入总分**：不独立算一遍，
    # 「accepted_score 只累加 accepted」这条约束就没人守着。
    _items = dispo.get("items") or {}
    _expect = round(sum(float(v.get("actual_score") or 0) for v in _items.values()
                        if isinstance(v, dict) and v.get("status") == "accepted"), 2)
    check("已落地分值合计只累加 accepted（弱定位不计分）",
          abs(_expect - float(dispo.get("accepted_score") or 0)) < 0.01,
          f"独立计算={_expect} 台账={dispo.get('accepted_score')} "
          f"弱定位={counts.get('anchor_weak')}")
    system_side = (int(counts.get("anchor_unresolved") or 0) + int(counts.get("score_invalid") or 0)
                   + int(counts.get("reason_missing") or 0))
    log(f"评分落地台账：{line}")
    log(f"系统侧未落地（引用/分值/理由）：{system_side} 项；"
        f"模型判定资料不足：{counts.get('model_pending')} 项；未提及：{counts.get('not_mentioned')} 项")

    r = req("POST", f"/api/agent/workflow/{session_id}/verify-logic", token=token, json_body={})
    d = bof(r)
    check("POST verify-logic", d.get("success"), str(d)[:150])

    r = req("GET", f"/api/agent/workflow/{session_id}/extract-deductions", token=token)
    d = bof(r)
    check("GET extract-deductions", d.get("success"), str(d)[:150])

    r = req("GET", f"/api/agent/workflow/{session_id}/report-order", token=token)
    d = bof(r)
    check("GET report-order", d.get("success"), str(d)[:150])

    r = req("GET", f"/api/agent/workflow/{session_id}/export-style", token=token)
    d = bof(r)
    check("GET export-style", d.get("success") or r.status_code == 200, f"status={r.status_code} {str(d)[:120]}")

    r = req("GET", f"/api/agent/workflow/{session_id}/modules", token=token)
    d = bof(r)
    check("GET modules", d.get("success") or r.status_code == 200, f"status={r.status_code} {str(d)[:120]}")

    # 13. 模板
    r = req("GET", "/api/agent/workflow/templates", token=token)
    d = bof(r)
    check("GET 工作流模板", d.get("success"), str(d)[:150])

    # 14. 报告组装（工作流页 Step14 核心功能）
    r = req("GET", f"/api/agent/workflow/{session_id}/report-order", token=token)
    d = bof(r)
    sections = d.get("data") or []
    ok = d.get("success") and isinstance(sections, list) and len(sections) > 0
    check("GET report-order（章节列表）", ok, f"n={len(sections)} sections")
    if ok:
        has_content_count = sum(1 for s in sections if s.get("has_content"))
        check("report-order has_content 标记正确", has_content_count >= 6, f"has_content={has_content_count}")
        # 保存自定义顺序
        reordered = list(reversed(sections))
        r = req("POST", "/api/agent/workflow/report-order", token=token,
                json_body={"session_id": session_id, "sections": reordered})
        check("POST report-order（保存章节顺序）", bof(r).get("success"), str(bof(r))[:120])

    r = req("POST", "/api/agent/workflow/assemble-report", token=token,
            json_body={"session_id": session_id, "show_title": True,
                       "include_sections": [s.get("key") for s in sections]})
    d = bof(r)
    rep = (d.get("data") or {}).get("report", "")
    sec_cnt = (d.get("data") or {}).get("section_count", 0)
    check("POST assemble-report（组装报告有内容）", d.get("success") and len(rep) > 500 and sec_cnt > 0,
          f"len={len(rep)} sections={sec_cnt}")

    # 15. 格式校对（LLM调用，约30-60秒）
    r = req("POST", f"/api/agent/workflow/{session_id}/format-check", token=token, json_body={}, timeout=180)
    d = bof(r)
    check("POST format-check（格式校对）", d.get("success") and bool((d.get("data") or {}).get("check_result")),
          f"result_len={len((d.get('data') or {}).get('check_result', ''))}")

    # 16. 导出样式
    r = req("GET", f"/api/agent/workflow/{session_id}/export-style", token=token)
    d = bof(r)
    check("GET export-style", d.get("success") or r.status_code == 200, f"status={r.status_code} {str(d)[:100]}")
    r = req("POST", "/api/agent/workflow/export-style", token=token,
            json_body={"session_id": session_id, "font_family": "SimSun", "font_size": 12,
                       "line_spacing": 1.5, "page_margin": 2.5})
    d = bof(r)
    check("POST export-style（保存样式）", d.get("success"), str(d)[:120])

    # 17. 自定义模块（Step12）
    r = req("POST", "/api/agent/workflow/add-custom-module", token=token,
            json_body={"session_id": session_id, "module_key": "custom_test_module",
                       "module_name": "项目合规性分析", "content": "测试自定义模块内容"})
    d = bof(r)
    check("POST add-custom-module", d.get("success"), str(d)[:120])
    r = req("GET", f"/api/agent/workflow/{session_id}/modules", token=token)
    d = bof(r)
    mods = (d.get("data") or {}).get("modules", [])
    check("GET modules 包含自定义模块",
          any(isinstance(m, dict) and m.get("key") == "custom_test_module" for m in mods),
          f"n={len(mods)}")

    # 18. 管理端工作流监控包含本会话
    r = req("GET", "/api/client/reports", token=token)
    d = bof(r)
    rep_list = d.get("data") or []
    check("客户端报告列表包含本会话", any((x.get("session_id") == session_id) for x in rep_list if isinstance(x, dict)),
          f"n={len(rep_list)}")

    # 15. 管理端工作流监控应包含本会话（验证WorkflowSession持久化）
    r = req("GET", "/api/admin/workflows?page=1&page_size=50", token=token)
    d = bof(r)
    wf_list = (d.get("data") or {}).get("workflows", [])
    hit = [w for w in wf_list if w.get("session_id") == session_id]
    check("管理端工作流监控包含本会话", len(hit) > 0, f"n={len(wf_list)}")
    if hit:
        check("会话状态completed且current_step=14",
              hit[0].get("status") == "completed" and hit[0].get("current_step") == 14,
              f"status={hit[0].get('status')} step={hit[0].get('current_step')} user={hit[0].get('user_id')}")

    n_fail = sum(1 for c in results["checks"] if not c["ok"])
    results["fail_count"] = n_fail
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"工作流E2E完成: {len(results['checks'])} 项检查, 失败 {n_fail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        log(f"异常: {e}")
        import traceback
        traceback.print_exc()
        code = 1
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    sys.exit(code)
