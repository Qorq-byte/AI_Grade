"""多模型候选的确定性评分与择优（批次 40，B6 第一步）。

为什么不用模型来评：一是成本（候选 N 个，再评一轮就是 N+1 次调用），
二是**没有客观真值** —— 让模型判「哪段更好」，得到的只是另一个模型的偏好。
这里只用可复算的代理指标：引用能不能定位、指标有没有覆盖、结构是否完整、篇幅是否合理。
分数低不代表写得差，但**分数高的至少是可核对的**。

对应改造前的两个真问题（取证见 `progress_round3.md` B40）：
- 多模型分支 `return next(iter(outputs.values()))` —— 永远取第一个；
- `_record_step_result` 用 `{"default": content}` 覆盖 `raw_outputs`，候选当场丢失。
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional

#: 权重：引用可定位 > 指标覆盖 > 结构 > 篇幅。
WEIGHT_EVIDENCE = 30
WEIGHT_INDICATORS = 25
WEIGHT_STRUCTURE = 20
WEIGHT_LENGTH = 15

#: 引用编号的形态：`[P1]` / `[K12]` / `[E3]`。
_REF = re.compile(r"\[([A-Za-z]{1,2}\d{1,3})\]")
#: 标题行（与 chat_context 的启发式保持一致）。
_HEADING = re.compile(r"^\s*(#{1,4}\s|[一二三四五六七八九十]+、|（[一二三]+）|\d+[.、])", re.M)
#: 段落差异的展示上限：再多前端也读不完，而且会把响应撑大。
MAX_DIFF_ROWS = 60
MAX_DIFF_CHARS = 400
#: 相似度低于这个值就不算「改过」，拆成「只在左 / 只在右」两行。
#: 0.4 是拍的：实测「换了个小标题」这类整段替换的相似度在 0.1–0.3，
#: 而「同一句话改了几个数字」在 0.6 以上。
SIMILARITY_THRESHOLD = 0.4

#: 段落差异的展示上限：再多前端也读不完，而且会把响应撑大。
MAX_DIFF_ROWS = 60
MAX_DIFF_CHARS = 400
#: 相似度低于这个值就不算「改过」，拆成「只在左 / 只在右」两行。
#: 0.4 是拍的：实测「换了个小标题」这类整段替换的相似度在 0.1–0.3，
#: 而「同一句话改了几个数字」在 0.6 以上。
SIMILARITY_THRESHOLD = 0.4

#: 合理的正文篇幅区间（字）。太短是没写完，太长通常是复述资料。
MIN_CHARS = 300
MAX_CHARS = 4000


def entry_for(values: dict, step: int) -> Optional[dict]:
    """取某一步的对比记录。备份里的键是字符串，内存里是整数，两种都要认。"""
    history = values.get("comparison_history") or {}
    for key in (step, str(step)):
        entry = history.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def candidates_of(values: dict, step: int) -> Dict[str, str]:
    """候选正文：优先 comparison_history，其次 step_results 的 raw_outputs。"""
    entry = entry_for(values, step) or {}
    outputs = entry.get("individual_outputs") or {}
    if outputs:
        return {str(model): str(text) for model, text in outputs.items() if isinstance(text, str)}
    result = (values.get("step_results") or {}).get(step) or {}
    raw = result.get("raw_outputs") or {}
    return {str(model): str(text) for model, text in raw.items()
            if isinstance(text, str) and model != "default"}


def _evidence_ids(values: dict, step: int) -> set:
    hits = (values.get("step_evidence") or {}).get(str(step)) or []
    return {str(item.get("id")) for item in hits if item.get("id")}


def _indicator_names(values: dict) -> List[str]:
    from backend.agents.tools.questionnaire_gen import indicator_leaves

    system = values.get("indicator_system") or {}
    try:
        # indicator_leaves 是生成器：必须 list() 才会真正执行，否则异常逃出 try
        leaves = list(indicator_leaves(system.get("level1_indicators") or []))
    except Exception:  # noqa: BLE001
        leaves = []
    names = [str(item.get("name") or "").strip() for item in leaves]
    return [name for name in names if len(name) >= 3]


def score_candidate(text: str, values: dict, step: int) -> Dict:
    """给一个候选打分（0–100）并给出理由。纯函数，可复算、可断言。"""
    text = text or ""
    reasons: List[str] = []
    score = 0.0

    # 1) 引用可定位：文本里出现的编号必须真的在本步证据里
    valid = _evidence_ids(values, step)
    refs = _REF.findall(text)
    if not refs:
        reasons.append("未引用证据编号")
    else:
        hit = sum(1 for ref in refs if ref in valid)
        ratio = hit / len(refs)
        score += WEIGHT_EVIDENCE * ratio
        reasons.append(f"引用 {hit}/{len(refs)} 条可定位")

    # 2) 指标覆盖：正文提到的指标名（覆盖率按「至少覆盖三成」给满）
    names = _indicator_names(values)
    if names:
        covered = sum(1 for name in names if name in text)
        ratio = min(1.0, covered / max(1.0, len(names) * 0.3))
        score += WEIGHT_INDICATORS * ratio
        reasons.append(f"覆盖 {covered}/{len(names)} 个指标")
    else:
        reasons.append("没有可比对的指标名")

    # 3) 结构：标题与段落
    headings = len(_HEADING.findall(text))
    paragraphs = len([item for item in re.split(r"\n{2,}", text) if item.strip()])
    structure = min(1.0, (headings * 0.15) + (paragraphs * 0.1))
    score += WEIGHT_STRUCTURE * structure
    reasons.append(f"{headings} 个小标题 / {paragraphs} 段")

    # 4) 篇幅：区间内给满，过短过长都扣
    chars = len(text)
    if MIN_CHARS <= chars <= MAX_CHARS:
        length_ratio = 1.0
    elif chars < MIN_CHARS:
        length_ratio = chars / MIN_CHARS
    else:
        length_ratio = max(0.0, 1.0 - (chars - MAX_CHARS) / MAX_CHARS)
    score += WEIGHT_LENGTH * length_ratio
    reasons.append(f"{chars} 字")

    return {"score": round(score, 1), "reasons": reasons, "chars": chars}


def rank(values: dict, step: int) -> List[Dict]:
    """给所有候选排序。**禁止默认取第一个**：分数相同就没有推荐。

    返回 `[{model, score, reasons, chars}]`，按分数降序；分数并列时保持模型名排序
    （稳定），但 `recommended` 只在**严格领先**时给出 —— 并列还硬推一个，
    等于把「先写的赢」换成「名字靠前的赢」。
    """
    outputs = candidates_of(values, step)
    scored = []
    for model in sorted(outputs):
        item = score_candidate(outputs[model], values, step)
        scored.append({"model": model, **item})
    scored.sort(key=lambda item: (-item["score"], item["model"]))
    if len(scored) >= 2 and scored[0]["score"] <= scored[1]["score"]:
        for item in scored:
            item["recommended"] = False
        return scored
    for index, item in enumerate(scored):
        item["recommended"] = index == 0
    return scored


def auto_pick(values: dict, step: int) -> Optional[Dict]:
    """工作流继续跑时的默认选择：排名第一（并列时取名字序第一，稳定但标注非推荐）。"""
    ranked = rank(values, step)
    if not ranked:
        return None
    return {"model": ranked[0]["model"], "score": ranked[0]["score"],
            "recommended": bool(ranked[0].get("recommended")), "ranked": ranked}

def paragraphs(text: str) -> List[str]:
    """切段落：先按空行，空行切不出来再按行。

    与 `chat_context` 的切块规则不同 —— 那边要按标题分组（为了检索），
    这边要的是**逐段对齐**，标题单独成段更容易看出「哪一段被换了」。
    """
    text = text or ""
    parts = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
    if len(parts) <= 1:
        parts = [line.strip() for line in text.splitlines() if line.strip()]
    return parts


def _clip(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def diff_paragraphs(left_text: str, right_text: str,
                    max_rows: int = MAX_DIFF_ROWS) -> Dict:
    """按段落对齐两个候选，标出 same / changed / only_left / only_right。

    为什么不是行级 diff：两段长文逐行比，会得到几百行噪音（换行位置一改全红）。
    段落级对齐 + 相似度阈值，能回答用户真正的问题：「他比它多了什么、少了什么」。
    """
    left, right = paragraphs(left_text), paragraphs(right_text)
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    rows: List[Dict] = []
    stats = {"same": 0, "changed": 0, "only_left": 0, "only_right": 0}

    def add(kind: str, li=None, ri=None, text: str = "", similarity=None) -> None:
        stats[kind] += 1
        rows.append({"kind": kind, "left_index": li, "right_index": ri,
                     "text": _clip(text), "similarity": similarity})

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                add("same", i1 + offset, j1 + offset, left[i1 + offset])
        elif tag == "replace":
            for offset in range(max(i2 - i1, j2 - j1)):
                li = i1 + offset if i1 + offset < i2 else None
                ri = j1 + offset if j1 + offset < j2 else None
                if li is not None and ri is not None:
                    ratio = round(difflib.SequenceMatcher(
                        a=left[li], b=right[ri], autojunk=False).ratio(), 3)
                    if ratio >= SIMILARITY_THRESHOLD:
                        add("changed", li, ri, right[ri], ratio)
                    else:
                        # 整段换了内容：不是「改过」，是「这段没了 + 那段是新的」
                        add("only_left", li, None, left[li])
                        add("only_right", None, ri, right[ri])
                elif li is not None:
                    add("only_left", li, None, left[li])
                else:
                    add("only_right", None, ri, right[ri])
        elif tag == "delete":
            for offset in range(i2 - i1):
                add("only_left", i1 + offset, None, left[i1 + offset])
        else:  # insert
            for offset in range(j2 - j1):
                add("only_right", None, j1 + offset, right[j1 + offset])

    truncated = len(rows) > max_rows
    return {"rows": rows[:max_rows], "stats": stats, "truncated": truncated,
            "left_paragraphs": len(left), "right_paragraphs": len(right)}
