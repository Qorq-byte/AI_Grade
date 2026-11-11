"""refine 的提示词构建与差异计算（批次 36，B4 第一步）。

改造前的问题（都在 `progress_round3.md` B36 记了取证）：

1. **不注入证据**：提示词只写「依据证据」，但证据根本没传，模型只能凭要求改写 ——
   改出来的东西和原文一样没有出处。步骤 7 早就做了证据注入，这里一直没跟上。
2. **静默截断**：`current[:8000]` 之后的正文直接消失，模型不知道、用户也不知道。
   截断本身是必要的（上下文预算），但必须留痕，并且保住结尾 ——
   报告类文本的结论、合计、落款都在尾部，只留开头等于把最该改的部分丢了。
3. **改完立刻落库**：前端没有「先看看再决定」的机会，一次误点就得再改一次。

这里只放**纯函数**（不碰 DB、不调模型），路由负责编排，测试可以逐条钉住行为。
"""
from __future__ import annotations

import difflib
from typing import Dict, List, Tuple

from backend.agents.prompts.default_prompts import STEP_META

#: 正文预算。超了走「首尾保留 + 中间标记」，不做静默截断。
MAX_CONTENT_CHARS = 8000
#: 证据预算。步骤 7 的实测是整份材料 6k 字以内全给；这里按现有证据条数截断即可。
MAX_EVIDENCE_CHARS = 3000
#: 上一步摘要预算。只给「上文说了什么」，不给全文。
MAX_NEIGHBOR_CHARS = 600


def clip(text: str, limit: int) -> Tuple[str, bool]:
    """保留首尾、中间打标记。返回 (文本, 是否截断)。

    为什么不是 `text[:limit]`：报告类文本的结论与合计都在尾部。
    为什么是 7:3：开头承载「这一章是什么」，尾部承载「结论是什么」，
    实测（人工抽读 12 份步骤正文）尾部信息密度更高，但开头不能丢。
    """
    text = text or ""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    head_len = int(limit * 0.7)
    tail_len = max(limit - head_len, 1)
    omitted = len(text) - head_len - tail_len
    return (f"{text[:head_len]}\n……（中间省略 {omitted} 字）……\n{text[-tail_len:]}", True)


def evidence_context(values: dict, step: int, limit: int = MAX_EVIDENCE_CHARS) -> Tuple[str, int]:
    """本步检索到的证据，渲染成可引用的清单。

    注意：只有**本步**的 `step_evidence`。跨步借用会让模型引到别的章节的编号，
    报告里就会出现「第四章引用了第二章的 [P3]」这种查不回去的引用。
    """
    from backend.services.workflow_evidence import render_evidence

    hits = (values.get("step_evidence") or {}).get(str(step)) or []
    if not hits:
        return "", 0
    rendered, _ = clip(render_evidence(hits), limit)
    return rendered, len(hits)


def neighbor_context(values: dict, step: int) -> str:
    """项目名 + 上一步摘要。让模型知道「这一段接在哪」。"""
    lines: List[str] = []
    project = values.get("project_name")
    if project:
        lines.append(f"项目：{project}")
    previous = (values.get("step_results") or {}).get(step - 1)
    if previous and previous.get("content"):
        summary, _ = clip(str(previous["content"]), MAX_NEIGHBOR_CHARS)
        name = STEP_META.get(step - 1, {}).get("name", f"步骤{step - 1}")
        lines.append(f"上一步（{name}）摘要：\n{summary}")
    return "\n".join(lines)


def build_prompt(values: dict, step: int, current_content: str, message: str, *,
                 content_limit: int = MAX_CONTENT_CHARS,
                 evidence_limit: int = MAX_EVIDENCE_CHARS) -> Dict:
    """拼 refine 提示词。返回 prompt 与两个可展示的事实（证据条数、是否截断）。"""
    content, truncated = clip(current_content, content_limit)
    evidence, evidence_count = evidence_context(values, step, evidence_limit)
    neighbor = neighbor_context(values, step)
    step_name = STEP_META.get(step, {}).get("name", f"步骤{step}")

    parts = [f"依据证据及人工要求修订步骤 {step}（{step_name}），保留来源编号。"
             "无法证实的事实标注「待核实」，不要编造引用编号。"]
    if neighbor:
        parts.append(f"【上下文】\n{neighbor}")
    if evidence:
        parts.append(f"【本步证据】\n{evidence}")
    else:
        parts.append("【本步证据】\n资料不足：本步骤没有检索到可定位的证据。"
                     "只做文字与结构上的修订，不要新增事实性数据。")
    parts.append(f"【原文】\n{content}")
    parts.append(f"【修订要求】\n{message}")
    if step == 3:
        parts.append("只输出结构化指标 JSON 对象。")
    else:
        parts.append("输出修订后的完整内容，不要解释。")

    return {"prompt": "\n\n".join(parts), "evidence_count": evidence_count, "truncated": truncated}


def diff_summary(before: str, after: str, max_lines: int = 80) -> Dict:
    """给前端做「改了什么」的最小差异。

    不引入 diff 库：标准库 `difflib` 足够，且输出结构稳定（add/del/ctx/hunk 四态）。
    行数上限是为了别把一份 3 万字的报告整篇塞进响应。
    """
    rows: List[Dict[str, str]] = []
    added = removed = 0
    for line in difflib.unified_diff((before or "").splitlines(),
                                     (after or "").splitlines(), lineterm="", n=1):
        if line.startswith("@@"):
            rows.append({"op": "hunk", "text": line})
        elif line.startswith("+"):
            rows.append({"op": "add", "text": line[1:]})
            added += 1
        elif line.startswith("-"):
            rows.append({"op": "del", "text": line[1:]})
            removed += 1
        else:
            rows.append({"op": "ctx", "text": line[1:]})
    truncated = len(rows) > max_lines
    return {"added": added, "removed": removed, "lines": rows[:max_lines], "truncated": truncated}


def build_model(model_name: str, temperature: float = 0.3):
    """按名字造一个客户端（主模型与备选共用）。

    单独成函数是为了让路由与测试都只依赖这一个入口：
    测试打桩这里就能完全绕开真模型，不需要网络与密钥。
    """
    from backend.agents.base_agent import build_llm_for

    return build_llm_for(model_name, temperature, 0.9)
