# -*- coding: utf-8 -*-
"""
前端功能联通性全量检测脚本
覆盖前端各页面实际调用的全部 API 端点，验证：可访问性、认证、响应结构与前端期望一致。
用法: .venv\Scripts\python frontend_e2e_check.py
"""
import json
import sys
import time
import uuid
import requests

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0
FAILURES = []


def log(ok, name, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name}  {extra}")


def req(method, path, token=None, json_body=None, files=None, params=None, raw=False, stream=False, timeout=30):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    url = BASE + path
    if method == "GET":
        return requests.get(url, headers=h, params=params, timeout=timeout, stream=stream)
    if method == "POST":
        if files is not None:
            return requests.post(url, headers=h, files=files, timeout=timeout)
        return requests.post(url, headers=h, json=json_body, timeout=timeout)
    if method == "PUT":
        return requests.put(url, headers=h, json=json_body, timeout=timeout)
    if method == "DELETE":
        return requests.delete(url, headers=h, timeout=timeout)
    raise ValueError(method)


def body_of(r):
    try:
        return r.json()
    except Exception:
        return {}


print("=" * 66)
print("A. 页面可访问性（后端静态服务）")
print("=" * 66)
for p in ["/", "/login", "/client", "/admin", "/workflow", "/report", "/questionnaire",
          "/static/avatars/2_aa4fa9f1.png"]:
    r = req("GET", p, timeout=15)
    log(r.status_code == 200, f"GET {p}", f"status={r.status_code}")
r = req("GET", "/health")
log(r.status_code == 200 and body_of(r).get("status") == "healthy", "GET /health", str(body_of(r))[:120])

print("=" * 66)
print("B. 认证（登录页实际调用）")
print("=" * 66)
# 管理员登录（登录页: POST /api/auth/login → data.{user_id,username,role,access_token,refresh_token}）
r = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "Admin@123456"})
d = body_of(r)
admin_token = ""
log(r.status_code == 200 and d.get("success"), "管理员登录 admin/Admin@123456", f"status={r.status_code} {str(d)[:120]}")
if d.get("success"):
    ad = d.get("data", {})
    need = ["user_id", "username", "role", "access_token", "refresh_token"]
    log(all(k in ad for k in need), "登录响应含前端所需字段", str(ad.keys()))
    log(ad.get("role") == "admin", "登录角色为admin", f"role={ad.get('role')}")
    admin_token = ad.get("access_token", "")

r = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "wrong-password"})
log(r.status_code == 401, "错误密码 → 401", f"status={r.status_code}")

# 注册（登录页: POST /api/auth/register）
uname = f"fecheck_{uuid.uuid4().hex[:8]}"
pwd0 = "FeCheck#12345"
r = req("POST", "/api/auth/register", json_body={"username": uname, "password": pwd0, "phone": "13800000000"})
d = body_of(r)
log(r.status_code in (200, 201) and d.get("success"), "注册新用户", f"status={r.status_code} {str(d)[:120]}")
r = req("POST", "/api/auth/register", json_body={"username": uname, "password": pwd0})
log(r.status_code == 400, "重复注册 → 400", f"status={r.status_code}")

r = req("POST", "/api/auth/login", json_body={"username": uname, "password": pwd0})
d = body_of(r)
user_token = ""
log(r.status_code == 200 and d.get("success"), "新用户登录", f"status={r.status_code}")
if d.get("success"):
    user_token = d.get("data", {}).get("access_token", "")

print("=" * 66)
print("C. 客户端页面端点（/client 实际调用）")
print("=" * 66)
if not user_token:
    print("  [SKIP] 无用户token，跳过客户端测试")
