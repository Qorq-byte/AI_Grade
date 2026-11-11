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
