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
import base64
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
for p in ["/", "/login", "/client", "/admin", "/workflow", "/report", "/questionnaire"]:
    r = req("GET", p, timeout=15)
    log(r.status_code == 200, f"GET {p}", f"status={r.status_code}")
r = req("GET", "/health")
log(r.status_code == 200 and body_of(r).get("status") == "healthy", "GET /health", str(body_of(r))[:120])

print("=" * 66)
print("B. 认证（登录页实际调用）")
print("=" * 66)
# 管理员登录（登录页: POST /api/auth/login → data.{user_id,username,role,access_token,refresh_token}）
# 用 E2E 专用管理员：admin 已被首登强制改密打标，管理端接口对它一律 403
from _e2e_auth import ensure_e2e_admin
_admin_user, _admin_pwd = ensure_e2e_admin(BASE)
r = req("POST", "/api/auth/login", json_body={"username": _admin_user, "password": _admin_pwd})
d = body_of(r)
admin_token = ""
log(r.status_code == 200 and d.get("success"), f"管理员登录 {_admin_user}", f"status={r.status_code} {str(d)[:120]}")
if d.get("success"):
    ad = d.get("data", {})
    need = ["user_id", "username", "role", "access_token", "refresh_token"]
    log(all(k in ad for k in need), "登录响应含前端所需字段", str(ad.keys()))
    log(ad.get("role") == "admin", "登录角色为admin", f"role={ad.get('role')}")
    admin_token = ad.get("access_token", "")

r = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "wrong-password"})
log(r.status_code == 401, "错误密码 → 401", f"status={r.status_code}")

# 安全回归（批次 24）：初始口令登录必须带 must_change_password=true，
# 且管理端接口对它返回 403 —— 这正是"默认口令不能直接用"的护栏。
r = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "Admin@123456"})
_d = body_of(r)
if r.status_code == 200 and _d.get("success"):
    _adm = _d.get("data", {})
    log(_adm.get("must_change_password") is True, "初始口令登录 → 标记必须改密",
        f"must_change_password={_adm.get('must_change_password')}")
    _r2 = req("GET", "/api/admin/dashboard", token=_adm.get("access_token", ""))
    log(_r2.status_code == 403, "未改密的管理端请求 → 403",
        f"status={_r2.status_code}")
    log(_r2.headers.get("X-Password-Change-Required") == "1",
        "403 带 X-Password-Change-Required 标记（前端据此跳改密而非登出）",
        f"header={_r2.headers.get('X-Password-Change-Required')}")
else:
    log(True, "初始口令已不可登录（说明 admin 密码已被修改）", f"status={r.status_code}")

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
        models = d["data"]["models"]
        log(isinstance(models, list) and all(isinstance(m.get("model"), str) for m in models),
            "模型列表结构正常")

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
    avatar_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/xkAAAAASUVORK5CYII="
    )
    r = req("POST", "/api/client/avatar", token=user_token, files={"file": ("t.png", avatar_png, "image/png")})
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

print("=" * 66)
print("G. 报告页一致性检查（步骤14 徽标与其「重新检查」按钮）")
print("=" * 66)
r = req("GET", "/api/agent/workflow/fe-no-such-session/status", token=user_token)
d = body_of(r)
# consistency 字段是否随状态返回，由 tests/test_engineering_regressions.py 的
# test_status_exposes_consistency_report 覆盖；这里只验路由是否按归属拦截而非崩 500。
log(r.status_code == 404, "GET 非本人会话状态 → 404（不泄露、不 500）", f"status={r.status_code}")
r = req("POST", "/api/agent/workflow/fe-no-such-session/consistency-check", token=user_token)
d = body_of(r)
log(r.status_code == 404, "POST 非本人会话一致性检查 → 404（不泄露、不 500）",
    f"status={r.status_code} {str(d)[:120]}")

print("=" * 66)
print("H. 步骤7 评分落地台账（徽标与实际服务的前端代码）")
print("=" * 66)
r = req("GET", "/api/agent/workflow/fe-no-such-session/score-summary", token=user_token)
log(r.status_code == 404, "GET 非本人会话评分汇总 → 404（不泄露、不 500）", f"status={r.status_code}")
# 徽标是否真的会被浏览器加载到：直接查后端实际服务的 HTML，而不是查磁盘文件
r = req("GET", "/client", timeout=15)
html = r.text if r.status_code == 200 else ""
log("buildScoreDispositionBadge" in html, "/client 服务出的 HTML 含评分落地徽标函数",
    f"len={len(html)}")