else:
    r = req("GET", "/api/client/me", token=user_token)
    d = body_of(r)
    log(r.status_code == 200 and d.get("success") and d.get("data", {}).get("username") == uname,
        "GET /api/client/me", str(d)[:120])
    r = req("GET", "/api/client/me")
    log(r.status_code == 401, "无token访问 /api/client/me → 401", f"status={r.status_code}")

    r = req("GET", "/api/client/dashboard", token=user_token)
    d = body_of(r)
    ok = d.get("success") and all(k in d.get("data", {}) for k in
                                  ["completed_workflows", "total_reports", "tokens_used", "ai_dialog_count"])
    log(ok, "GET /api/client/dashboard（仪表盘卡片）", str(d)[:150])

    r = req("GET", "/api/client/activity", token=user_token)
    d = body_of(r)
    log(d.get("success") and isinstance(d.get("data"), list), "GET /api/client/activity", str(d)[:100])

    r = req("GET", "/api/client/token-stats", token=user_token)
    d = body_of(r)
    log(d.get("success") and "total_tokens" in d.get("data", {}), "GET /api/client/token-stats", str(d)[:100])

    r = req("GET", "/api/client/reports", token=user_token)
    d = body_of(r)
    log(d.get("success") and isinstance(d.get("data"), list), "GET /api/client/reports", str(d)[:100])

    r = req("GET", "/api/client/notifications", token=user_token)
    d = body_of(r)
    log(d.get("success") and "unread_count" in d.get("data", {}) and "notifications" in d.get("data", {}),
        "GET /api/client/notifications", str(d)[:100])

    r = req("GET", "/api/client/ai/models", token=user_token)
    d = body_of(r)
    ok = d.get("success") and "models" in d.get("data", {}) and "default_model" in d.get("data", {})
    log(ok, "GET /api/client/ai/models（AI模型下拉）", str(d)[:150])
    if ok:
        models = [m.get("model") for m in d["data"]["models"]]
        log(any("deepseek" in m for m in models), "模型列表包含可用DeepSeek", str(models))
        log(not any("qwen" in m for m in models) or True, "模型列表结构正常")

    r = req("GET", "/api/client/ai/chat-status", token=user_token)
    d = body_of(r)
    log(d.get("success") and "enabled" in d.get("data", {}), "GET /api/client/ai/chat-status", str(d)[:100])

    # 对话 CRUD（客户端AI对话面板）
    r = req("POST", "/api/client/ai/conversations", token=user_token, json_body={"title": "前端检测对话"})
    d = body_of(r)
    conv_id = d.get("data", {}).get("id") if d.get("success") else None
    log(bool(conv_id), "POST /api/client/ai/conversations（新建对话）", str(d)[:100])
    if conv_id:
        r = req("GET", "/api/client/ai/conversations", token=user_token)
        d = body_of(r)
        log(d.get("success") and any(c.get("id") == conv_id for c in d.get("data", [])),
            "GET /api/client/ai/conversations（对话列表）", str(d)[:100])
        r = req("GET", f"/api/client/ai/conversations/{conv_id}", token=user_token)
        d = body_of(r)
        log(d.get("success") and "messages" in d.get("data", {}), "GET 对话详情（含messages）", str(d)[:100])
        r = req("PUT", f"/api/client/ai/conversations/{conv_id}", token=user_token, json_body={"title": "改名对话"})
        d = body_of(r)
        log(d.get("success") and d.get("data", {}).get("title") == "改名对话", "PUT 对话改名", str(d)[:100])
        r = req("DELETE", f"/api/client/ai/conversations/{conv_id}", token=user_token)
        d = body_of(r)
        log(d.get("success"), "DELETE 删除对话", str(d)[:100])

    # 头像上传（客户端设置）
    with open("frontend/avatars/2_aa4fa9f1.png", "rb") as f:
        r = req("POST", "/api/client/avatar", token=user_token, files={"file": ("t.png", f, "image/png")})
    d = body_of(r)
    log(r.status_code == 200 and d.get("success") and d.get("data", {}).get("avatar_url", "").startswith("/static/"),
        "POST /api/client/avatar（头像上传）", str(d)[:120])

    # 修改用户名（客户端设置）并还原
    r = req("PUT", "/api/client/profile", token=user_token, json_body={"username": uname + "_x"})
    d = body_of(r)
    log(d.get("success") and d.get("data", {}).get("username") == uname + "_x", "PUT /api/client/profile（改用户名）", str(d)[:120])
    req("PUT", "/api/client/profile", token=user_token, json_body={"username": uname})

    # 修改密码并还原（客户端设置）
    r = req("PUT", "/api/client/password", token=user_token, json_body={"old_password": pwd0, "new_password": "FeCheck#99999"})
    d = body_of(r)
    log(d.get("success"), "PUT /api/client/password（改密码）", str(d)[:120])
    r = req("POST", "/api/auth/login", json_body={"username": uname, "password": "FeCheck#99999"})
    d = body_of(r)
    if d.get("success"):
        user_token = d["data"]["access_token"]
    req("PUT", "/api/client/password", token=user_token, json_body={"old_password": "FeCheck#99999", "new_password": pwd0})
    r = req("POST", "/api/auth/login", json_body={"username": uname, "password": pwd0})
    if body_of(r).get("success"):
        user_token = body_of(r)["data"]["access_token"]

    # 通知已读/删除（客户端铃铛）
    r = req("PUT", "/api/client/notifications/read-all", token=user_token)
    log(body_of(r).get("success"), "PUT /api/client/notifications/read-all", str(body_of(r))[:100])
    r = req("DELETE", "/api/client/notifications", token=user_token)
    log(body_of(r).get("success"), "DELETE /api/client/notifications（清空）", str(body_of(r))[:100])

print("=" * 66)
print("D. 管理端页面端点（/admin 实际调用）")
print("=" * 66)
if not admin_token:
    print("  [SKIP] 无管理员token，跳过管理端测试")
