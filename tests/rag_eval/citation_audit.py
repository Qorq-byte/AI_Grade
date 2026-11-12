"""证据引用抽读（批次 51，手工审计用，不进 CI）。

把报告里带 `[P1-x-y]` 的句子与对应证据原文并排打出来，供人判断"引用是否贴题"。
不做自动判定 —— 判"贴不贴题"需要读，机器判只会得到另一个主观意见。

用法：
    .venv/Scripts/python.exe -m tests.rag_eval.citation_audit --session <id> --limit 10
"""
import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional

from backend.agents.performance_agent import PerformanceAgent

CITE = re.compile(r"\[(P\d+-\d+-\d+)\]")
SENTENCE = re.compile(r"[^。；\n]*\[(P\d+-\d+-\d+)\][^。\n]*[。]?")


def _evidence_texts(values: dict) -> Dict[str, str]:
    texts = {}
    for hits in (values.get("step_evidence") or {}).values():
        for hit in hits:
            if hit.get("id") and hit.get("text"):
                texts.setdefault(hit["id"], hit["text"])
    return texts


def collect(values: dict, limit: int = 10) -> List[Dict]:
    report = values.get("final_report") or ""
    texts = _evidence_texts(values)
    rows, seen = [], set()
    for match in SENTENCE.finditer(report):
        cid = match.group(1)
        claim = match.group(0).strip()
        # 报告末尾的"证据索引"也是 `- [P1-x-y] 来源 · 行号`，那不是引用句
        if claim.lstrip().startswith("- [P"):
            continue
        if cid in seen or cid not in texts:
            continue
        seen.add(cid)
        rows.append({"id": cid, "claim": claim, "evidence": texts[cid]})
        if len(rows) >= limit:
            break
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="证据引用抽读")
    parser.add_argument("--session", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    values = PerformanceAgent.load_state_backup(args.session)
    if not values:
        raise SystemExit(f"会话不存在：{args.session}")
    rows = collect(values, args.limit)
    print(f"# 会话 {args.session}｜抽 {len(rows)} 条\n")
    for index, row in enumerate(rows, 1):
        print(f"[{index}] {row['id']}")
        print(f"  报告原句：{row['claim']}")
        print(f"  证据原文：{row['evidence'][:220].replace(chr(10), ' / ')}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
