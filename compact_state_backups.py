"""把现有 state_backups/*.json 重写成紧凑格式（批次 25 = C3 的一次性维护脚本）

批次 25 之后，每次 `update_state_backup` 都会自动写紧凑格式，但**历史文件不会自己
变小** —— 它们要等对应会话再写一次才会被重写。本脚本就是给这堆存量做一次迁移。

安全性：
- 默认只报告，不动磁盘；加 `--apply` 才真的重写。
- 重写前先做严格校验：`hydrate(新内容)` 必须与 `hydrate(旧内容)` 完全相等，
  不等就跳过这个文件并打印原因（宁可少省几个 KB，也不能丢一秒的状态）。
- 落盘复用 `_publish_backup`（临时文件 + 原子替换 + Windows 权限退避重试），
  不自己写裸 `open`。
- 建议停服执行：脚本与正在跑的服务不共享进程内锁，同时写同一个会话会互相覆盖。

用法：
    python compact_state_backups.py            # 只体检
    python compact_state_backups.py --apply    # 真迁移
    python compact_state_backups.py --dir state_backups --apply
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.agents import performance_agent
from backend.services import state_backup


def _load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def compact_file(path: Path, apply: bool = False) -> Dict[str, Any]:
    """压缩单个备份文件，返回一行统计。"""
    backup = _load(path)
    if backup is None:
        return {"file": path.name, "status": "无法解析", "before": path.stat().st_size,
                "after": path.stat().st_size}

    payload = state_backup.dumps(backup).encode("utf-8")
    before = path.stat().st_size
    after = len(payload)

    # 严格校验：还原后的语义必须完全一致
    if state_backup.hydrate(json.loads(payload)) != state_backup.hydrate(backup):
        return {"file": path.name, "status": "校验不通过，已跳过", "before": before, "after": before}

    if not apply or after >= before:
        return {"file": path.name, "status": "无需重写" if after >= before else "待重写",
                "before": before, "after": before if after >= before else after}

    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(payload)
        performance_agent._publish_backup(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"file": path.name, "status": "已重写", "before": before, "after": after}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(performance_agent.STATE_BACKUP_DIR))
    parser.add_argument("--apply", action="store_true", help="真的重写磁盘（默认只体检）")
    args = parser.parse_args()

    root = Path(args.dir)
    files: List[Path] = sorted(root.glob("*.json"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        print(f"{root} 下没有备份文件")
        return

    rows = [compact_file(path, apply=args.apply) for path in files]

    print(f"{'会话':<40}{'原KB':>9}{'新KB':>9}{'比例':>8}  状态")
    for row in rows:
        ratio = (row["after"] / row["before"]) if row["before"] else 1.0
        print(f"{row['file']:<40}{row['before']/1024:>9.0f}{row['after']/1024:>9.0f}"
              f"{ratio:>7.0%}  {row['status']}")

    before_total = sum(row["before"] for row in rows)
    after_total = sum(row["after"] for row in rows)
    print()
    print(f"合计 {before_total/1024/1024:.1f} MB → {after_total/1024/1024:.1f} MB"
          f"（{after_total/before_total:.0%}）")
    if not args.apply:
        print("这是体检模式，磁盘未改动。加 --apply 执行迁移。")


if __name__ == "__main__":
    main()
