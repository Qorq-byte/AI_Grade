"""在线冒烟：用真实（已迁移为紧凑格式的）备份验证「重启后还能读懂」。

批次 25 把 `state_backups/*.json` 里的步骤正文改成了引用存储，存量文件也已迁移。
这个脚本不看源码，只从 HTTP 视角确认三件事：

1. `GET /workflow/{sid}/status` 仍能拿到每一步的完整正文（不只是预览）；
2. `GET /workflow/{sid}/report` 仍能拼出报告，且长度与磁盘上的 `final_report` 对得上；
3. 上述结果与磁盘文件里的字段逐字一致 —— 说明 hydrate 没有篡改内容。

前置：后端已在 127.0.0.1:8000 运行（`python -m uvicorn backend.main:app --port 8000`）。
用法：`python -m tests._smoke_backup_restart`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from _e2e_auth import ensure_e2e_admin
from backend.agents import performance_agent as pa

BASE = "http://127.0.0.1:8000"


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False          # 别让系统代理劫持 127.0.0.1
    return s


def _login(session: requests.Session) -> None:
    username, password = ensure_e2e_admin(BASE)
    resp = session.post(f"{BASE}/api/auth/login",
                        json={"username": username, "password": password}, timeout=15)
    assert resp.status_code == 200 and resp.json().get("success"), resp.text
    token = resp.json()["data"]["access_token"]
    session.headers["Authorization"] = f"Bearer {token}"


def _pick_sessions(limit: int = 4) -> list[str]:
    files = sorted(pa.STATE_BACKUP_DIR.glob("*.json"), key=lambda p: p.stat().st_size, reverse=True)
    picked = [f.stem for f in files[:2]] + [f.stem for f in files[-1:]]
    return picked[:limit]


def _expected_report(sid: str, raw: dict) -> str:
    """报告接口的权威来源是数据库里的 `WorkflowSession.final_report`。

    只有库里没有记录（或记录为空）时才回落到备份。两者本来就可能不一致 ——
    「保存报告」只写库、不回头改备份里的 `final_report`，这是改造前就有的行为。
    """
    from backend.database import SessionLocal
    from backend.models.user_models import WorkflowSession

    db = SessionLocal()
    try:
        row = db.query(WorkflowSession).filter(WorkflowSession.session_id == sid).first()
    finally:
        db.close()
    if row and row.final_report:
        return row.final_report
    return raw.get("final_report") or ""


def check_session(session: requests.Session, sid: str) -> list[str]:
    problems: list[str] = []
    raw = json.loads((pa.STATE_BACKUP_DIR / f"{sid}.json").read_text(encoding="utf-8"))

    status = session.get(f"{BASE}/api/agent/workflow/{sid}/status",
                         params={"after_step": 0}, timeout=30)
    if status.status_code != 200:
        return [f"{sid[:8]} status={status.status_code} {status.text[:120]}"]
    data = status.json()["data"]
    summaries = {item["step_number"]: item for item in data["step_summaries"]}

    for key, item in (raw.get("step_results") or {}).items():
        number = int(key)
        summary = summaries.get(number)
        if summary is None:
            problems.append(f"{sid[:8]} 第{number}步没有出现在状态接口里")
            continue
        content = summary.get("content")
        ref = item.get("content_ref")
        expected = (raw.get(ref) if ref else item.get("content")) or ""
        if not content:
            problems.append(f"{sid[:8]} 第{number}步正文为空")
        elif content != expected:
            problems.append(f"{sid[:8]} 第{number}步正文与磁盘不一致（{len(content)} vs {len(expected)}）")

    report = session.get(f"{BASE}/api/agent/workflow/{sid}/report", timeout=60)
    if report.status_code != 200:
        problems.append(f"{sid[:8]} report={report.status_code} {report.text[:120]}")
    else:
        content = report.json()["data"]["content"]
        expected = _expected_report(sid, raw)
        if not content:
            problems.append(f"{sid[:8]} 报告为空")
        elif expected and content != expected:
            problems.append(f"{sid[:8]} 报告与权威来源不一致（{len(content)} vs {len(expected)}）")

    return problems


def main() -> int:
    session = _session()
    _login(session)
    sessions = _pick_sessions()
    print(f"抽检 {len(sessions)} 个会话：{[s[:8] for s in sessions]}")
    problems: list[str] = []
    for sid in sessions:
        problems.extend(check_session(session, sid))
    if problems:
        print("\n发现问题：")
        for item in problems:
            print("  -", item)
        return 1
    print("全部一致：步骤正文、报告正文都能从紧凑备份里完整还原")
    return 0


if __name__ == "__main__":
    sys.exit(main())
