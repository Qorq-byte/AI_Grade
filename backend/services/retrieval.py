"""检索排序：BM25 + 中文二元组分词。

替代原来 `workflow_evidence.retrieve_step_evidence` 里的排序方式：
    score = sum(term.casefold() in text.casefold() for term in STEP_TERMS[step])
那条式子有两个硬伤——(1) 只看「词表命中几个」，长句套话天然占优；
(2) 同分时保持原文顺序，等于把「相关性」交给了文件排列顺序。

这里改成标准 BM25：
  - 中文按**二元组**切（不引入 jieba 之类的新依赖）："总投资" → "总投"/"投资"；
  - 英文与数字按连续字符切，金额里的千分位与空格先归一化；
  - idf 由本批语料动态算出，长文档里高频套话（"项目""资金"）自然降权；
  - 文档长度归一化（b=0.75），长段落不再因为「字多所以命中多」而占便宜。

纯函数 + 纯 Python，不依赖网络、向量库或模型，可以在测试里逐条断言排序。
"""
import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

K1 = 1.2
B = 0.75

_CJK = r"\u4e00-\u9fff"
_TOKEN_STRIP = re.compile(r"[\s\u3000]+")
# 金额里的千分位属于书写习惯，不是语义：「4,860」与「4860」必须是同一个词
_THOUSANDS = re.compile(r"(?<=\d),(?=\d)")
_ASCII_RUN = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> str:
    return _TOKEN_STRIP.sub("", _THOUSANDS.sub("", (text or "").casefold()))


def tokenize(text: str) -> List[str]:
    """中文二元组 + 英文/数字连续串。

    单字中文不入索引：中文单字几乎无区分度（"的""项""目"），会把 BM25 的 idf 拉平。
    查询侧同理，所以「一句话只有一个字」这种退化查询只能靠回退路径处理。
    """
    normalized = _normalize(text)
    if not normalized:
        return []
    tokens: List[str] = []
    run = ""
    for index, char in enumerate(normalized):
        if re.match(f"[{_CJK}]", char):
            if run:
                tokens.extend(_ASCII_RUN.findall(run))
                run = ""
            if index + 1 < len(normalized) and re.match(f"[{_CJK}]", normalized[index + 1]):
                tokens.append(normalized[index:index + 2])
        else:
            run += char
    if run:
        tokens.extend(_ASCII_RUN.findall(run))
    return tokens


@dataclass
class IndexedChunk:
    id: str
    source: str
    locator: str
    kind: str
    text: str
    step_number: Optional[int] = None
    tokens: List[str] = field(default_factory=list, repr=False)

    def as_hit(self) -> dict:
        return {"id": self.id, "source": self.source, "locator": self.locator,
                "kind": self.kind, "text": self.text}


class BM25Index:
    """一份语料的 BM25 索引；构造一次，多次查询。"""

    def __init__(self, chunks: Sequence[IndexedChunk]):
        self.chunks = list(chunks)
        self.doc_tokens: List[Dict[str, int]] = []
        self.doc_len: List[int] = []
        df: Dict[str, int] = {}
        for chunk in self.chunks:
            tokens = tokenize(chunk.text)
            chunk.tokens = tokens
            counts: Dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.doc_tokens.append(counts)
            self.doc_len.append(len(tokens))
            for token in counts:
                df[token] = df.get(token, 0) + 1
        total = len(self.chunks)
        self.avg_len = (sum(self.doc_len) / total) if total else 0.0
        self.idf = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in df.items()
        }

    def score(self, query: str, index: int) -> float:
        counts = self.doc_tokens[index]
        if not counts:
            return 0.0
        length = self.doc_len[index] or 1
        norm = K1 * (1 - B + B * length / (self.avg_len or 1))
        total = 0.0
        for token in set(tokenize(query)):
            tf = counts.get(token)
            if not tf:
                continue
            total += self.idf.get(token, 0.0) * tf * (K1 + 1) / (tf + norm)
        return total

    def search(self, query: str, limit: int = 30) -> List[Tuple[float, IndexedChunk]]:
        scored = [(self.score(query, index), chunk) for index, chunk in enumerate(self.chunks)]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return scored[:limit]

    def known_tokens(self, query: str) -> int:
        return sum(1 for token in set(tokenize(query)) if token in self.idf)


def build_index(chunks: Iterable[dict]) -> BM25Index:
    """从 `_chunks()` 产出的 dict 列表建索引（保留 kind / step_number 元信息）。"""
    return BM25Index([
        IndexedChunk(
            id=chunk["id"], source=chunk["source"], locator=chunk["locator"],
            kind=chunk["kind"], text=chunk["text"], step_number=chunk.get("step_number"),
        )
        for chunk in chunks
    ])
