"""
API 全功能测试脚本 — 测试所有核心端点（与当前前端调用保持一致）
用法: python test_all_apis.py
"""
import requests
import json
import sys
import uuid

BASE = "http://127.0.0.1:8000"
HEADERS = {"Content-Type": "application/json"}
passed = 0
failed = 0

def test(name, fn):
    global passed, failed
    try:
        result = fn()
        if result is True:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {result}")
            failed += 1
    except Exception as e:
        print(f"  FAIL: {name} — 异常: {e}")
        failed += 1

def api(method, path, data=None, token=None, files=None):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if files is not None:
        return requests.post(f"{BASE}{path}", files=files, headers=h, timeout=20)
    if method == "GET":
        return requests.get(f"{BASE}{path}", headers=h, timeout=20)
    if method == "POST":
        return requests.post(f"{BASE}{path}", json=data, headers=h, timeout=30)
    if method == "PUT":
        return requests.put(f"{BASE}{path}", json=data, headers=h, timeout=20)
    if method == "DELETE":
        return requests.delete(f"{BASE}{path}", headers=h, timeout=20)
    raise ValueError(method)


def _e2e_admin_login():
    """用 E2E 专用管理员登录。

    不再硬编码 admin/Admin@123456：该账号已被首登强制改密打标，
    管理端接口对它一律 403（这是安全策略在起作用，不是故障）。详见 _e2e_auth.py。
    """
    from _e2e_auth import ensure_e2e_admin
    username, password = ensure_e2e_admin(BASE)
    return api("POST", "/api/auth/login", {"username": username, "password": password})

def expect_success(r, expected_code=200):
    if r.status_code != expected_code:
        return f"状态码={r.status_code} body={r.text[:200]}"
    body = r.json() if r.text else {}
    if not body.get("success"):
        return f"success=false body={body}"
    return True

def expect_code(r, code):
    if r.status_code != code:
        return f"期望状态码={code}, 实际={r.status_code} body={r.text[:200]}"
    return True

# =================== 认证测试 ===================
print("=" * 60)
print("1. 认证 API 测试")
print("=" * 60)

test("首页加载", lambda: expect_code(api("GET", "/"), 200))

test_username = f"testuser_{uuid.uuid4().hex[:8]}"
test_password = "Test#123456"
reg_data = {"username": test_username, "password": test_password, "phone": f"138{uuid.uuid4().hex[:8]}"}
reg_resp = api("POST", "/api/auth/register", reg_data)
test("注册新用户", lambda: expect_success(reg_resp))

reg_resp2 = api("POST", "/api/auth/register", reg_data)
test("重复注册应失败", lambda: expect_code(reg_resp2, 400))

login_resp = api("POST", "/api/auth/login", {"username": test_username, "password": test_password})
test("登录", lambda: expect_success(login_resp) == True and bool(login_resp.json().get("data", {}).get("access_token")))
user_token = login_resp.json().get("data", {}).get("access_token", "")

test("错误密码登录应失败", lambda: expect_code(
    api("POST", "/api/auth/login", {"username": test_username, "password": "wrong"}), 401))

# =================== 客户端 API 测试 ===================
print("\n" + "=" * 60)
print("2. 客户端 API 测试")
print("=" * 60)

if user_token:
    test("获取个人信息 GET /api/client/me", lambda: expect_success(
        api("GET", "/api/client/me", token=user_token)))
    test("获取仪表盘 GET /api/client/dashboard", lambda: expect_success(
        api("GET", "/api/client/dashboard", token=user_token)))
    test("获取最近活动 GET /api/client/activity", lambda: expect_success(
        api("GET", "/api/client/activity", token=user_token)))
    test("Token统计 GET /api/client/token-stats", lambda: expect_success(
        api("GET", "/api/client/token-stats", token=user_token)))
    test("报告列表 GET /api/client/reports", lambda: expect_success(
        api("GET", "/api/client/reports", token=user_token)))

    def reports_carry_review():
        """批次 27：每条报告都要带质量结论，且状态值只能是四态之一。"""
        r = api("GET", "/api/client/reports", token=user_token)
        if not r.ok:
            return f"HTTP {r.status_code}"
        for row in r.json().get("data", []):
            review = row.get("review")
            if not isinstance(review, dict):
                return f"缺少 review 字段：{row.get('session_id')}"
            if review.get("verdict") not in ("pass", "review", "partial", "unknown"):
                return f"verdict 非法：{review.get('verdict')}"
            if review.get("required") and not review.get("reasons"):
                return f"标了建议复核却没给理由：{row.get('session_id')}"
        return True

    test("报告列表带质量结论", reports_carry_review)
    test("通知列表 GET /api/client/notifications", lambda: expect_success(
        api("GET", "/api/client/notifications", token=user_token)))
    test("可用模型 GET /api/client/ai/models", lambda: expect_success(
        api("GET", "/api/client/ai/models", token=user_token)))
    test("AI聊天状态 GET /api/client/ai/chat-status", lambda: expect_success(
        api("GET", "/api/client/ai/chat-status", token=user_token)))
    test("对话列表 GET /api/client/ai/conversations", lambda: expect_success(
        api("GET", "/api/client/ai/conversations", token=user_token)))

    conv_resp = api("POST", "/api/client/ai/conversations", {"title": "测试对话"}, token=user_token)
    test("新建对话", lambda: expect_success(conv_resp))
    conv_id = conv_resp.json().get("data", {}).get("id")
    test("删除对话", lambda: expect_success(
        api("DELETE", f"/api/client/ai/conversations/{conv_id}", token=user_token))) if conv_id else None

    test("修改用户名并还原", lambda: expect_success(api("PUT", "/api/client/profile",
        {"username": test_username + "_x"}, token=user_token)) and expect_success(
        api("PUT", "/api/client/profile", {"username": test_username}, token=user_token)) == True)
