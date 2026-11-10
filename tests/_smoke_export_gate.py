"""在线冒烟：导出闸门在真实服务上的行为（批次 26 = B2）。

三件事，全部走 HTTP：
1. 真实完整报告（挑一份存量会话）→ 直接导出 200；
2. 造一份「第六章只有占位符」的会话 → 导出 409 + `X-Export-Blocked: 1`，
   且 detail 里点得出是哪一章；
3. 同一份带 `force: true` 再导 → 200（用户确认后不该被卡死）；
4. `/status` 的 consistency 里能读到 `exportable` 与 `blocking_count`。

造的临时会话在 finally 里删掉，不污染 state_backups。
前置：后端在 127.0.0.1:8000 运行。
用法：`python -m tests._smoke_export_gate`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from _e2e_auth import ensure_e2e_admin
from backend.agents import performance_agent as pa

BASE = "http://127.0.0.1:8000"
TEMP_SESSION = "b26-gate-smoke"
EMPTY_CHAPTER = "（本部分内容尚未生成）"


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _login(session: requests.Session) -> None:
    username, password = ensure_e2e_admin(BASE)
    resp = session.post(f"{BASE}/api/auth/login",
                        json={"username": username, "password": password}, timeout=15)
    assert resp.status_code == 200 and resp.json().get("success"), resp.text
    session.headers["Authorization"] = f"Bearer {resp.json()['data']['access_token']}"


def _clean_session_id() -> str:
    """挑一份完整报告（导出闸门放行的那种）。"""
    for path in sorted(pa.STATE_BACKUP_DIR.glob("*.json"), key=lambda p: p.stat().st_size, reverse=True):
        try:
            backup = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if backup.get("final_report") and EMPTY_CHAPTER not in backup["final_report"]:
            return path.stem
    raise SystemExit("没有可用的完整报告样本")


def _seed_blocked(report: str) -> None:
    payload = {
        "session_id": TEMP_SESSION,
        "project_name": "导出闸门冒烟项目",
        "final_report": report,
        "completed_steps": list(range(1, 15)),
        "report_section_order": None,
    }
    pa.STATE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    (pa.STATE_BACKUP_DIR / f"{TEMP_SESSION}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _export(session, session_id: str, **extra):
    return session.post(f"{BASE}/api/agent/workflow/export",
                        json={"session_id": session_id, "format": "word", **extra}, timeout=120)


def main() -> int:
    session = _session()
    _login(session)
    problems = []

    clean = _clean_session_id()
    resp = _export(session, clean)
    print(f"完整报告 {clean[:8]} → {resp.status_code}")
    if resp.status_code != 200:
        problems.append(f"完整报告被误拦：{resp.status_code} {resp.text[:120]}")

    body = ("# 冒烟项目\n\n## 一、绩效评价工作开展情况\n\n" + "评价组通过资料审查与现场核查开展工作。" * 30
            + f"\n\n## 六、主要经验做法\n\n{EMPTY_CHAPTER}\n")
    _seed_blocked(body)
    try:
        blocked = _export(session, TEMP_SESSION)
        print(f"缺章会话 → {blocked.status_code}，标记头={blocked.headers.get('X-Export-Blocked')}")
        if blocked.status_code != 409:
            problems.append(f"缺章会话没有被拦：{blocked.status_code} {blocked.text[:160]}")
        elif blocked.headers.get("X-Export-Blocked") != "1":
            problems.append("409 缺少 X-Export-Blocked 标记头")
        elif "六、主要经验做法" not in blocked.text:
            problems.append("拦截原因没有点名缺哪一章")

        forced = _export(session, TEMP_SESSION, force=True)
        print(f"缺章会话 + force → {forced.status_code}")
        if forced.status_code != 200:
            problems.append(f"force 没能放行：{forced.status_code} {forced.text[:160]}")

        status = session.get(f"{BASE}/api/agent/workflow/{TEMP_SESSION}/status", timeout=30)
        consistency = (status.json().get("data") or {}).get("consistency") or {}
        print(f"/status consistency: exportable={consistency.get('exportable')} "
              f"blocking={consistency.get('blocking_count')}")
        if consistency.get("exportable") is not False or not consistency.get("blocking_count"):
            problems.append(f"状态接口没有暴露拦截信息：{consistency}")
    finally:
        (pa.STATE_BACKUP_DIR / f"{TEMP_SESSION}.json").unlink(missing_ok=True)

    if problems:
        print("\n发现问题：")
        for item in problems:
            print("  -", item)
        return 1
    print("\n导出闸门行为符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
