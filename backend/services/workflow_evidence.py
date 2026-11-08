"""Step evidence from authorized uploads and the administrator-maintained knowledge base.

排序方式（Batch 7 起）：BM25 + 中文二元组，查询由「步骤意图」构成。
改造前是「步骤固定词表命中计数」，两个硬伤：长句套话命中词多所以排前面；同分时按原文顺序，
等于把相关性交给了文件排列。基线（tests/rag_eval）测得 Recall@3 = 0.172、查询无关性 = 1.0。

保持不变的部分（有意为之）：
  - 返回条数仍是「项目资料 3 条 + 知识库 2 条」——本批只改排序，不改预算，
    这样基线对比才是干净的；条数是下一个可单独测量的杠杆（见 PROJECT_LIMIT）。
  - 返回结构仍是 {id, source, locator, kind, text}，调用方与前端无需改动。
  - 按 kind 去重、按规范化文本去重、证据编号规则全部沿用。
"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_

from backend.models.user_models import KnowledgeBase
from backend.services import retrieval

# 返回条数。只改排序、不改预算，基线的提升才归因于排序本身。
PROJECT_LIMIT = 3
KNOWLEDGE_LIMIT = 2
# 单份文件进入索引的最大字符数。超出部分不再静默消失——会写进 warnings。
# 原来的 100_000 是无提示截断，与 Batch 5 修的是同一类问题。
MAX_INDEX_CHARS_PER_DOC = 300_000
INDEX_CACHE_SIZE = 4
# 步骤词表保留为「排序上的轻微偏好」，权重远小于 BM25，不再决定排序。
STEP_TERM_BOOST = 1.05
# 线上是否启用「多取证面软融合」。关掉即退回 Batch 7 的单查询排序，便于 A/B 与回滚。
USE_FACET_PLAN = True
# 各取证面在融合分里的权重。1.0 会让面分与基础分等权，容易被面词带偏，故取 0.5。
FACET_WEIGHT = 0.5

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

# 每步「这一步到底要找什么」——原来被当成打分项的那组词，现在改成检索的查询句。
STEP_QUERIES = {
    1: "项目资料清单 已提供文件 缺失资料 合同 验收报告 立项批复",
    2: "项目总投资 投资估算 资金来源 合同价款 工期 支出进度 预算执行率 实际完成 满意度",
    3: "绩效目标 产出指标 效益指标 满意度目标 完成率 服务人口",
    4: "指标分值 权重 分值合计 评分细则",
    5: "评分标准 扣分标准 违约金 付款比例 变更审批 质量验收标准",
    6: "评价表 指标得分 评分依据 权重",
    7: "实际完成 未完成 遗留问题 整改 扣分 验收结论 满意度得分",
    8: "经验做法 可复制 管理措施 成效 例会 旁站",
    9: "问题 原因 进度滞后 凭证缺失 未整改 审计 风险",
    10: "整改建议 责任主体 整改时限 措施 落实",
    11: "总分 等级 结论 自评得分 目标完成 综合评价",
    12: "项目基本情况 项目单位 批复文号 建设内容 资金来源 建设地点",
    13: "评价范围 评价方法 时间安排 验收时间 组织形式",
    14: "最终报告 结论 责任单位 整改要求 数据来源",
}

# 每步三个「取证面」。证据预算只有 3 条，让 3 个不同的面各占一条，
# 比让一个笼统的查询句把关——Batch 7 的 2×2 诊断说明：查询是瓶颈，不是排序。
# 这三个面按绩效评价的通用结构写（资金 / 实施 / 结果），不针对某份具体资料，
# 换一份语料仍然成立——这一点很重要，否则就是对着评估集调参。
STEP_SUBQUERIES = {
    1: ("立项批复 可行性研究报告 批复文号", "合同 中标通知书 招标投标", "验收报告 检测报告 档案 清单"),
    2: ("总投资 投资估算 资金来源 财政拨款 专项债", "合同价款 工期 开工 竣工 参建单位", "累计支付 支出进度 预算执行率 实际完成 满意度"),
    3: ("绩效目标 产出指标 效益指标", "完成率 服务人口 覆盖范围 建设规模", "满意度目标 质量目标 时限要求"),
    4: ("分值 权重 评分细则 计算方法", "指标 评分标准 依据 等级", "扣分 加分 合计 满分"),
    5: ("评分标准 扣分标准 得分区间", "违约金 违约责任 合同条款", "付款比例 支付条件 变更审批 验收标准"),
    6: ("评价表 指标得分 评分依据", "质量评定 验收结论 合格", "权重 分值 等级 合计"),
    7: ("实际完成 完成率 未完成 差异", "遗留问题 整改 缺陷 返工", "扣分 得分 满意度 验收"),
    8: ("经验做法 管理措施 成效 亮点", "例会 旁站 检查 制度 台账", "可复制 推广 培训 标准化"),
    9: ("问题 原因 分析 风险", "进度滞后 延期 征地拆迁 协调", "凭证缺失 计量 审计 违规 不规范"),
    10: ("整改建议 措施 责任主体 整改时限", "落实 台账 闭环 跟踪", "预算执行 资金 支付 管理"),
    11: ("总分 得分 等级 结论", "自评 目标完成 综合评价", "成效 问题 建议"),
    12: ("项目名称 项目单位 批复文号 建设地点", "建设内容 规模 投资 工程量", "资金来源 工期 参建单位 验收"),
    13: ("评价范围 评价方法 组织形式", "时间安排 工作进度 阶段", "验收 时点 基准日 资料依据"),
    14: ("结论 等级 总分 评价意见", "问题 建议 责任单位 整改要求", "数据来源 依据 附件 出处"),
}

_INDEX_CACHE: Dict[tuple, retrieval.BM25Index] = {}


def _chunks(source_id, source, text, kind, step_number=None):
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
                "step_number": step_number,
            }


def _note_truncation(state, name, truncated_chars):
    """索引截断必须可见：写进 warnings（同一条只写一次）。"""
    if not truncated_chars or state is None:
        return
    noted = state.setdefault("_retrieval_truncated_files", [])
    if name in noted:
        return
    noted.append(name)
    state.setdefault("warnings", []).append(
        f"资料「{name}」超过 {MAX_INDEX_CHARS_PER_DOC} 字，其后 {truncated_chars} 字未建立检索索引，"
        "相关事实可能检索不到"
    )


def _project_chunks(state, name_filter=None) -> List[dict]:
    chunks: List[dict] = []
    for number, (name, content) in enumerate((state.get("all_raw_texts") or {}).items(), 1):
        label = re.sub(r"^u\d+_[a-f0-9]{32}_", "", Path(name).name)
        text = content or ""
        if len(text) > MAX_INDEX_CHARS_PER_DOC:
            _note_truncation(state, label, len(text) - MAX_INDEX_CHARS_PER_DOC)
            text = text[:MAX_INDEX_CHARS_PER_DOC]
        chunks.extend(_chunks(f"P{number}", label, text, "project"))
    return chunks


def _knowledge_chunks(db, step) -> List[dict]:
    if db is None:
        return []
    records = db.query(KnowledgeBase).filter(
        KnowledgeBase.is_active.is_(True),
        or_(KnowledgeBase.step_number == step, KnowledgeBase.step_number.is_(None)),
    ).order_by((KnowledgeBase.step_number == step).desc(), KnowledgeBase.id).limit(80)
    chunks: List[dict] = []
    for record in records:
        for hit in _chunks(f"K{record.id}", record.name, record.content[:20_000], "knowledge",
                           step_number=record.step_number):
            chunks.append(hit)
    return chunks


def _cached_index(key: tuple, chunks: List[dict]) -> retrieval.BM25Index:
    index = _INDEX_CACHE.get(key)
    if index is None:
        index = retrieval.build_index(chunks)
        if len(_INDEX_CACHE) >= INDEX_CACHE_SIZE:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
        _INDEX_CACHE[key] = index
    return index


def clear_index_cache():
    _INDEX_CACHE.clear()


def step_query(state, step: int, extra: str = "") -> str:
    """步骤意图 → 检索查询句。没有 query 参数的调用方走这里。"""
    base = STEP_QUERIES.get(step) or STEP_QUERIES.get(2, "")
    parts = [base, extra or "", (state or {}).get("indicator_type") or ""]
    return " ".join(part for part in parts if part).strip()


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _score_map(index: retrieval.BM25Index, query: str, step: int,
               limit: int = 60) -> Dict[str, Tuple[float, retrieval.IndexedChunk]]:
    """一次查询 → {块id: (分数, 块)}，含步骤偏好与知识库亲和。"""
    terms = STEP_TERMS.get(step, ())
    out: Dict[str, Tuple[float, retrieval.IndexedChunk]] = {}
    for score, chunk in index.search(query, limit=limit):
        body = chunk.text.casefold()
        if any(term.casefold() in body for term in terms):
            score *= STEP_TERM_BOOST
        if chunk.kind == "knowledge" and chunk.step_number == step:
            score *= 1.1
        previous = out.get(chunk.id)
        if previous is None or score > previous[0]:
            out[chunk.id] = (score, chunk)
    return out


def _ordered(mapping: Dict[str, Tuple[float, retrieval.IndexedChunk]]):
    return sorted(mapping.values(), key=lambda item: (-item[0], item[1].id))


def _pick(ordered_items, project_limit: int, knowledge_limit: int) -> List[dict]:
    chosen, seen = [], set()
    for kind, limit in (("project", project_limit), ("knowledge", knowledge_limit)):
        count = 0
        for _, chunk in ordered_items:
            normalized = _norm_text(chunk.text)
            if chunk.kind != kind or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            chosen.append(chunk.as_hit())
            count += 1
            if count == limit:
                break
    return chosen


def _rank(index: retrieval.BM25Index, query: str, step: int,
          project_limit: int, knowledge_limit: int) -> List[dict]:
    """单查询排序（显式传入 query 时走这条，评估的「问题模式」也用它）。"""
    return _pick(_ordered(_score_map(index, query, step)), project_limit, knowledge_limit)


def _plan_rank(index: retrieval.BM25Index, state, step: int,
               project_limit: int, knowledge_limit: int) -> List[dict]:
    """多查询**软融合**：基础分 + 各取证面分数的加权，再按融合分排序。

    试过「每面硬取一条」，实测没有净收益（Recall@3 0.414 → 0.414，精确率还降了）：
    3 个槽位被 3 个不同的面占住，反而挤掉了基础查询本来能命中的那条。
    软融合保留了「一个块同时匹配多个面就被抬高」的好处，又不强制分槽。
    """
    base_map = _score_map(index, step_query(state, step), step)
    fused: Dict[str, Tuple[float, retrieval.IndexedChunk]] = dict(base_map)

    for facet in STEP_SUBQUERIES.get(step, ()):
        for chunk_id, (score, chunk) in _score_map(index, facet, step, limit=20).items():
            previous = fused.get(chunk_id)
            if previous is None:
                fused[chunk_id] = (FACET_WEIGHT * score, chunk)
            else:
                fused[chunk_id] = (previous[0] + FACET_WEIGHT * score, chunk)
    return _pick(_ordered(fused), project_limit, knowledge_limit)


def _legacy_rank(state, step: int, project_limit: int, knowledge_limit: int,
                 knowledge_chunks: List[dict]) -> List[dict]:
    """BM25 一个词都没命中时的回退：保持旧行为，避免退化查询直接空手而归。"""
    terms = (*STEP_TERMS.get(step, ()), state.get("indicator_type") or "")
    candidates = []
    for hit in _project_chunks(state) + list(knowledge_chunks):
        body = hit["text"].casefold()
        score = sum(term.casefold() in body for term in terms if term)
        if score:
            candidates.append((score, hit))
    candidates.sort(key=lambda item: -item[0])

    seen, selected = set(), []
    for kind, limit in (("project", project_limit), ("knowledge", knowledge_limit)):
        count = 0
        for _, hit in candidates:
            normalized = re.sub(r"\s+", "", hit["text"]).casefold()
            if hit["kind"] != kind or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            selected.append({key: hit[key] for key in ("id", "source", "locator", "kind", "text")})
            count += 1
            if count == limit:
                break
    return selected


def retrieve_step_evidence(db, state, step, include_knowledge=False, query=None,
                          project_limit: Optional[int] = None,
                          knowledge_limit: Optional[int] = None):
    """返回一组可定位的原文摘录，项目资料与通用知识分开计数。

    query 为空 → 线上路径：按「取证面」多查询选证据（每面一条），再用步骤意图补满；
    query 显式传入 → 单查询排序（评估的「问题模式」用它量上限，也方便外部按需检索）。
    """
    project_limit = PROJECT_LIMIT if project_limit is None else project_limit
    knowledge_limit = KNOWLEDGE_LIMIT if knowledge_limit is None else knowledge_limit

    project = _project_chunks(state)
    knowledge = _knowledge_chunks(db, step) if include_knowledge else []
    if not project and not knowledge:
        return []

    key = (
        tuple((hit["id"], len(hit["text"])) for hit in project),
        step if include_knowledge else None,
        tuple((hit["id"], len(hit["text"])) for hit in knowledge),
    )
    index = _cached_index(key, project + knowledge)

    explicit = (query or "").strip()
    text_query = explicit or step_query(state, step)
    if not index.known_tokens(text_query):
        return _legacy_rank(state, step, project_limit, knowledge_limit, knowledge)

    if explicit or not USE_FACET_PLAN or step not in STEP_SUBQUERIES:
        ranked = _rank(index, text_query, step, project_limit, knowledge_limit)
    else:
        ranked = _plan_rank(index, state, step, project_limit, knowledge_limit)
    if ranked:
        return ranked
    return _legacy_rank(state, step, project_limit, knowledge_limit, knowledge)


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