log("if(sn===7)inner+=buildScoreDispositionBadge();" in html, "步骤7 面板会挂上该徽标")
log("data.score_disposition" in html, "徽标数据来自 status 的 score_disposition 字段")
log("模型判定资料不足" in html, "徽标区分「模型判定资料不足」与系统侧原因")
# 弱定位是 Batch 12 新增的第 7 类：文案里必须能看出「不计入总分」，
# 否则用户会把它当成已落地评分。
log("引用可定位到原文行" in html, "徽标识别弱定位（引用只到原文行号）")
log("不计入总分" in html, "弱定位文案说明不计入总分")

# 批次 27：报告列表的质量徽标。三种状态必须在文案上分得开 ——
# 若「部分项未验证」被显示成「通过」，用户会把没查过的报告当成查过了。
log("建议复核" in html, "/client 服务出的 HTML 含「建议复核」徽标")
log("部分项未验证" in html, "质量徽标区分「部分项未验证」（有台账项通过、其余无从判断）")
log("质量未验证" in html, "质量徽标区分「质量未验证」（一条判据都没跑）")
log("d.review" in html or "rv=d.review" in html, "报告卡片读取接口返回的 review 字段")

# 批次 29：步骤 9 的漏项补全。徽标读 /status 里的台账，正文走单独接口。
log("buildCritiqueBadge" in html, "/client 服务出的 HTML 含漏项补全徽标（两步共用）")
log("showCritique" in html, "徽标可展开修订前后对比")
log("'/critique/'+step" in html, "对比面板调用 /critique/{step} 接口")
log("toggleCritique" in html, "/client 含补全开关按钮")
log("critique_switches" in html, "开关状态来自 /status 的 critique_switches")

# 批次 36（B4）：refine 先出草稿、预览差异，采用后才落库；步骤带版本回滚入口。
log("renderRefinePreview" in html, "/client 服务出的 HTML 含 refine 草稿预览函数")
log("preview:true" in html, "refine 请求走 preview 分支（不直接落库）")
log("applyRefinePreview" in html, "预览卡片带「采用」动作")
log("showStepHistory" in html, "步骤面板带历史版本入口")
log("'/rollback'" in html, "历史版本可回滚（调用 /rollback）")
log("refine-degraded" in html, "降级生成的草稿会标注「由备选模型完成」")

# 批次 38（B5）：聊天走 SSE、引用报告上下文、失败回退非流式
log("streamChatReply" in html, "/client 含聊天流式函数")
log("/api/client/ai/chat/stream" in html, "聊天优先调用流式接口")
log("workflow_session_id" in html, "聊天请求带当前报告 id（用于引用）")
log("appendChatReferences" in html, "回答下方展示引用编号")
log("addStreamingBubble" in html, "流式回答逐段渲染到气泡")

# 批次 39（B5 第二步）：引用从「编号」变成「可展开的章节片段」
log("toggleChatRef" in html, "引用徽标可展开片段原文")
log("chat-ref-detail-open" in html, "展开的片段有独立样式")

r = req("GET", "/api/agent/workflow/fe-no-such-session/critique/10", token=user_token)
log(r.status_code == 404, "GET 不存在会话的步骤10补全台账 → 404（不 500）", f"status={r.status_code}")

# 不支持的步骤必须 400，但依赖 `_require_path_workflow` 会先判会话 ——
# 拿不存在的 session 只会得到 404，测不到 400 那条分支。用管理员账号（名下有真实会话）。
_adm_reports = (req("GET", "/api/client/reports", token=admin_token).json().get("data") or [])
_adm_sid = _adm_reports[0]["session_id"] if _adm_reports else ""
r = req("GET", f"/api/agent/workflow/{_adm_sid}/critique/12", token=admin_token)
log(bool(_adm_sid) and r.status_code == 400,
    "已有会话 + 不支持的步骤 → 400（而不是静默返回别的步骤）",
    f"sid={_adm_sid[:8] or '无'} status={r.status_code}")