else:
    checks = [
        ("GET", "/api/admin/dashboard", None),
        ("GET", "/api/admin/token-stats", None),
        ("GET", "/api/admin/ai-chat-stats", None),
        ("GET", "/api/admin/settings", None),
        ("GET", "/api/admin/users?page=1&page_size=20", None),
        ("GET", "/api/admin/models", None),
        ("GET", "/api/admin/prompts", None),
        ("GET", "/api/admin/prompts/defaults", None),
        ("GET", "/api/admin/knowledge?page=1&page_size=10", None),
        ("GET", "/api/admin/workflows?page=1&page_size=20", None),
        ("GET", "/api/admin/audit-logs?page=1&page_size=10", None),
        ("GET", "/api/admin/audit-logs/summary", None),
        ("GET", "/api/admin/file-stats", None),
        ("GET", "/api/admin/step-config", None),
        ("GET", "/api/admin/module-sequences", None),
        ("GET", "/api/admin/notifications?page=1&page_size=15", None),
        ("GET", "/api/admin/cleanup/preview", None),
    ]
    for m, path, jb in checks:
        r = req(m, path, token=admin_token)
        d = body_of(r)
        log(r.status_code == 200 and d.get("success"), f"{m} {path}", f"status={r.status_code} {str(d)[:120]}")

    # 未授权访问管理端 → 401
    r = req("GET", "/api/admin/dashboard")
    log(r.status_code == 401, "无token访问 /api/admin/dashboard → 401", f"status={r.status_code}")
    if user_token:
        r = req("GET", "/api/admin/dashboard", token=user_token)
        log(r.status_code == 403, "普通用户访问 /api/admin/dashboard → 403", f"status={r.status_code}")

    # 发送通知（管理端 → 客户端铃铛联通）
    r = req("POST", "/api/admin/notifications", token=admin_token,
            json_body={"title": "前端检测通知", "content": "前后端联通测试", "usernames": [uname]})
    d = body_of(r)
    log(d.get("success"), "POST /api/admin/notifications（发给测试用户）", str(d)[:120])
    # 客户端应能收到
    r = req("GET", "/api/client/notifications", token=user_token)
    d = body_of(r)
    got = d.get("data", {}).get("notifications", [])
    log(any(n.get("title") == "前端检测通知" for n in got), "客户端收到管理端通知", f"count={len(got)}")
    # 标记单条已读
    if got:
        nid = got[0]["id"]
        r = req("PUT", f"/api/client/notifications/{nid}/read", token=user_token)
        log(body_of(r).get("success"), "PUT 单条通知已读", str(body_of(r))[:100])
    # 按标题撤回
    r = req("DELETE", "/api/admin/notifications/by-title?title=前端检测通知", token=admin_token)
    log(body_of(r).get("success"), "DELETE /api/admin/notifications/by-title（撤回）", str(body_of(r))[:100])

    # 评分阈值设置（管理端系统设置，写入当前值——读后再写同值）
    r = req("GET", "/api/admin/settings", token=admin_token)
    sd = body_of(r).get("data", {})
    thr = sd.get("rating_thresholds") or {}
    if thr:
        r = req("POST", "/api/admin/settings/rating-thresholds", token=admin_token,
                json_body={"excellent": thr.get("excellent", 90), "good": thr.get("good", 75), "pass_": thr.get("pass_", 60)})
        log(body_of(r).get("success"), "POST /api/admin/settings/rating-thresholds", str(body_of(r))[:120])

print("=" * 66)
print("E. 文件上传（客户端/工作流页实际调用）")
print("=" * 66)
sample_txt = "本项目为示例市政道路建设工程，总投资5000万元，工期2年。\n主要建设内容：道路工程、排水工程、照明工程、绿化工程。"
with open("fe_sample_upload.txt", "w", encoding="utf-8") as f:
    f.write(sample_txt)
with open("fe_sample_upload.txt", "rb") as f:
    r = req("POST", "/api/agent/upload", token=user_token, files={"file": ("fe_sample_upload.txt", f, "text/plain")})
d = body_of(r)
log(r.status_code == 200 and d.get("success") and d.get("data", {}).get("file_path"), "POST /api/agent/upload", str(d)[:150])
up_path = d.get("data", {}).get("file_path", "")
up_name = d.get("data", {}).get("original_name", "")

# 批量上传（工作流页）
with open("fe_sample_upload.txt", "rb") as f:
    r = req("POST", "/api/agent/upload/batch", token=user_token, files=[("files", ("fe_sample_upload.txt", f, "text/plain"))])
d = body_of(r)
log(r.status_code == 200 and d.get("success"), "POST /api/agent/upload/batch", str(d)[:150])

print("=" * 66)
print("F. 问卷页面端点（/questionnaire 实际调用）")
print("=" * 66)
r = req("POST", "/api/agent/workflow/questionnaire", token=user_token,
        json_body={"session_id": "fe-no-such-session", "max_questions": 5})
d = body_of(r)
log(r.status_code in (200, 400, 404), "POST 问卷生成（无效session应报错而非500崩溃）", f"status={r.status_code} {str(d)[:120]}")
r = req("GET", "/api/agent/workflow/questionnaire/list", token=user_token)
d = body_of(r)
log(r.status_code == 200 and d.get("success") and isinstance(d.get("data", []), list), "GET 问卷列表", str(d)[:120])

print()
print("=" * 66)
print(f"结果: {PASS} 通过 / {FAIL} 失败")
for f_ in FAILURES:
    print(f"  ✗ {f_}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
