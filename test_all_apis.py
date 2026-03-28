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
        if result:
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
test_password = "test123456"
reg_data = {"username": test_username, "password": test_password, "phone": f"138{uuid.uuid4().hex[:8]}"}
reg_resp = api("POST", "/api/auth/register", reg_data)
test("注册新用户", lambda: expect_success(reg_resp, 201))

reg_resp2 = api("POST", "/api/auth/register", reg_data)
test("重复注册应失败", lambda: expect_code(reg_resp2, 400))

login_resp = api("POST", "/api/auth/login", {"username": test_username, "password": test_password})
test("登录", lambda: expect_success(login_resp))
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
    test("通知列表 GET /api/client/notifications", lambda: expect_success(
        api("GET", "/api/client/notifications", token=user_token)))
    test("可用模型 GET /api/client/ai/models", lambda: expect_success(
        api("GET", "/api/client/ai/models", token=user_token)))
    test("AI聊天状态 GET /api/client/ai/chat-status", lambda: expect_success(
        api("GET", "/api/client/ai/chat-status", token=user_token)))
    test("对话列表 GET /api/client/ai/conversations", lambda: expect_success(
        api("GET", "/api/client/ai/conversations", token=user_token)))

