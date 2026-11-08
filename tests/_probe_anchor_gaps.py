"""批次 19 取证：把备份里记成「引用无法定位」的条目，用**今天的**解析器重放一遍。

为什么要重放：台账是会话当时的快照。`b20e9f85` 的注释里就写着「按强度排序」那次修复
正是针对它的 `第1段及第四部分`——用旧代码跑出来的 `anchor_unresolved` 不能当现状。

输出四个口径：
  1. 重放后仍失败 / 已可解析（说明台账过期，缺陷不成立）；
  2. 仍失败的按**解析器给出的原因**分桶（Batch 19 起 Anchor.reason）；
  3. 每一桶给一条真实样本；
  4. 若把编号池改成「本会话所有步骤下发过的编号」，桶 3 能否落地（评估跨步骤池的收益）。

用法：python tests/_probe_anchor_gaps.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from backend.agents import performance_agent as pa  # noqa: E402
from backend.services import evidence_anchor as ea  # noqa: E402

ID = re.compile(r"P\d+-\d+-\d+")
LINE = re.compile(r"第\s*(\d+)\s*行")
ORDINAL = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*部分")
DUAN = re.compile(r"第\s*(\d+)\s*段")
FILE = re.compile(r"[\w\-]+\.(?:txt|docx?|pdf|xlsx?|md)")


def inputs_for(state):
    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    material = agent._step7_material(state)
    source = agent._step7_source_text(state)
    hits = [h for h in (state.get("step_evidence") or {}).get("7") or []
            if isinstance(h, dict) and h.get("kind") == "project"]
    return {
        "evidence_ids": {h.get("id") for h in hits if h.get("id")},
        "material": material,
        "headings": ea.material_headings(source),
        "chunks": hits,
        "ordinals": ea.section_ordinals(source),
        "raw_texts": state.get("all_raw_texts") or {},
        "line_counts": state.get("raw_line_counts") or {},
    }


def crossstep_ids(state):
    pool = set()
    for hits in (state.get("step_evidence") or {}).values():
        for hit in hits or []:
            if isinstance(hit, dict) and hit.get("kind") == "project" and hit.get("id"):
                pool.add(hit["id"])
    return pool


def bucket_of(text):
    if not str(text or "").strip():
        return "empty"
    if ID.search(str(text)):
        return "id_not_in_pool"
    if LINE.search(str(text)):
        return "line_unverifiable"
    if ORDINAL.search(str(text)) or "「" in str(text):
        return "section_or_quote_unmatched"
    if DUAN.search(str(text)) or FILE.search(str(text)):
        return "too_coarse"
    return "other"


def main():
    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    still, fixed = [], []
    buckets = {}
    cross_gain = []

    for path in sorted((ROOT / "state_backups").glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        items = (state.get("score_disposition") or {}).get("items") or {}
        bad = [(k, v) for k, v in items.items() if v.get("status") == "anchor_unresolved"]
        if not bad:
            continue
        kw = inputs_for(state)
        wide = crossstep_ids(state)
        for key, item in bad:
            text = item.get("evidence")
            hit = ea.resolve(text, **kw)
            if hit.ok:
                fixed.append((path.stem[:8], key, hit.kind, str(text)[:50]))
                continue
            bucket = hit.reason or bucket_of(text)
            buckets.setdefault(bucket, []).append((path.stem[:8], key, str(text)[:50]))
            still.append((path.stem[:8], key, bucket, str(text)[:60]))
            if bucket == "id_not_in_pool":
                try:
                    again = ea.resolve(text, **{**kw, "evidence_ids": kw["evidence_ids"] | wide})
                except TypeError:
                    again = None
                if again is not None and again.ok:
                    cross_gain.append((path.stem[:8], key, again.kind, str(text)[:40]))

    print(f"台账记成「引用无法定位」共 {len(still) + len(fixed)} 条")
    print(f"用今天的解析器重放已可解析：{len(fixed)} 条 → 台账过期，不是现状缺陷")
    for row in fixed[:6]:
        print(f"    {row[0]} {row[1]} → {row[2]}  {row[3]}")
    print()
    print(f"重放后仍失败：{len(still)} 条")
    for name, rows in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {name}: {len(rows)} 条")
        for row in rows[:2]:
            print(f"      {row[0]} {row[1]}  {row[2]}")
    print()
    print(f"把编号池放宽到「本会话所有步骤下发过的编号」可额外落地：{len(cross_gain)} 条")
    for row in cross_gain:
        print(f"    {row[0]} {row[1]} → {row[2]}  {row[3]}")
    return 0 if not cross_gain else 1


if __name__ == "__main__":
    raise SystemExit(main())