# 开关的联调：真的关掉再打开，并从 /status 读回当前值。
# 跑完会恢复成「开」——这是新会话的默认值，不留副作用。
if _adm_sid:
    _off = req("POST", "/api/agent/workflow/set-critique", token=admin_token,
               json_body={"session_id": _adm_sid, "enabled": False, "step": 10})
    _st1 = req("GET", f"/api/agent/workflow/{_adm_sid}/status", token=admin_token).json()
    _on = req("POST", "/api/agent/workflow/set-critique", token=admin_token,
              json_body={"session_id": _adm_sid, "enabled": True, "step": 10})
    _st2 = req("GET", f"/api/agent/workflow/{_adm_sid}/status", token=admin_token).json()
    log(_off.status_code == 200 and _st1["data"]["critique_switches"]["10"] is False
        and _on.status_code == 200 and _st2["data"]["critique_switches"]["10"] is True,
        "步骤10补全开关能真正关掉再打开（读回状态为准）")

r = req("GET", "/api/agent/workflow/fe-no-such-session/step9-critique", token=user_token)
log(r.status_code == 404, "批次 29 的旧路径仍可用（404 说明路由在）", f"status={r.status_code}")

r = req("POST", "/api/agent/workflow/set-critique", token=user_token,
        json_body={"session_id": "fe-no-such-session", "enabled": True, "step": 10})
log(r.status_code == 404, "POST 开关不存在的会话 → 404（不 500）", f"status={r.status_code}")

# 用管理员 token：这个会话属于管理员，换成刚注册的 user_token 会先被判「工作流不存在」
r = req("POST", "/api/agent/workflow/set-critique", token=admin_token,
        json_body={"session_id": _adm_sid, "enabled": True, "step": 11})
log(bool(_adm_sid) and r.status_code == 400, "开关不支持的步骤 → 400",
    f"status={r.status_code}")

# 批次 32：备用模型（降级链）的配置入口 —— 能配、能读回，且跑完把原值放回去
r = req("GET", "/admin", timeout=15)
admin_html = r.text if r.status_code == 200 else ""
log("备用模型（降级链）" in admin_html, "/admin 服务出的 HTML 含「备用模型（降级链）」卡片")
log("saveFallbackModels" in admin_html, "卡片带保存函数")

_before = (req("GET", "/api/admin/settings", token=admin_token).json() or {}).get("data", {})
_origin = _before.get("fallback_models") or []
r = req("POST", "/api/admin/settings/fallback-models", token=admin_token,
        json_body={"models": ["e2e-probe-model"]})
log(r.status_code == 200 and (r.json().get("data") or {}).get("fallback_models") == ["e2e-probe-model"],
    "POST 备用模型 → 读回一致", f"status={r.status_code}")
r = req("GET", "/api/admin/settings", token=admin_token)
log((r.json().get("data") or {}).get("fallback_models") == ["e2e-probe-model"],
    "GET 系统设置能看到备用模型")
r = req("POST", "/api/admin/settings/fallback-models", token=admin_token,
        json_body={"models": ["a", "b", "c"]})
log(r.status_code == 400, "超过 2 个备用模型 → 400", f"status={r.status_code}")
# 还原成跑之前的值，别给开发库留副作用
req("POST", "/api/admin/settings/fallback-models", token=admin_token,
    json_body={"models": _origin})

r = req("GET", "/api/client/reports", token=user_token)
rows = (r.json().get("data") or []) if r.status_code == 200 else []
log(all(isinstance(row.get("review"), dict) for row in rows),
    "报告列表每条都带 review 结论", f"{len(rows)} 条")
log(all(row["review"].get("verdict") in ("pass", "review", "partial", "unknown")
        for row in rows), "review.verdict 取值在四态之内")
log(all(row["review"].get("reasons") for row in rows if row["review"].get("required")),
    "标了建议复核的报告都给出了理由")

print()
print("=" * 66)
print(f"结果: {PASS} 通过 / {FAIL} 失败")
for f_ in FAILURES:
    print(f"  ✗ {f_}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
