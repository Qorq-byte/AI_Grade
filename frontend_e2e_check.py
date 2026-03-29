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