else:
    print("  跳过客户端测试（无 token）")

# =================== 管理端 API 测试 ===================
print("\n" + "=" * 60)
print("3. 管理端 API 测试")
print("=" * 60)

admin_login = _e2e_admin_login()
admin_ok = admin_login.status_code == 200 and admin_login.json().get("success")

if admin_ok:
    admin_token = admin_login.json().get("data", {}).get("access_token", "")

    for name, path in [
        ("管理端仪表盘", "/api/admin/dashboard"),
        ("Token统计", "/api/admin/token-stats"),
        ("AI对话统计", "/api/admin/ai-chat-stats"),
        ("系统设置", "/api/admin/settings"),
        ("用户列表", "/api/admin/users?page=1&page_size=20"),
        ("模型配置", "/api/admin/models"),
        ("Prompt模板", "/api/admin/prompts"),
        ("默认Prompt", "/api/admin/prompts/defaults"),
        ("知识库列表", "/api/admin/knowledge?page=1&page_size=10"),
        ("工作流监控", "/api/admin/workflows?page=1&page_size=20"),
        ("审计日志", "/api/admin/audit-logs?page=1&page_size=10"),
        ("审计摘要", "/api/admin/audit-logs/summary"),
        ("文件统计", "/api/admin/file-stats"),
        ("步骤配置", "/api/admin/step-config"),
        ("模块序列", "/api/admin/module-sequences"),
        ("通知历史", "/api/admin/notifications?page=1&page_size=15"),
        ("清理预览", "/api/admin/cleanup/preview"),
    ]:
        test(name, lambda p=path: expect_success(api("GET", p, token=admin_token)))

    test("无token访问管理端 → 401", lambda: expect_code(api("GET", "/api/admin/dashboard"), 401))
    if user_token:
        test("普通用户访问管理端 → 403", lambda: expect_code(
            api("GET", "/api/admin/dashboard", token=user_token), 403))
else:
    print("  WARNING: 管理员登录失败，跳过管理端测试")

# =================== 文件上传测试 ===================
print("\n" + "=" * 60)
print("4. 文件上传测试")
print("=" * 60)

sample_txt = "测试项目资料：某道路建设工程，总投资5000万元。"
with open("_api_test_sample.txt", "w", encoding="utf-8") as f:
    f.write(sample_txt)

with open("_api_test_sample.txt", "rb") as f:
    up_resp = api("POST", "/api/agent/upload", token=user_token,
                  files={"file": ("_api_test_sample.txt", f, "text/plain")})
test("单文件上传", lambda: expect_success(up_resp))

with open("_api_test_sample.txt", "rb") as f:
    batch_resp = api("POST", "/api/agent/upload/batch", token=user_token,
                     files=[("files", ("_api_test_sample.txt", f, "text/plain"))])
test("批量上传", lambda: expect_success(batch_resp))

# =================== 前端页面加载测试 ===================
print("\n" + "=" * 60)
print("5. 前端页面加载测试")
print("=" * 60)

for p in ["/", "/login", "/login?redirect=/client", "/client", "/admin",
          "/workflow", "/report", "/questionnaire"]:
    test(f"页面加载 {p}", lambda p=p: expect_code(api("GET", p), 200))

# =================== 结果汇总 ===================
print("\n" + "=" * 60)
total = passed + failed
print(f"测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
if failed > 0:
    print(f"存在 {failed} 个失败用例！")
    sys.exit(1)
else:
    print("全部通过")
    sys.exit(0)
