"""Step evidence from authorized uploads and the administrator-maintained knowledge base."""
import re
from pathlib import Path

from sqlalchemy import or_

from backend.models.user_models import KnowledgeBase


STEP_TERMS = {
    1: ("资料", "预算", "合同", "验收"),
    2: ("项目", "时间", "金额", "预算", "支出", "目标", "数量", "验收"),
    3: ("指标", "目标", "产出", "效益", "满意度"),
    4: ("指标", "分值", "权重", "合计"),
    5: ("评分", "标准", "扣分", "依据"),
    6: ("指标", "分值", "标准", "评价"),
    7: ("得分", "扣分", "指标", "实际", "完成", "验收"),
    8: ("经验", "措施", "成效", "做法"),
    9: ("问题", "原因", "扣分", "偏差", "未完成", "风险"),
    10: ("建议", "整改", "措施", "问题", "责任", "时限"),
    11: ("得分", "等级", "结论", "完成", "目标"),
    12: ("项目", "资金", "时间", "金额", "预算", "合同"),
    13: ("评价", "范围", "方法", "流程", "时间"),
}


def _chunks(source_id, source, text, kind):
    location = ""
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith(("【PDF 第", "=== ", "# ")):
            location = line.strip("#= ")
            continue
        for offset in range(0, len(line), 480):
            yield {
                "id": f"{source_id}-{line_no}-{offset // 480 + 1}",
                "source": source, "locator": f"{location + ' · ' if location else ''}提取文本第{line_no}行",
                "kind": kind, "text": line[offset:offset + 480].strip(),
            }


def retrieve_step_evidence(db, state, step, include_knowledge=False):
    """Return a bounded set of literal excerpts, with project evidence kept separate."""
    terms = (*STEP_TERMS.get(step, ()), state.get("indicator_type") or "")
    candidates = []
    for number, (name, content) in enumerate(state.get("all_raw_texts", {}).items(), 1):
        label = re.sub(r"^u\d+_[a-f0-9]{32}_", "", Path(name).name)
        for hit in _chunks(f"P{number}", label, content[:100_000], "project"):
            score = sum(term.casefold() in hit["text"].casefold() for term in terms if term)
            if score:
                candidates.append((score, hit))

    if include_knowledge:
        records = db.query(KnowledgeBase).filter(
            KnowledgeBase.is_active.is_(True),
            or_(KnowledgeBase.step_number == step, KnowledgeBase.step_number.is_(None)),
        ).order_by((KnowledgeBase.step_number == step).desc(), KnowledgeBase.id).limit(80)
        for record in records:
            for hit in _chunks(f"K{record.id}", record.name, record.content[:20_000], "knowledge"):
                body = f"{record.name} {record.category or ''} {hit['text']}".casefold()
                score = sum(term.casefold() in body for term in terms if term)
                if score:
                    candidates.append((score + (record.step_number == step), hit))

    seen = set()
    selected = []
    for kind, limit in (("project", 3), ("knowledge", 2)):
        count = 0
        for _, hit in sorted(candidates, key=lambda item: -item[0]):
            normalized = re.sub(r"\s+", "", hit["text"]).casefold()
            if hit["kind"] != kind or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            selected.append(hit)
            count += 1
            if count == limit:
                break
    return selected


def render_evidence(hits):
    if not hits:
        return "资料不足，当前步骤没有检索到可定位的证据。"
    return "\n".join(
        f"[{hit['id']}] {hit['source']}（{hit['locator']}，"
        f"{'项目资料' if hit['kind'] == 'project' else '通用知识'}）{hit['text']}" for hit in hits
    )


def report_evidence_index(step_evidence):
    unique = {}
    for hits in step_evidence.values():
        for hit in hits:
            unique[hit["id"]] = hit
    if not unique:
        return ""
    return "## 证据索引\n\n" + "\n".join(
        f"- [{hit['id']}] {hit['source']} · {hit['locator']}" for hit in unique.values()
    )
