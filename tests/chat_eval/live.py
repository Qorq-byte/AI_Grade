"""真模型抽读跑分器（批次 44，B5 第四步）。

与 `run.py` 的分工：
- `run.py` 是**离线**评测：只问「该给的材料进没进上下文」，不调模型，可进 CI；
- 本文件是**在线**抽读：真调模型，看回答里有没有编造编号、有没有答到点上。
  它有网络依赖、有成本、结论要人判，所以**不进 CI**，产出是一份 markdown 记录。

用法：
    .venv/Scripts/python.exe -m tests.chat_eval.live --session <session_id> --write

判定口径（能自动算的部分）：
- `fabricated`：回答里出现的引用编号（`S7P1` / `P1` / `INDICATOR_MISSING#1` 等写法），
  **不在**本次上下文的 references 里 —— 硬编造；
- `cited`：回答里出现了多少个可核对的编号；
- `refused`：回答里出现「报告里没有」这类明确拒答 —— 对素材外的问题是正确行为；
- **答得对不对仍要人读**，自动指标只负责把「编造」与「没引用」筛出来。

第一次跑时的一个教训：检测器一开始只认 `[P1]`，而模型的自然写法是 `（S11P0）`
和 `[S5P21]`，于是 10 条全被判成「未引用」——**指标错了，不是模型错了**。
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from backend.agents.performance_agent import PerformanceAgent
from backend.config import settings
from backend.routes import client_routes
from backend.services import chat_context

#: 引用编号的三种写法：`S7P1`（章节片段）、`P1`/`K2`（证据）、`CODE#1`（问题）。
# 注意顺序：长格式在前。真实证据编号是 `P1-3-1`，只匹配 `P1` 会把「真实引用」
# 误判成「编造」——第一轮就是这么误报了一条。
REF_RE = re.compile(r"(S\d{1,2}P\d{1,3}|P\d{1,3}-\d{1,3}-\d{1,3}|[PK]\d{1,3}|"
                    r"[A-Z][A-Z0-9_]{2,40}#\d{1,3})")
REFUSAL_HINTS = ("报告里没有", "报告中没有", "没有这项", "未提及", "无法从报告中")
OUTPUT = Path(__file__).resolve().parent.parent.parent / "docs" / "chat_live_eval.md"

#: 通用问题（适配任意真实报告；锚点判定留给离线评测，这里只看引用与拒答）。
LIVE_CASES = [
    {"id": "L01", "kind": "report", "question": "这个项目叫什么名字？"},
    {"id": "L02", "kind": "report", "question": "综合得分是多少？"},
    {"id": "L03", "kind": "report", "question": "扣分项在哪？"},
    {"id": "L04", "kind": "report", "question": "报告里哪一章还是空的？"},
    {"id": "L05", "kind": "report", "question": "这份报告能直接导出吗？"},
    {"id": "L06", "kind": "report", "question": "主要问题是什么？"},
    {"id": "L07", "kind": "report", "question": "有什么建议？"},
    {"id": "L08", "kind": "report", "question": "有没有合同类证据？"},
    {"id": "L09", "kind": "report", "question": "评价范围是什么？"},
    {"id": "L10", "kind": "chitchat", "question": "帮我订一张明天去北京的机票"},
]


def _ask(client, model: str, system: str, question: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": question}],
        temperature=0.3,
        max_tokens=700,
    )
    return response.choices[0].message.content or ""


def run(session_id: str, limit: int = 10, model: Optional[str] = None) -> Dict:
    values = PerformanceAgent.load_state_backup(session_id)
    if not values:
        raise SystemExit(f"会话不存在或备份为空：{session_id}")
    project = values.get("project_name") or ""
    used_model = model or settings.DEFAULT_MODEL
    client = client_routes._get_openai_client(used_model)

    rows = []
    for case in LIVE_CASES[:limit]:
        context = chat_context.build_report_context(values, project, query=case["question"])
        system = client_routes.BASE_SYSTEM_PROMPT
        if context["text"]:
            system = f"{system}\n\n{context['text']}"
        answer = _ask(client, used_model, system, case["question"])

        ref_ids = {item["id"] for item in context["references"]}
        cited = set(REF_RE.findall(answer))
        rows.append({
            "id": case["id"],
            "kind": case["kind"],
            "question": case["question"],
            "answer": answer,
            "cited": sorted(cited),
            "fabricated": sorted(cited - ref_ids),
            "refused": any(hint in answer for hint in REFUSAL_HINTS),
            "context_chars": len(context["text"]),
        })
        print(f"[{case['id']}] 引用={len(cited)} 编造={len(rows[-1]['fabricated'])} "
              f"拒答={rows[-1]['refused']}")

    report_rows = [row for row in rows if row["kind"] == "report"]
    chitchat_rows = [row for row in rows if row["kind"] == "chitchat"]
    summary = {
        "session_id": session_id,
        "model": used_model,
        "date": date.today().isoformat(),
        "total": len(rows),
        "fabricated": sum(1 for row in rows if row["fabricated"]),
        "cited_rows": sum(1 for row in report_rows if row["cited"]),
        "report_rows": len(report_rows),
        "chitchat_cited": sum(1 for row in chitchat_rows if row["cited"]),
    }
    return {"summary": summary, "rows": rows}


def render_markdown(result: Dict) -> str:
    summary = result["summary"]
    lines = [
        "# 聊天真模型抽读记录",
        "",
        f"- 日期：{summary['date']}",
        f"- 会话：`{summary['session_id']}`",
        f"- 模型：`{summary['model']}`",
        f"- 样本：{summary['report_rows']} 条通用问题 + 1 条无关请求",
        "",
        "## 汇总（自动判定）",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 编造编号（回答了上下文里不存在的编号） | **{summary['fabricated']} / {summary['total']}** |",
        f"| 报告类问题给出了可核对引用 | {summary['cited_rows']} / {summary['report_rows']} |",
        f"| 无关请求却引用了报告编号 | {summary['chitchat_cited']}（应为 0） |",
        "",
        "## 逐条",
        "",
        "| # | 问题 | 引用编号 | 编造 | 拒答 | 上下文 |",
        "|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        lines.append(f"| {row['id']} | {row['question']} | {', '.join(row['cited']) or '—'} | "
                     f"{', '.join(row['fabricated']) or '—'} | {'是' if row['refused'] else '—'} | "
                     f"{row['context_chars']} 字 |")
    lines += ["", "## 回答原文", ""]
    for row in result["rows"]:
        lines += [f"### {row['id']} {row['question']}", "", "```", row["answer"].strip(), "```", ""]
    lines += ["<!-- 人工结论请写在下面；自动指标只负责筛出编造与未引用。 -->", ""]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="聊天真模型抽读")
    parser.add_argument("--session", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--model", default=None)
    parser.add_argument("--write", action="store_true", help="写入 docs/chat_live_eval.md")
    args = parser.parse_args(argv)

    result = run(args.session, limit=args.limit, model=args.model)
    print(json.dumps(result["summary"], ensure_ascii=False))
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(render_markdown(result), encoding="utf-8")
        print(f"已写入 {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
