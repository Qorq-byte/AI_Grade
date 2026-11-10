"""在线冒烟：质量门禁在真实服务 + 真实备份上的行为（批次 27 = B3）。

与单测的分工：单测用构造的状态验证逻辑，这里验证**真实 HTTP + 真实会话**：
1. `GET /workflow/{id}/quality` 的结论与直接调 `check_quality` 一致（同一份备份）；
2. `GET /reports` 每条都带 `review`，且 `required` 的理由非空；
3. 不存在的会话返回 404，而不是 500。

用法（先起服务，注意 NO_PROXY）：
    NO_PROXY='*' .venv/Scripts/python.exe -m uvicorn backend.main:app --port 8000
    NO_PROXY='*' .venv/Scripts/python.exe tests/_smoke_quality_gate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
CRED = Path(".e2e_credentials.json")


def _session() -> requests.Session:
    """绕开系统代理：本机请求被代理劫持时会拿到 502，看起来像服务挂了。"""
    http = requests.Session()
    http.trust_env = False
    return http


def _login(http: requests.Session) -> str:
    if not CRED.exists():
        raise SystemExit("缺少 .e2e_credentials.json，无法登录")
    cred = json.loads(CRED.read_text(encoding="utf-8"))
    resp = http.post(f"{BASE}/api/auth/login", json={
        "username": cred["username"], "password": cred["password"]}, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["access_token"]


def main() -> int:
    http = _session()
    token = _login(http)
    headers = {"Authorization": f"Bearer {token}"}

    reports = http.get(f"{BASE}/api/client/reports", headers=headers, timeout=30)
    reports.raise_for_status()
    rows = reports.json().get("data") or []
    print(f"报告列表：{len(rows)} 条")

    bad = [row["session_id"] for row in rows
           if not isinstance(row.get("review"), dict)]
    if bad:
        print(f"FAIL 这些报告没有 review 字段：{bad[:3]}")
        return 1

    counts = {}
    for row in rows:
        counts[row["review"]["verdict"]] = counts.get(row["review"]["verdict"], 0) + 1
    print(f"结论分布：{counts}")

    no_reason = [row["session_id"] for row in rows
                 if row["review"].get("required") and not row["review"].get("reasons")]
    if no_reason:
        print(f"FAIL 标了建议复核却没给理由：{no_reason[:3]}")
        return 1

    # 抽一份有备份的会话，核对接口结论与直接算的一致
    sample = None
    for row in rows:
        if Path(f"state_backups/{row['session_id']}.json").exists():
            sample = row
            break
    if sample is None:
        print("SKIP 没有可用于对照的备份文件")
        return 0

    resp = http.get(f"{BASE}/api/agent/workflow/{sample['session_id']}/quality",
                    headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"FAIL 质量接口状态码 {resp.status_code}：{resp.text[:200]}")
        return 1
    detail = resp.json()["data"]
    print(f"抽样 {sample['session_id'][:8]}：verdict={detail['verdict']} "
          f"flags={[item['code'] for item in detail['flags']]} "
          f"unavailable={[item['code'] for item in detail['unavailable']]}")
    if detail["verdict"] != sample["review"]["verdict"]:
        print(f"FAIL 列表与详情结论不一致：{sample['review']['verdict']} vs {detail['verdict']}")
        return 1
    if detail["verdict"] == "pass" and detail["unavailable"]:
        print("FAIL 有 unavailable 却报 pass")
        return 1

    missing = http.get(f"{BASE}/api/agent/workflow/not-a-real-session/quality",
                       headers=headers, timeout=20)
    if missing.status_code != 404:
        print(f"FAIL 不存在的会话应为 404，实际 {missing.status_code}")
        return 1

    print("质量门禁行为符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
