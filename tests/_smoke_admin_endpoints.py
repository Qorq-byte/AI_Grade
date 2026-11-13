"""管理端端到端冒烟（对着真实 MySQL 跑，不进 pytest 收集）。

为什么需要它：单测跑在 SQLite 上，摸不到这几类东西 ——
- MySQL 特有 SQL（token-stats 里 `DATE_ADD ... INTERVAL 8 HOUR` 的分桶）
- 真实 `state_backups/*.json`（分步正文只从这里读）
- `information_schema` 补丁之后的真实表结构
- 真实数据规模下的响应形状（14 条系统提示词、几十个工作流）

做法：临时建一个 `_smoke_admin` 管理员（不动真实 admin —— 它还被强制改密挡着），
用真实登录拿令牌，把所有管理端接口按读写走一遍，最后删掉临时账号与它造的数据。

用法：python tests/_smoke_admin_endpoints.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.main import app
from backend.models.user_models import (
    KnowledgeBase, Notification, OperationLog, PromptTemplate, PromptVersion, User, UserRole,
)
from backend.services.auth_service import hash_password

SMOKE_USER = "_smoke_admin"
SMOKE_TARGET = "_smoke_target"   # 通知只发给普通用户，管理员会被过滤掉
SMOKE_PASSWORD = "Smoke@123456"
PREFIX = "_smoke_"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
    return ok


def cleanup(db):
    """把临时账号和它造的东西全删掉，顺序按外键来。"""
    names = [SMOKE_USER, SMOKE_TARGET]
    ids = [u.id for u in db.query(User).filter(User.username.in_(names)).all()]
    db.query(KnowledgeBase).filter(KnowledgeBase.name.like(PREFIX + "%")).delete(synchronize_session=False)
    if ids:
        db.query(Notification).filter(Notification.user_id.in_(ids)).delete(synchronize_session=False)
        db.query(Notification).filter(Notification.title.like(PREFIX + "%")).delete(synchronize_session=False)
    for p in db.query(PromptTemplate).filter(PromptTemplate.step_name.like(PREFIX + "%")).all():
        db.query(PromptVersion).filter(PromptVersion.prompt_id == p.id).delete(synchronize_session=False)
        db.delete(p)
    if ids:
        db.query(OperationLog).filter(OperationLog.user_id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
    db.commit()


def main():
    db = SessionLocal()
    cleanup(db)  # 上次跑挂了也不会留垃圾

    hashed, salt = hash_password(SMOKE_PASSWORD)
    smoke = User(username=SMOKE_USER, password_hash=f"{salt}:{hashed}",
                 role=UserRole.ADMIN, is_active=True, must_change_password=False)
    target = User(username=SMOKE_TARGET, password_hash=f"{salt}:{hashed}",
                  role=UserRole.USER, is_active=True, must_change_password=False)
    db.add_all([smoke, target])
    db.commit()
    db.refresh(smoke); db.refresh(target)
    smoke_id, target_id = smoke.id, target.id
    db.close()

    client = TestClient(app)
    token = ""
    try:
        print("\n== 认证 ==")
        r = client.post("/api/auth/login", json={"username": SMOKE_USER, "password": SMOKE_PASSWORD})
        check("登录", r.status_code == 200, f"HTTP {r.status_code}")
        token = (r.json().get("data") or {}).get("access_token", "")
        H = {"Authorization": f"Bearer {token}"}

        r = client.get("/api/admin/me", headers=H)
        check("GET /me", r.status_code == 200 and r.json()["data"]["role"] == "admin")

        print("\n== 只读接口 ==")
        read_only = [
            ("/api/admin/dashboard", "dashboard"),
            ("/api/admin/users?page=1&page_size=5", "users"),
            ("/api/admin/users/options?role=user", "users/options"),
            ("/api/admin/audit-logs?page_size=5", "audit-logs"),
            ("/api/admin/audit-logs/summary", "audit-logs/summary"),
            ("/api/admin/workflows?page_size=5", "workflows"),
            ("/api/admin/models", "models"),
            ("/api/admin/prompts", "prompts"),
            ("/api/admin/knowledge", "knowledge"),
            ("/api/admin/settings", "settings"),
            ("/api/admin/token-stats", "token-stats"),
            ("/api/admin/ai-chat-stats", "ai-chat-stats"),
            ("/api/admin/notifications?page_size=5", "notifications"),
            ("/api/admin/module-sequences", "module-sequences"),
            ("/api/admin/step-config", "step-config"),
            ("/api/admin/cleanup/preview", "cleanup/preview"),
            ("/api/admin/file-stats", "file-stats"),
            ("/api/admin/system-health", "system-health"),
        ]
        for path, name in read_only:
            r = client.get(path, headers=H)
            ok = r.status_code == 200 and r.json().get("success") is True
            check(f"GET {name}", ok, "" if ok else f"HTTP {r.status_code} {r.text[:120]}")

        # token-stats 的北京日分桶是 MySQL 专用 SQL，跑挂了这里就会红
        r = client.get("/api/admin/token-stats", headers=H)
        trend = (r.json().get("data") or {}).get("daily_trend") or []
        check("token-stats 北京日分桶可用", len(trend) > 0, f"{len(trend)} 个数据点")

        print("\n== 工作流详情与正文 ==")
        with SessionLocal() as db2:
            from backend.models.user_models import WorkflowSession
            w = (db2.query(WorkflowSession)
                 .filter(WorkflowSession.status == "completed")
                 .first())
            sid = w.session_id if w else None
        if sid:
            r = client.get(f"/api/admin/workflows/{sid}", headers=H)
            d = r.json().get("data") or {}
            check("GET 工作流详情", r.status_code == 200 and len(d.get("timeline", [])) == 14,
                  f"timeline {len(d.get('timeline', []))} 段 / sections {len(d.get('sections', []))} 段")

            r = client.get(f"/api/admin/workflows/{sid}/content?step=14", headers=H)
            secs = (r.json().get("data") or {}).get("sections") or []
            check("GET 第 14 步正文", r.status_code == 200 and len(secs) > 0,
                  f"{secs[0]['chars']} 字 / 来源 {r.json()['data']['source']}" if secs else "无内容")

            r = client.get("/api/admin/workflows/..%2Fsecret/content?step=1", headers=H)
            check("路径穿越被挡", r.status_code in (400, 404), f"HTTP {r.status_code}")
        else:
            check("工作流详情", False, "库里没有 completed 会话，跳过")

        print("\n== 边界 ==")
        r = client.get("/api/admin/users?page_size=101", headers=H)
        check("page_size 超限被拒", r.status_code == 422, f"HTTP {r.status_code}")

        r = client.get("/api/admin/users?sort=tokens_desc&page_size=10", headers=H)
        tokens = [u["tokens_used"] or 0 for u in r.json()["data"]["users"]]
        check("Token 降序排序", r.status_code == 200 and tokens == sorted(tokens, reverse=True),
              f"前三个 {tokens[:3]}")

        r = client.get("/api/admin/users?sort=drop_table", headers=H)
        check("未知排序键不炸", r.status_code == 200, f"HTTP {r.status_code}")

        if sid:
            r = client.get(f"/api/admin/workflows/{sid}/content?step=15", headers=H)
            check("步骤越界被拒", r.status_code == 422, f"HTTP {r.status_code}")

        r = client.get("/api/admin/audit-logs/export?start_date=不是日期", headers=H)
        check("坏日期参数被忽略而不是 500", r.status_code == 200, f"HTTP {r.status_code}")

        r = client.get("/admin")
        check("管理端页面可下发", r.status_code == 200 and "/static/admin/app.js" in r.text,
              f"{len(r.text)} 字节")
        r = client.get("/static/admin/app.js")
        check("脚本带缓存校验头", r.status_code == 200 and bool(r.headers.get("etag")),
              r.headers.get("content-type", ""))

        print("\n== 审计导出 ==")
        r = client.get("/api/admin/audit-logs/export", headers=H)
        ok = (r.status_code == 200 and "text/csv" in r.headers.get("content-type", "")
              and r.content.startswith("﻿".encode("utf-8")))
        check("导出 CSV", ok, f"{r.headers.get('X-Export-Rows')} 行 / {len(r.content)} 字节")
        r2 = client.get("/api/admin/audit-logs/export?action=login", headers=H)
        check("导出带筛选", r2.headers.get("X-Export-Rows") is not None,
              f"action=login 命中 {r2.headers.get('X-Export-Rows')} 行")

        print("\n== 写入类：知识库 ==")
        r = client.post("/api/admin/knowledge", headers=H,
                        json={"name": PREFIX + "single", "content": "内容", "step_number": 3})
        kb_id = (r.json().get("data") or {}).get("id")
        check("新增单条知识", r.status_code == 200 and kb_id, f"id={kb_id}")

        r = client.post("/api/admin/knowledge/bulk", headers=H, json={"items": [
            {"name": PREFIX + "bulk1", "content": "批量1", "step_number": 6},
            {"name": PREFIX + "bulk1", "content": "重复", "step_number": 6},
            {"name": "", "content": "脏数据"},
        ], "skip_existing": True})
        d = r.json().get("data") or {}
        check("批量导入（含重复与脏行）",
              d.get("created") == 1 and d.get("skipped") == 1 and len(d.get("failed") or []) == 1,
              f"新增 {d.get('created')} / 跳过 {d.get('skipped')} / 失败 {len(d.get('failed') or [])}")

        print("\n== 写入类：通知 ==")
        r = client.post("/api/admin/notifications", headers=H,
                        json={"title": PREFIX + "notif", "content": "冒烟", "user_ids": [target_id]})
        check("发送通知", r.status_code == 200 and r.json().get("success") is True, r.text[:80])

        r = client.get("/api/admin/notifications?page_size=100", headers=H)
        mine = [n for n in (r.json()["data"]["notifications"] or []) if n["title"] == PREFIX + "notif"]
        check("列表能看到刚发的通知", len(mine) == 1, f"{len(mine)} 条")

        if mine:
            r = client.post("/api/admin/notifications/batch-withdraw", headers=H,
                            json={"ids": [mine[0]["id"]]})
            check("批量撤回", r.status_code == 200 and r.json()["data"]["deleted"] == 1,
                  r.json().get("message", ""))

        print("\n== 写入类：提示词版本 ==")
        r = client.post("/api/admin/prompts/granular", headers=H, json={
            "step_number": 7, "step_name": PREFIX + "prompt",
            "prompt_content": "第一版内容", "change_log": "冒烟创建",
        })
        pid = (r.json().get("data") or {}).get("id")
        check("新建提示词", r.status_code == 200 and pid, f"id={pid}")

        if pid:
            client.put(f"/api/admin/prompts/{pid}", headers=H,
                       json={"prompt_content": "第二版内容", "change_log": "冒烟修改"})
            r = client.get(f"/api/admin/prompts/{pid}/versions", headers=H)
            versions = r.json()["data"]["versions"]
            check("版本历史有 2 版", [v["version"] for v in versions] == [2, 1],
                  f"{[v['version'] for v in versions]}")

            r = client.post(f"/api/admin/prompts/{pid}/rollback/1", headers=H)
            check("回滚到 v1", r.status_code == 200, r.json().get("message", ""))

            r = client.get(f"/api/admin/prompts/{pid}/versions", headers=H)
            versions = r.json()["data"]["versions"]
            check("回滚后历史变 3 版", len(versions) == 3, f"{[v['version'] for v in versions]}")

            r = client.delete(f"/api/admin/prompts/{pid}", headers=H)
            check("删除提示词", r.status_code == 200, r.text[:80])

        print("\n== 权限 ==")
        r = client.get("/api/admin/system-health")
        check("无令牌被拒", r.status_code in (401, 403), f"HTTP {r.status_code}")

    finally:
        with SessionLocal() as db3:
            cleanup(db3)
            left_user = db3.query(User).filter(User.username.in_([SMOKE_USER, SMOKE_TARGET])).count()
            left_kb = db3.query(KnowledgeBase).filter(KnowledgeBase.name.like(PREFIX + "%")).count()
            left_prompt = db3.query(PromptTemplate).filter(PromptTemplate.step_name.like(PREFIX + "%")).count()
            left_notif = db3.query(Notification).filter(Notification.title.like(PREFIX + "%")).count()
        print(f"\n清理：临时用户 {left_user} / 知识 {left_kb} / 提示词 {left_prompt} / 通知 {left_notif}（都应为 0）")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
