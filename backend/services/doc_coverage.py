"""
长文档覆盖度：分块、预算截取、覆盖度声明、未使用部分的要点压缩

问题背景：
  此前每一步的提示词都只放 `raw_text[:8000]` / `[:12000]`。一份 6 万字的项目资料，
  后 4.8 万字**从来没有进过任何一次模型调用**，而提示词里没有任何一句话说明这件事。
  模型于是对没看到的内容也会「合理推测」，报告看起来完整，实际上一半材料没参与。

本模块做四件事，且都不依赖模型：
  1. 把多份原始文本切成带**定位符**的块（PDF 用页码，Word 用标题，纯文本用段序）；
  2. 按预算挑选块——**头部 + 尾部**都取（结论/验收信息常在文末），不是一味砍尾巴；
  3. 生成**覆盖度声明**（总字数 / 使用字数 / 未使用块的来源与定位），供提示词与界面展示；
  4. 把未使用块分批交给模型压成要点清单（map-reduce），在**不增加原文预算**的前提下
     让模型至少知道「没看到的那部分讲了什么」。

边界：
  - 压缩摘要只覆盖到一定字数（见 MAX_DIGEST_BATCHES），超出部分会在声明里明确写出「未压缩」；
  - 原文永远完整保存在 `state["raw_text"]` / `all_raw_texts`，本模块只做「本次提示词用了哪些」的记录。
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 每块的目标字数（块越小定位越细，但块头越多）
CHUNK_TARGET_CHARS = 2000
# 留多少预算给文末（结论、验收、意见常在此）
TAIL_RESERVE_RATIO = 0.2
# 未使用部分少于该字数就不值得额外调用模型压缩
DIGEST_MIN_CHARS = 500
# 摘要压缩的单批上限与最大批次（防止一次上传把 token 打爆）
DIGEST_BATCH_CHARS = 6000
MAX_DIGEST_BATCHES = 3
DIGEST_MAX_CHARS = 800

_PAGE_MARKER = re.compile(r"【(?:PDF\s*)?第\s*(\d+)\s*页】")
_HEADING = re.compile(r"^(#{1,4})\s*(\S.{0,40})$", re.MULTILINE)
_SENTENCE_END = "。！？!?；;\n"


@dataclass
class DocChunk:
    index: int
    source: str
    locator: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)

    def ref(self) -> str:
        return f"{self.source} {self.locator}".strip()

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "source": self.source, "locator": self.locator, "chars": self.chars}


@dataclass
class CoveragePlan:
    """一次「提示词用了哪些资料」的完整记录"""

    total_chars: int = 0
    used_chunks: List[DocChunk] = field(default_factory=list)
    omitted_chunks: List[DocChunk] = field(default_factory=list)
    digest: str = ""
    digest_chars_covered: int = 0

    @property
    def used_chars(self) -> int:
        return sum(chunk.chars for chunk in self.used_chunks)

    @property
    def omitted_chars(self) -> int:
        return sum(chunk.chars for chunk in self.omitted_chunks)

    @property
    def mode(self) -> str:
        return "full" if not self.omitted_chunks else "head_tail"

    @property
    def used_ratio(self) -> float:
        return round(self.used_chars / self.total_chars, 4) if self.total_chars else 1.0

    def text(self, limit: Optional[int] = None) -> str:
        """本次提示词可用的原文；limit 给出时按「节选」截到前 limit 字（不超过）。"""
        if limit is None or limit >= self.used_chars:
            return "\n\n".join(chunk.text for chunk in self.used_chunks)
        parts: List[str] = []
        used = 0
        for chunk in self.used_chunks:
            separator = 2 if parts else 0
            room = limit - used - separator
            if room <= 0:
                break
            piece = chunk.text[:room]
            if not piece:
                break
            parts.append(piece)
            used += separator + len(piece)
        return "\n\n".join(parts)

    def omitted_summary(self, limit: int = 6) -> str:
        items = [f"{chunk.ref()}（{chunk.chars} 字）" for chunk in self.omitted_chunks[:limit]]
        if len(self.omitted_chunks) > limit:
            items.append(f"等 {len(self.omitted_chunks)} 段")
        return "、".join(items)

    def statement(self, limit: int = 6, delivered_chars: Optional[int] = None) -> str:
        """写进提示词的覆盖度声明。

        delivered_chars 用于「本步骤只放入节选」的场景（如步骤 1 用前 8000 字）：
        声明里的数字必须和该步骤真正收到的字数一致，否则等于新的静默截断。
        """
        if not self.total_chars:
            return "【资料覆盖度】本次未提供可解析的原始资料。"
        delivered = self.used_chars if delivered_chars is None else max(
            0, min(int(delivered_chars), self.used_chars)
        )
        clipped = delivered < self.used_chars
        if not self.omitted_chunks and not clipped:
            return f"【资料覆盖度】资料共 {self.total_chars} 字，已全部纳入本次提示词。"

        head = (
            f"【资料覆盖度】资料共 {self.total_chars} 字，本步骤提示词直接引用 {delivered} 字"
            f"（{delivered / self.total_chars * 100:.1f}%）"
        )
        if clipped:
            head += f"；本次预算可选 {self.used_chars} 字，本步骤只放入前 {delivered} 字"
        head += f"，其余 {self.total_chars - delivered} 字未进入本步骤提示词。"
        body = ""
        if self.omitted_chunks:
            body = f"未直接引用的部分：{self.omitted_summary(limit)}。"
        if clipped and self.used_chars > delivered:
            body += f"\n预算内还有 {self.used_chars - delivered} 字未放入本步骤，也未做摘要。"
        if self.digest:
            covered = f"（已压缩 {self.digest_chars_covered} 字为下方要点，其余未压缩）" \
                if self.digest_chars_covered < self.omitted_chars else ""
            body += f"\n未直接引用部分的要点摘要{covered}见「未直接引用部分的要点」。"
        else:
            body += "\n这些内容未被模型看到：不要猜测其中细节，需要时标注「资料未纳入本次分析」。"
        body = body.strip("\n")
        return f"{head}\n{body}" if body.strip() else head

    def to_dict(self) -> Dict[str, Any]:
        """给状态接口 / 前端徽标用的轻量结构（不含原文）。"""
        return {
            "total_chars": self.total_chars,
            "used_chars": self.used_chars,
            "omitted_chars": self.omitted_chars,
            "used_ratio": self.used_ratio,
            "mode": self.mode,
            "chunks": len(self.used_chunks) + len(self.omitted_chunks),
            "omitted": [chunk.to_dict() for chunk in self.omitted_chunks],
            "has_digest": bool(self.digest),
            "statement": self.statement(),
        }


# ═══════════════════════════════════════════════════════
#  切块
# ═══════════════════════════════════════════════════════

def _split_long(text: str, limit: int) -> List[str]:
    """把超长段落按句子边界切成 ≤ limit 的块。"""
    if len(text) <= limit:
        return [text]
    pieces, buffer = [], ""
    for char in text:
        buffer += char
        if len(buffer) >= limit and char in _SENTENCE_END:
            pieces.append(buffer)
            buffer = ""
    if buffer.strip():
        pieces.append(buffer)
    # 极端情况：整段没有句末符号，只能硬切
    if any(len(piece) > limit * 2 for piece in pieces):
        pieces = [piece[i:i + limit] for piece in pieces for i in range(0, len(piece), limit)]
    return pieces


def _segments(text: str) -> List[Tuple[str, str]]:
    """按页码 / 标题把一份文本切成带定位符的段。"""
    pages = list(_PAGE_MARKER.finditer(text))
    if pages:
        segments = []
        for order, match in enumerate(pages):
            start = match.end()
            end = pages[order + 1].start() if order + 1 < len(pages) else len(text)
            body = text[start:end].strip()
            if body:
                segments.append((f"第{match.group(1)}页", body))
        if segments:
            return segments

    headings = list(_HEADING.finditer(text))
    if headings:
        segments = []
        for order, match in enumerate(headings):
            start = match.end()
            end = headings[order + 1].start() if order + 1 < len(headings) else len(text)
            body = text[start:end].strip()
            title = match.group(2).strip()
            if body:
                segments.append((title, body))
        if segments:
            return segments

    return [("全文", text.strip())]


def _paragraphs(text: str) -> Iterable[str]:
    for block in re.split(r"\n\s*\n", text):
        cleaned = block.strip()
        if cleaned:
            yield cleaned


def split_documents(all_raw_texts: Dict[str, str], target_chars: int = CHUNK_TARGET_CHARS) -> List[DocChunk]:
    """把 {文件名: 文本} 切成带定位符的块序列（顺序即原文顺序）。"""
    chunks: List[DocChunk] = []
    for source, text in (all_raw_texts or {}).items():
        if not text or not text.strip():
            continue
        for locator, body in _segments(text):
            pieces: List[str] = []
            buffer = ""
            for paragraph in _paragraphs(body):
                for piece in _split_long(paragraph, target_chars):
                    if buffer and len(buffer) + len(piece) + 2 > target_chars:
                        pieces.append(buffer)
                        buffer = piece
                    else:
                        buffer = f"{buffer}\n\n{piece}" if buffer else piece
            if buffer.strip():
                pieces.append(buffer)

            for order, piece in enumerate(pieces, 1):
                suffix = f"（{order}/{len(pieces)}）" if len(pieces) > 1 else ""
                chunks.append(DocChunk(
                    index=len(chunks), source=source, locator=locator + suffix, text=piece,
                ))
    return chunks


# ═══════════════════════════════════════════════════════
#  预算选择 + 覆盖度
# ═══════════════════════════════════════════════════════

def build_plan(all_raw_texts: Optional[Dict[str, str]] = None,
               raw_text: str = "",
               budget_chars: int = 12000) -> CoveragePlan:
    """按预算挑选块：先从头取，再为文末预留一段预算。"""
    texts = {name: text for name, text in (all_raw_texts or {}).items() if text and text.strip()}
    if not texts and raw_text and raw_text.strip():
        texts = {"原始资料": raw_text}

    chunks = split_documents(texts)
    plan = CoveragePlan(total_chars=sum(chunk.chars for chunk in chunks))
    if not chunks:
        return plan

    if plan.total_chars <= budget_chars:
        plan.used_chunks = list(chunks)
        return plan

    tail_budget = max(int(budget_chars * TAIL_RESERVE_RATIO), 0)
    head_budget = budget_chars - tail_budget

    selected: List[DocChunk] = []
    used = 0
    head_end = 0
    for order, chunk in enumerate(chunks):
        if used + chunk.chars > head_budget:
            break
        selected.append(chunk)
        used += chunk.chars
        head_end = order + 1

    tail: List[DocChunk] = []
    remaining = budget_chars - used
    for chunk in reversed(chunks[head_end:]):
        if chunk.chars > remaining:
            break
        tail.insert(0, chunk)
        remaining -= chunk.chars

    chosen = {chunk.index for chunk in selected} | {chunk.index for chunk in tail}
    plan.used_chunks = [chunk for chunk in chunks if chunk.index in chosen]
    plan.omitted_chunks = [chunk for chunk in chunks if chunk.index not in chosen]
    return plan


def digest_batches(plan: CoveragePlan, batch_chars: int = DIGEST_BATCH_CHARS,
                   max_batches: int = MAX_DIGEST_BATCHES) -> List[str]:
    """把未使用块打包成若干批提示词片段（顺序、可定位）。"""
    batches: List[str] = []
    buffer, size = [], 0
    for chunk in plan.omitted_chunks:
        entry = f"[{chunk.ref()}] {chunk.text}"
        if buffer and size + len(entry) > batch_chars:
            batches.append("\n\n".join(buffer))
            buffer, size = [], 0
            if len(batches) >= max_batches:
                return batches
        buffer.append(entry)
        size += len(entry)
    if buffer and len(batches) < max_batches:
        batches.append("\n\n".join(buffer))
    return batches


def digest_messages(batch: str, max_chars: int = DIGEST_MAX_CHARS) -> List[Dict[str, str]]:
    """map-reduce 里 map 阶段的提示词（纯文本消息，便于离线测试）。"""
    system = (
        "你是资料压缩器。只做要点摘录：不评价、不推断、不补全、不编造。"
        "金额、时间、主体、数量、结论必须与原文字面一致。"
    )
    human = (
        f"下面是本次未被直接引用的一段项目资料（带定位符）。请压缩为不超过 {max_chars} 字的中文要点清单：\n"
        "1. 逐条以「- 」开头，每条不超过 40 字；\n"
        "2. 优先保留：金额、时间、责任主体、产出数量、验收结论、异常与风险；\n"
        "3. 每条末尾用方括号标注来源定位符，例如 [预算说明.pdf 第3页]；\n"
        "4. 只输出清单，不要开场白、不要总结段落。\n\n"
        f"资料：\n{batch}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": human}]


def reduce_digest(parts: List[str], max_chars: int = DIGEST_MAX_CHARS) -> str:
    """把多批摘要合并成一段（去重、截断到上限）。"""
    seen, lines = set(), []
    for part in parts:
        for line in (part or "").splitlines():
            cleaned = line.strip()
            if not cleaned or not cleaned.startswith("- "):
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            lines.append(cleaned)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text
