"""聊天用的报告上下文与历史预算（批次 38，B5 第一步）。

聊天界面的真实后端是 `/api/client/ai/chat`，它改造前有两个硬伤：

1. **不知道用户在看哪份报告**：system 提示只有一句「你是云海睿评的AI助手」，
   用户问「我这份报告扣分点在哪」，模型只能编；
2. **历史全量塞**：`db.query(AiMessage)...all()` 把整段对话（几十轮）全发给模型，
   长对话既贵又容易把关键上下文挤出窗口。

这里只做两件纯函数的事：**把报告压成一段可引用的上下文**、**把历史压进预算**。
路由负责取数（DB / 工作流状态），本模块不碰数据库。
"""
from __future__ import annotations

from typing import Dict, List, Optional

#: 注入 system 的报告上下文上限。3000 字 ≈ 1.5k token，够放问题清单与证据索引。
MAX_CONTEXT_CHARS = 3000
#: 最多列几条一致性问题（blocking 优先）。
MAX_ISSUES = 6
#: 证据索引最多列几条。
MAX_EVIDENCE = 10
#: 历史预算：轮数与字符数取先到者。
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 6000

NO_REPORT_TEXT = ""


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_report_context(values: Optional[dict], project_name: str = "") -> Dict:
    """把工作流状态压成一段可引用的上下文。

    返回 `{text, references}`：`text` 进 system 提示，`references` 回给前端
    （用户可以点开看到"这句话是从哪条问题/哪份材料来的"）。
    """
    values = values or {}
    if not values:
        return {"text": NO_REPORT_TEXT, "references": []}

    from backend.services import consistency
    from backend.services.workflow_evidence import report_evidence_index

    name = project_name or values.get("project_name") or "未命名项目"
    completed = values.get("completed_steps") or []
    if isinstance(completed, list) and completed:
        steps_text = f"{min(completed)}–{max(completed)}（共 {len(completed)} 步）"
    else:
        steps_text = "尚未完成"

    references: List[Dict] = []
    lines = [f"【当前报告】{name}；已完成步骤：{steps_text}。"]

    try:
        report = consistency.check_consistency(values)
        issues = list(report.get("issues") or [])
    except Exception:  # noqa: BLE001
        issues = []
    # blocking 优先：这些是"导出会被拦"的结构性缺陷，也是用户最该先问的
    issues.sort(key=lambda item: (not item.get("blocking"), item.get("code") or ""))
    if issues:
        lines.append("【结构性问题】")
        for item in issues[:MAX_ISSUES]:
            flag = "阻断" if item.get("blocking") else "提示"
            lines.append(f"- [{item.get('id') or item.get('code')}·{flag}] {item.get('message', '')}")
            references.append({"id": item.get("id") or item.get("code"),
                               "label": item.get("code") or "问题",
                               "detail": item.get("message", "")})

    index = report_evidence_index(values.get("step_evidence") or {})
    if index:
        rows = [line for line in index.splitlines() if line.startswith("- ")]
        if rows:
            lines.append("【可用证据】")
            for row in rows[:MAX_EVIDENCE]:
                lines.append(row)
                raw = row[2:].strip()
                references.append({"id": raw.split("]")[0].lstrip("["),
                                   "label": "证据", "detail": raw})
    if not issues and not index:
        lines.append("【说明】这份报告还没有可引用的问题清单与证据索引。")

    lines.append("请只依据以上信息回答；问到报告里没有的内容时，直接说「报告里没有这项」，"
                 "不要编造章节号、数字或引用编号。")
    return {"text": _clip("\n".join(lines), MAX_CONTEXT_CHARS), "references": references}


def trim_history(messages: List[Dict], max_turns: int = MAX_HISTORY_TURNS,
                 max_chars: int = MAX_HISTORY_CHARS) -> List[Dict]:
    """把对话历史压进预算：先按轮次留最近 N 条，再按字符从旧到新丢。

    - system 消息永远保留在最前（它承载报告上下文）；
    - **最后一条永远保留**：那是用户刚发的问题，丢了就没法回答；
    - 丢的时候从旧到新，保证最近的上下文完整。
    """
    if not messages:
        return []
    system = [item for item in messages if item.get("role") == "system"]
    rest = [item for item in messages if item.get("role") != "system"]

    if max_turns > 0:
        rest = rest[-max_turns:]

    def size(items):
        return sum(len(str(item.get("content") or "")) for item in items)

    while len(rest) > 1 and size(system + rest) > max_chars:
        rest.pop(0)

    return system + rest
