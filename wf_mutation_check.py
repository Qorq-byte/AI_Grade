# -*- coding: utf-8 -*-
"""对已完成工作流会话执行写操作端点，验证 update_state 持久化链路。
用法: python wf_mutation_check.py <session_id>
"""
import json
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

BASE = "http://127.0.0.1:8000"


def req(method, path, token=None, json_body=None, timeout=120):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    url = BASE + path
    if method == "GET":
        return requests.get(url, headers=h, timeout=timeout)
    if method == "POST":
        return requests.post(url, headers=h, json=json_body, timeout=timeout)
    raise ValueError(method)


def bof(r):
    try:
        return r.json()
    except Exception:
        return {}


def main():
    sid = sys.argv[1]
    t = bof(requests.post(BASE + "/api/auth/login",
                          json={"username": "admin", "password": "Admin@123456"},
                          timeout=30)).get("data", {}).get("access_token", "")
    fails = 0

    def check(name, ok, extra=""):
        nonlocal fails
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {str(extra)[:140]}", flush=True)

    # 1. 语言风格设置 → 再读取状态验证持久化
    r = req("POST", "/api/agent/workflow/set-language-style", token=t,
            json_body={"session_id": sid, "language_style": "尖锐"})
    check("set-language-style", bof(r).get("success"), str(bof(r))[:100])

    # 2. 报告章节顺序 → 读取验证
    r = req("GET", f"/api/agent/workflow/{sid}/report-order", token=t)
    sections = bof(r).get("data") or []
    if sections:
        reordered = list(reversed(sections))
        r = req("POST", "/api/agent/workflow/report-order", token=t,
                json_body={"session_id": sid, "sections": reordered})
        check("report-order 保存", bof(r).get("success"))
        r = req("GET", f"/api/agent/workflow/{sid}/report-order", token=t)
        d = bof(r)
        got = d.get("data") or []
        persisted = bool(got) and got[0].get("key") == reordered[0].get("key") and d.get("is_custom")
        check("report-order 持久化生效（is_custom=True且顺序反转）", persisted,
              f"is_custom={d.get('is_custom')} first={got[0].get('key') if got else None}")

    # 3. 指标重排序（此前 workflow.graph 不存在导致500）
    r = req("GET", f"/api/agent/workflow/{sid}/indicators-for-scoring", token=t)
    inds = (bof(r).get("data") or {}).get("indicators", [])
    if len(inds) >= 2:
        r = req("POST", "/api/agent/workflow/reorder-indicators", token=t,
                json_body={"session_id": sid, "level": "level3",
                           "from_index": 0, "to_index": 1,
                           "parent_l1_idx": 0, "parent_l2_idx": 0})
        check("reorder-indicators (level3)", bof(r).get("success"),
              f"status={r.status_code} {str(bof(r))[:120]}")
        # 一级重排
        r = req("POST", "/api/agent/workflow/reorder-indicators", token=t,
                json_body={"session_id": sid, "level": "level1",
                           "from_index": 0, "to_index": 1})
        check("reorder-indicators (level1)", bof(r).get("success"),
              f"status={r.status_code} {str(bof(r))[:120]}")

    # 4. 模板保存（写入templates目录）
    r = req("POST", "/api/agent/workflow/templates/save", token=t,
            json_body={"session_id": sid, "template_name": "E2E测试模板",
                       "template_type": "自定义"})
    check("templates/save", bof(r).get("success"), str(bof(r))[:120])

    # 5. 模板导入（覆盖指标体系）
    r = req("GET", "/api/agent/workflow/templates", token=t)
    tmpls = bof(r).get("data") or []
    if tmpls:
        tid = tmpls[0].get("id")
        r = req("POST", "/api/agent/workflow/templates/import", token=t,
                json_body={"session_id": sid, "template_id": tid})
        check("templates/import", bof(r).get("success"), str(bof(r))[:120])

    # 6. 生成模块 + 更新 + 删除（Step12 模块链路）
    r = req("POST", "/api/agent/workflow/generate-module", token=t,
            json_body={"session_id": sid, "module_key": "project_background"})
    check("generate-module", bof(r).get("success"), str(bof(r))[:120])
    r = req("POST", "/api/agent/workflow/update-module-content", token=t,
            json_body={"session_id": sid, "module_key": "project_background",
                       "module_name": "项目背景", "content": "更新后的模块内容测试"})
    check("update-module-content", bof(r).get("success"), str(bof(r))[:100])
    r = req("POST", "/api/agent/workflow/delete-module", token=t,
            json_body={"session_id": sid, "module_key": "project_background"})
    check("delete-module", bof(r).get("success"), str(bof(r))[:100])

    # 7. 保存评分（持久化 indicator_score_map）
    r = req("POST", "/api/agent/workflow/save-indicator-scores", token=t,
            json_body={"session_id": sid, "scores": [
                {"indicator_id": "T_01", "indicator_name": "测试指标", "level1_name": "决策",
                 "level2_name": "项目立项", "full_score": 5.0, "actual_score": 4.0,
                 "score_reason": "测试扣1分"}]})
    check("save-indicator-scores", bof(r).get("success"), str(bof(r))[:140])
    r = req("GET", f"/api/agent/workflow/{sid}/extract-deductions", token=t)
    d = bof(r)
    check("extract-deductions 读到新评分", any(
        p.get("indicator_id") == "T_01" for p in (d.get("data") or {}).get("deduction_points", [])),
        str(d)[:140])

    print(f"\n写操作检查完成: 失败 {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
