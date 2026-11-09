"""探针：量化 state_backups 的写放大与内容冗余（批次 25 = C3）。

做三件事，全部基于**磁盘上现有备份**的离线重放，不调模型、不花钱：

1. 写放大：把一份真实备份按 14 个步骤逐步喂给 `_persist_state_backup`，
   记每一步落盘后的文件大小。总写入量 / 最终体积 = 写放大倍数。
2. 读放大：`update_state_backup` 每次都是「读全量 → merge → 写全量」，
   所以除了写，每次还要把上一版整个读回来。
3. 内容冗余：`step_results[step].content` 与各步骤独立字段是否逐字相同。

注意：批次 25 之后落盘是紧凑格式（正文只存一份引用），但**运行期状态里始终是
全文**，所以重放前先 `hydrate`，否则探针测到的是一个现实里不存在的形状。

用法：
    python -m tests._probe_backup_write
    python -m tests._probe_backup_write --all              # 扫全目录取中位数
    python -m tests._probe_backup_write --all --dir .backup/state_backups_pre_b25
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
from pathlib import Path

from backend.agents import performance_agent as pa
from backend.services import state_backup

# 步骤 -> 该步写入的状态字段（从节点函数里 grep 出来的真实映射）
STEP_FIELDS = {
    1: ("cost_list",),
    2: ("effective_data",),
    3: ("indicator_system",),
    4: ("indicator_scores",),
    5: ("scoring_standards",),
    6: ("evaluation_table",),
    7: ("indicator_analysis",),
    8: ("experience_summary",),
    9: ("problems_analysis",),
    10: ("suggestions",),
    11: ("comprehensive_evaluation",),
    12: ("basic_info",),
    13: ("work_introduction",),
    14: ("final_report",),
}


def _bootstrap_state(backup: dict) -> dict:
    """把备份里「与步骤无关」的头部字段先放进 state。"""
    state = {"session_id": backup.get("session_id", "probe")}
    for key in ("project_name", "indicator_type"):
        if key in backup:
            state[key] = backup[key]
    return state


def replay_one(backup_path: Path, workdir: Path) -> dict:
    """按 14 步重放一次落盘，返回 {sizes, read_bytes, ...}。"""
    raw = json.loads(backup_path.read_text(encoding="utf-8"))
    # 运行期状态里永远是全文；紧凑格式只是磁盘上的编码
    backup = state_backup.hydrate(raw)
    step_results = backup.get("step_results", {})

    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    agent.session_id = backup.get("session_id") or backup_path.stem

    target = workdir / f"{agent.session_id}.json"
    state = _bootstrap_state(backup)
    sizes: list[int] = []
    read_bytes = 0

    present: dict[str, dict] = {}
    for step in range(1, 15):
        item = step_results.get(str(step))
        if item is None:
            continue
        present[str(step)] = item
        # 节点写的就是这几个字段；这里照抄，不猜
        for field in STEP_FIELDS.get(step, ()):
            if field in backup:
                state[field] = backup[field]
        state["step_results"] = dict(present)

        before = target.stat().st_size if target.exists() else 0
        agent._persist_state_backup(state)          # 真实落盘路径
        after = target.stat().st_size
        sizes.append(after)
        read_bytes += before                        # 先读全量再写

    final = sizes[-1] if sizes else 0
    report = backup.get("final_report") or ""
    report_bytes = len(report.encode("utf-8"))

    return {
        "session": agent.session_id[:8],
        "steps": len(sizes),
        "sizes": sizes,
        "final_bytes": final,
        "total_write": sum(sizes),
        "read_bytes": read_bytes,
        "report_bytes": report_bytes,
        "amplification": (sum(sizes) / final) if final else 0.0,
        "vs_report": (final / report_bytes) if report_bytes else 0.0,
        "backup": backup,
    }


def redundancy(backup: dict) -> dict:
    """step_results 里有多少是和独立字段重复的。"""
    step_results = backup.get("step_results", {})
    total = len(json.dumps(step_results, ensure_ascii=False).encode("utf-8"))
    dup = 0
    dup_steps = []
    for step, fields in STEP_FIELDS.items():
        item = step_results.get(str(step))
        if not item:
            continue
        content = item.get("content") or ""
        if not isinstance(content, str):
            continue
        for field in fields:
            other = backup.get(field)
            if isinstance(other, str) and other.strip() and content.strip() == other.strip():
                dup += len(content.encode("utf-8"))
                dup_steps.append(step)
                break
    return {"step_results_bytes": total, "duplicate_bytes": dup,
            "duplicate_ratio": (dup / total) if total else 0.0, "steps": dup_steps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="扫描全部备份取统计")
    ap.add_argument("--file", default="", help="只测单个文件")
    ap.add_argument("--dir", default="", help="备份目录（默认 state_backups）")
    args = ap.parse_args()

    root = Path(args.dir) if args.dir else pa.STATE_BACKUP_DIR
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        print(f"{root} 下没有备份文件")
        return
    if args.file:
        files = [Path(args.file)]
    elif not args.all:
        files = files[:1]

    original_dir = pa.STATE_BACKUP_DIR
    workdir = Path(tempfile.mkdtemp(prefix="backup_probe_"))
    pa.STATE_BACKUP_DIR = workdir
    try:
        rows = []
        for path in files:
            try:
                rows.append(replay_one(path, workdir))
            except Exception as exc:  # 单个坏文件不该让整轮探针失败
                print(f"跳过 {path.name}: {exc}")
    finally:
        pa.STATE_BACKUP_DIR = original_dir
        shutil.rmtree(workdir, ignore_errors=True)

    if not rows:
        return

    print(f"样本 {len(rows)} 份")
    print()
    print(f"{'会话':<10}{'步数':>5}{'最终KB':>9}{'总写KB':>9}{'读KB':>9}"
          f"{'写放大':>8}{'报告KB':>8}{'备份/报告':>10}")
    for r in rows:
        print(f"{r['session']:<10}{r['steps']:>5}{r['final_bytes']/1024:>9.0f}"
              f"{r['total_write']/1024:>9.0f}{r['read_bytes']/1024:>9.0f}"
              f"{r['amplification']:>7.1f}x{r['report_bytes']/1024:>8.0f}"
              f"{r['vs_report']:>9.1f}x")

    if len(rows) > 1:
        print()
        print(f"写放大中位数 {statistics.median(r['amplification'] for r in rows):.1f}x  "
              f"备份/报告中位数 {statistics.median(r['vs_report'] for r in rows):.1f}x  "
              f"总写中位数 {statistics.median(r['total_write'] for r in rows)/1024:.0f} KB")

    red = redundancy(rows[0]["backup"])
    print()
    print(f"step_results 体积 {red['step_results_bytes']/1024:.0f} KB，"
          f"其中与独立字段逐字重复 {red['duplicate_bytes']/1024:.0f} KB "
          f"（{red['duplicate_ratio']*100:.0f}%），涉及步骤 {red['steps']}")

    enc = state_backup.redundancy(rows[0]["backup"])
    print(f"编码层：{enc['naive_bytes']/1024:.0f} KB → {enc['compacted_bytes']/1024:.0f} KB"
          f"（{enc['ratio']*100:.0f}%），其中 raw_outputs 重复 "
          f"{enc['raw_outputs_bytes']/1024:.0f} KB、正文引用 "
          f"{enc['content_ref_bytes']/1024:.0f} KB")


if __name__ == "__main__":
    main()
