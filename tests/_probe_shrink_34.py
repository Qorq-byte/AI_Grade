"""批次 17 探针：把 26 份真实备份的步骤 3 / 4 输出过一遍新闸门。

要回答三个问题：
  1. 新的树形口径能不能数出条目（改前步骤 3 恒为 0 条 → 闸门等于不存在）；
  2. 数出来的叶子数，与备份里的 `indicator_system` 是否一致（口径对不对）；
  3. 内容相同的情况下 `shrink_report` 会不会误报缩水（假阳性）。

用法：python tests/_probe_shrink_34.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from backend.agents.tools import structured_output as so  # noqa: E402


def leaves_of(payload):
    nodes = (payload or {}).get("level1_indicators")
    return len(list(so._iter_leaves(nodes))) if isinstance(nodes, list) else 0


def main():
    files = sorted((ROOT / "state_backups").glob("*.json"))
    rows = []
    blind = []
    false_positive = []
    mismatch = []

    for path in files:
        state = json.loads(path.read_text(encoding="utf-8"))
        results = state.get("step_results") or {}
        sid = path.stem[:8]

        for step in (3, 4):
            text = (results.get(str(step)) or {}).get("content") or ""
            if not text:
                continue
            outcome = so.parse(step, text)
            before = so.coverage_of_text(text, step)
            after = so.coverage_of_payload(outcome.payload, step)
            report = so.shrink_report(step, text, outcome)

            if after.entries == 0:
                blind.append((sid, step, before.entries, after.entries))
            if report.shrunk:
                false_positive.append((sid, step, report.reason))

            if step == 3:
                truth = leaves_of(state.get("indicator_system"))
                if truth and after.entries != truth:
                    mismatch.append((sid, after.entries, truth))
                rows.append((sid, step, outcome.status, before.entries, before.numeric,
                             after.entries, after.numeric, truth))
            else:
                truth = len(state.get("indicator_scores") or {})
                rows.append((sid, step, outcome.status, before.entries, before.numeric,
                             after.entries, after.numeric, truth))

    print(f"{'sid':9} {'步骤':>3} {'状态':13} {'基准条目':>7} {'基准数值':>7} "
          f"{'修复后条目':>8} {'修复后数值':>8} {'备份真值':>7}")
    for row in rows:
        print(f"{row[0]:9} {row[1]:>3} {row[2]:13} {row[3]:>7} {row[4]:>7} "
              f"{row[5]:>8} {row[6]:>8} {row[7]:>7}")

    print(f"\n步骤 3 覆盖：{sum(1 for r in rows if r[1] == 3)} 份，"
          f"步骤 4：{sum(1 for r in rows if r[1] == 4)} 份")
    print(f"数不出条目（闸门仍盲）：{blind or '无'}")
    print(f"误判缩水（假阳性）：{false_positive or '无'}")
    print(f"与备份 indicator_system 的叶子数不一致：{mismatch or '无'}")
    return 0 if not (blind or false_positive or mismatch) else 1


if __name__ == "__main__":
    raise SystemExit(main())
