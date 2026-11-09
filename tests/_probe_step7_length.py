"""步骤 7 输出长度取证：量每份备份里步骤 7 的原始输出有多长、JSON 是否闭合。

不做任何修改，只打印一张表。判定「截断」的三个互相独立的证据：
  1. 原始输出末尾明显断在半句（最后一个 `}` 之后还有非空正文 = 模型把分析写在 JSON 后面）；
  2. `structured_output.extract_json` 取出的块 `json.loads` 失败；
  3. 输出里出现的指标 id 数 < 应评条目数（只写了一半）。

用法：
    NO_PROXY='*' .venv/Scripts/python.exe -m tests._probe_step7_length
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.tools import structured_output  # noqa: E402

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state_backups")


def load_backups():
    rows = []
    for name in sorted(os.listdir(BACKUP_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {name}: {exc}")
            continue
        rows.append((name[:-5], data))
    return rows


def step7_raw(data):
    """取步骤 7 的原始输出。优先 step_results（原文），退回 indicator_analysis。"""
    results = data.get("step_results") or {}
    node = results.get("7") or results.get(7) or {}
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, str) and content.strip():
            return content
    analysis = data.get("indicator_analysis")
    return analysis if isinstance(analysis, str) else ""


def measured(sid, data):
    raw = step7_raw(data)
    if not raw.strip():
        return None
    audit = structured_output.audit_step7_length(raw)
    disposition = data.get("score_disposition") or {}
    counts = disposition.get("counts") or {}
    return {
        "sid": sid,
        "chars": audit.total_chars,
        "json_chars": audit.json_chars,
        "prose_chars": audit.prose_chars,
        "json_first": audit.json_first,
        "json_ok": audit.json_parsed,
        "items": audit.items,
        "over": audit.reason_over_limit,
        "rmax": audit.reason_max_chars,
        "accepted": counts.get("accepted", 0),
        "total": disposition.get("total", 0),
    }


def main():
    rows = load_backups()
    table = []
    for sid, data in rows:
        row = measured(sid, data)
        if row:
            table.append(row)
    table.sort(key=lambda r: r["chars"], reverse=True)
    print(f"{'session':10} {'chars':>7} {'json':>6} {'prose':>6} {'first':>6} {'jsonok':>6} "
          f"{'items':>6} {'over':>5} {'rmax':>5} {'acc':>4} {'total':>5}  {'rate':>5}")
    for row in table:
        rate = f"{row['accepted'] / row['total']:.0%}" if row["total"] else "-"
        print(f"{row['sid'][:8]:10} {row['chars']:>7} {row['json_chars']:>6} {row['prose_chars']:>6} "
              f"{'yes' if row['json_first'] else 'NO':>6} {'ok' if row['json_ok'] else 'FAIL':>6} "
              f"{row['items']:>6} {row['over']:>5} {row['rmax']:>5} {row['accepted']:>4} "
              f"{row['total']:>5}  {rate:>5}")

    over = [r for r in table if r["chars"] >= 8000]
    broken = [r for r in table if not r["json_ok"]]
    prose_first = [r for r in table if not r["json_first"]]
    long_prose = [r for r in table if r["prose_chars"] > structured_output.STEP7_PROSE_CHAR_LIMIT]
    print()
    print(f"有步骤 7 输出的会话：{len(table)}")
    print(f"≥8,000 字：{len(over)} -> {[ (r['sid'][:8], r['chars']) for r in over ]}")
    print(f"JSON 不可解析：{len(broken)} -> {[ (r['sid'][:8], r['chars']) for r in broken ]}")
    print(f"JSON 不在最前：{len(prose_first)}/{len(table)}")
    print(f"正文超过 {structured_output.STEP7_PROSE_CHAR_LIMIT} 字：{len(long_prose)}/{len(table)}")
    if table:
        print(f"最长 / 中位：{table[0]['chars']} / {table[len(table)//2]['chars']}")


if __name__ == "__main__":
    main()
