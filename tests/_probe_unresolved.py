# -*- coding: utf-8 -*-
"""列出某一轮里仍未落地 / 弱定位的引用原文，用于判断解析器还缺哪类锚点。

口径必须与 `_node_step7` **完全一致**，否则会得出互相矛盾的结论
（踩过：本工具自己用 `_extract_payload` 取第一段 JSON，而 `_node_step7` 走
`structured_output` 取修复后的那段，同一份备份一个说「0 条未落地」、一个说「4 条」）。

所以这里不再自己解析模型输出，而是直接跑一遍 `_node_step7`，读它写下的台账。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8")

import _probe_step7 as pr  # noqa: E402
from backend.agents import performance_agent  # noqa: E402
from backend.services import evidence_anchor as ea  # noqa: E402

session_id = sys.argv[1]
state = pr.load(session_id)
line_counts, counts_from = pr.fill_line_counts(state)
raw = state.get("indicator_analysis") or ""

replay = dict(state)
replay["indicator_score_map"] = {}
replay.pop("score_disposition", None)
agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
agent.session_id = session_id
agent.model_name = "probe-unresolved"
agent.base_agent = SimpleNamespace(llm=None)
agent._execute_step = lambda st, step: raw
agent._invoke_llm = lambda *a, **k: raw
agent._node_step7(replay)

dispo = replay.get("score_disposition") or {}
items = dispo.get("items") or {}
source_text = agent._step7_source_text(state)
hits = [h for h in (state.get("step_evidence", {}).get("7") or [])
        if isinstance(h, dict) and h.get("kind") == "project"]

print(f"备份 {session_id}")
print(f"line_counts（{counts_from}）：{line_counts}")
print(f"chunks: {[(h.get('id'), h.get('source'), h.get('locator')) for h in hits]}")
print(f"ordinals(原文): {ea.section_ordinals(source_text)}")
print(f"headings(原文): {ea.material_headings(source_text)}")
print(f"summary: {dispo.get('summary_line')}")
print("-" * 74)

buckets = {}
for key, item in items.items():
    status = item.get("status")
    if status == "accepted":
        continue
    buckets.setdefault(status, []).append((key, item))

for status in ("anchor_unresolved", "anchor_weak", "score_invalid", "reason_missing"):
    rows = buckets.get(status) or []
    if not rows:
        continue
    print(f"\n### {status}（{len(rows)} 条）")
    for key, item in rows:
        print(f"  [{key}] score={item.get('actual_score')!r} evidence={item.get('evidence')!r}")
        anchor = item.get("evidence_anchor") or {}
        if anchor:
            print(f"      anchor={anchor.get('kind')} matched={anchor.get('matched')}")
        if item.get("cited_ids"):
            print(f"      cited_ids={item.get('cited_ids')}")

print("\n" + "-" * 74)
print("分项：" + "、".join(f"{k}={len(v)}" for k, v in buckets.items()) or "（无未落地项）")
