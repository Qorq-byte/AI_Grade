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

import re
from typing import Dict, List, Optional, Tuple

#: 注入 system 的报告上下文上限。3000 字 ≈ 1.5k token，够放问题清单与证据索引。
MAX_CONTEXT_CHARS = 3000
#: 最多列几条一致性问题（blocking 优先）。
MAX_ISSUES = 6
#: 证据索引最多列几条。
MAX_EVIDENCE = 10
#: 历史预算：轮数与字符数取先到者。
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 6000
#: 按问题检索章节片段（批次 39）：最多几段、每段多少字、整份报告最多扫多少字。
MAX_SNIPPETS = 3
MAX_SNIPPET_CHARS = 400
MAX_REPORT_CHARS = 60_000
#: 章节标题的形态：`一、` / `（一）` / `##` / `1.`。用它把正文切成「标题 + 正文」的块。
HEADING_RE = re.compile(r"^\s*(#{1,4}\s|[一二三四五六七八九十]+、|（[一二三]+）|\d+[.、])")

NO_REPORT_TEXT = ""


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_report_context(values: Optional[dict], project_name: str = "",
                         query: str = "") -> Dict:
    """把工作流状态压成一段可引用的上下文。

    `query` 是用户当前的问题：给了就按它检索章节片段（批次 39），
    这样「第四章怎么写的」也能拿到可定位的原文，而不是只能答"问题清单"。

    返回 `{text, references}`：`text` 进 system 提示，`references` 回给前端
    （用户可以点开看到"这句话是从哪条问题/哪份材料/哪一段来的"）。
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
    lines = [
        f"【当前报告】{name}；已完成步骤：{steps_text}。",
        # 指令放在**顶部**：整体截断时不能被吃掉，否则模型会开始编
        "请只依据以下信息回答；问到报告里没有的内容时，直接说「报告里没有这项」，"
        "不要编造章节号、数字或引用编号。",
        # 不要在指令里写具体编号当例子：模型会把它当成"可用的编号"直接引用
        "回答报告相关问题时，每条结论后用（编号）标注来源；编号必须与上面列出的编号逐字一致，"
        "没有编号支撑的话不要写成结论，也不要自造、改写或补全编号。",
        "与报告无关的请求，只说明不在服务范围内即可，不要展开报告内容、不要引用编号。",
    ]

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

    snippets = retrieve_chapter_snippets(values, query) if query else []
    if snippets:
        lines.append("【相关章节】")
        for snippet in snippets:
            lines.append(f"- [{snippet['id']}] {snippet['chapter']} · {snippet['locator']}："
                         f"{snippet['text']}")
            references.append({
                "id": snippet["id"],
                "label": snippet.get("label") or f"步骤{snippet['step']}·{snippet['locator']}",
                "detail": snippet["text"][:120],
                "step": snippet["step"], "chapter": snippet["chapter"],
                "offset": snippet["offset"],
            })

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
    if not issues and not index and not snippets:
        lines.append("【说明】这份报告还没有可引用的章节、问题清单与证据索引。")
    return {"text": _clip("\n".join(lines), MAX_CONTEXT_CHARS), "references": references}


def _paragraphs(text: str) -> List[Tuple[int, str]]:
    """按空行切段，返回 (偏移, 文本)。偏移用于「点开到第几段」。"""
    out: List[Tuple[int, str]] = []
    start = 0
    for match in re.finditer(r"\n{2,}", text):
        raw = text[start:match.start()]
        stripped = raw.strip()
        if stripped:
            out.append((start + raw.index(stripped), stripped))
        start = match.end()
    raw = text[start:]
    stripped = raw.strip()
    if stripped:
        out.append((start + raw.index(stripped), stripped))
    return out


def _window(offset: int, text: str, size: int) -> List[Tuple[int, str]]:
    """超长段落按 size 切窗（尽量落在换行处），偏移仍然指向原文。"""
    out: List[Tuple[int, str]] = []
    pos = 0
    while pos < len(text):
        raw = text[pos:pos + size]
        cut = raw.rfind("\n")
        if cut >= size // 2:
            raw = raw[:cut + 1]
        pos += max(len(raw), 1)
        piece = raw.strip()
        if piece:
            out.append((offset + pos - len(raw) + raw.index(piece), piece))
    return out


def _blocks(text: str, target: int = MAX_SNIPPET_CHARS) -> List[Tuple[int, str]]:
    """把一章切成「标题 + 正文」的块。标题开新块，过长再切窗。"""
    sections: List[List[Tuple[int, str]]] = []
    current: List[Tuple[int, str]] = []
    for offset, para in _paragraphs(text):
        if current and HEADING_RE.match(para.splitlines()[0]):
            sections.append(current)
            current = []
        current.append((offset, para))
    if current:
        sections.append(current)

    blocks: List[Tuple[int, str]] = []
    for section in sections:
        buf: List[str] = []
        start = None
        for offset, para in section:
            if len(para) > target * 2:
                if buf:
                    blocks.append((start, "\n\n".join(buf)))
                    buf, start = [], None
                blocks.extend(_window(offset, para, target))
                continue
            if start is None:
                start = offset
            buf.append(para)
            if sum(len(item) for item in buf) >= target:
                blocks.append((start, "\n\n".join(buf)))
                buf, start = [], None
        if buf:
            blocks.append((start, "\n\n".join(buf)))
    return [(offset, text.strip()) for offset, text in blocks if text.strip()]


def _corpus(values: dict, max_chars: int):
    """检索语料：**报告正文**（主）+ 分步正文（辅）。

    为什么两个都要：
    - 报告是拼装后的产物，有些内容只在报告里（比如「评价范围」那一章，
      对应步骤的正文可能是空的）；
    - 分步正文里有报告没有的细节（表格、JSON 片段、扣分台账）。
    只索引其中一个，都会出现「明明有，却答没有」。
    """
    from backend.agents.prompts.default_prompts import STEP_META

    chunks, meta = [], {}
    budget = 0

    report = values.get("final_report") or ""
    if isinstance(report, str) and report.strip():
        heading = ""
        for index, (offset, text) in enumerate(_blocks(report, max_chars)):
            first = text.splitlines()[0].strip()
            if HEADING_RE.match(first):
                heading = first.lstrip("#").strip()[:24]
            chunk_id = f"R{index}"
            chunks.append({"id": chunk_id, "source": heading or "报告", "locator": f"第{index + 1}段",
                           "kind": "report", "text": _clip(text, max_chars), "step_number": None})
            meta[chunk_id] = {"step": None, "chapter": heading or "报告正文", "offset": offset,
                              "locator": f"第{index + 1}段",
                              "label": f"报告·{heading or '正文'}·第{index + 1}段"}
            budget += len(text)
            if budget >= MAX_REPORT_CHARS:
                break

    results = values.get("step_results") or {}
    for key in sorted(results, key=lambda item: int(item)):
        if budget >= MAX_REPORT_CHARS:
            break
        item = results[key]
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str) or not content.strip():
            continue
        step = int(key)
        chapter = STEP_META.get(step, {}).get("name", f"步骤{step}")
        for index, (offset, text) in enumerate(_blocks(content, max_chars)):
            if budget >= MAX_REPORT_CHARS:
                break
            chunk_id = f"S{step}P{index}"
            chunks.append({"id": chunk_id, "source": chapter, "locator": f"第{index + 1}段",
                           "kind": "report", "text": _clip(text, max_chars), "step_number": step})
            meta[chunk_id] = {"step": step, "chapter": chapter, "offset": offset,
                              "locator": f"第{index + 1}段",
                              "label": f"步骤{step}·第{index + 1}段"}
            budget += len(text)
    return chunks, meta


#: 高频同义词组：只用于**查询扩展**，不改语料原文（批次 48）。
#: 为什么是"扩查询"而不是"改写语料"：snippet 要原样给模型看，
#: 改写会让模型引用到"报告里并不存在的词"。
SYNONYM_GROUPS = (
    ("方法", "方式", "手段", "做法"),
    ("资金", "经费", "款项"),
    ("建议", "意见", "对策"),
    ("问题", "缺陷", "不足", "短板"),
    ("证据", "佐证", "材料", "凭证"),
    ("完成率", "达成率", "完成比例"),
    ("满意度", "满意程度"),
    ("投资", "投资额", "总投资"),
    ("得分", "分值", "分数"),
    ("等级", "评级", "档次"),
    ("范围", "边界"),
    ("工期", "期限"),
    ("验收", "交付"),
    ("立项", "批复"),
)


def expand_query(query: str) -> str:
    """在查询后追加同组词，让「方法/方式」这类写法差异能互相命中。

    实测漏掉的那条：问「评价用了哪些方法」，正文写「……相结合的方式」。
    """
    query = query or ""
    extra = []
    for group in SYNONYM_GROUPS:
        if any(term in query for term in group):
            extra.extend(term for term in group if term not in query)
    if not extra:
        return query
    return f"{query} {' '.join(dict.fromkeys(extra))}"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def retrieve_chapter_snippets(values: Optional[dict], query: str,
                              limit: int = MAX_SNIPPETS,
                              max_chars: int = MAX_SNIPPET_CHARS) -> List[Dict]:
    """按问题在**报告 + 分步正文**里找最相关的几段（BM25，中文二元组）。

    为什么不用整章：一章动辄几千字，塞进 3000 字预算会把问题清单和证据挤掉；
    只给命中的片段，模型才能回答「第四章怎么写的」而不跑题。

    报告由步骤正文拼成，同一段会在两个源里各出现一次 —— 命中后按文本去重，
    否则前 3 个名额可能被同一段话占掉两个。
    """
    query = (query or "").strip()
    values = values or {}
    if not query:
        return []

    from backend.services import retrieval

    chunks, meta = _corpus(values, max_chars)
    if not chunks:
        return []

    hits = retrieval.build_index(chunks).search(expand_query(query),
                                                limit=max(limit * 3, limit))
    picked: List[Dict] = []
    seen: List[str] = []
    for score, chunk in hits:
        key = _normalize(chunk.text)
        if any(key and (key in prev or prev in key) for prev in seen):
            continue
        info = meta[chunk.id]
        seen.append(key)
        picked.append({
            "id": chunk.id, "step": info["step"], "chapter": info["chapter"],
            "locator": info["locator"], "offset": info["offset"],
            "label": info["label"], "text": chunk.text, "score": round(score, 4),
        })
        if len(picked) >= limit:
            break
    return picked


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
