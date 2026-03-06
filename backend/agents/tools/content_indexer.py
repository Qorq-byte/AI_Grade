"""
核心内容索引工具
Step 2 的核心功能：建立「有效项目资料」与「原文出处」的双向映射
"""
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
from dataclasses import dataclass, field
import re
import hashlib
from loguru import logger

# 条件导入 RAG embedding，避免硬依赖
try:
    from sentence_transformers import SentenceTransformer, util as st_util
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    _HAS_SENTENCE_TRANSFORMERS = False
    logger.warning("sentence-transformers 未安装，语义相似度降级为关键词匹配")


@dataclass
class ContentChunk:
    """内容块"""
    id: str
    text: str
    source_file: str
    source_location: str  # 例如 "第3页第2段" 或 "表2"
    category: str = ""   # 项目概况/资金管理/实施内容/绩效目标...
    is_effective: bool = True  # 是否为核心有效内容
    related_chunks: List[str] = field(default_factory=list)  # 关联的其他chunk ID


@dataclass
class IndexEntry:
    """索引条目"""
    core_content: str         # 核心内容摘要
    source_refs: List[str]    # 原文出处引用列表
    category: str            # 所属类别
    confidence: float        # 匹配置信度 0-1


class ContentIndexer:
    """
    核心内容索引器

    功能：
    1. 将原始文本切分为段落块
    2. 过滤无效内容（页眉页脚、空白、模板套话）
    3. 按类别对内容进行分类
    4. 建立「核心内容 ↔ 原文出处」双向索引
    5. 检测内容矛盾/不一致

    使用方式：
        indexer = ContentIndexer()
        indexer.index(raw_texts)  # raw_texts = {filename: text_content}
        result = indexer.get_index()  # Dict[str, str]
    """

    # 无效内容关键词（会被过滤或标记）
    INVALID_PATTERNS = [
        r'^此页无正文',
        r'^Page \d+',
        r'^\d+$',
        r'^[\.\-\*\_\s]+$',
        r'^第\s*\d+\s*页',
        r'^（此页无内容）',
    ]

    # 有效内容的类别关键词映射
    CATEGORY_KEYWORDS = {
        "项目概况": [
            "项目名称", "项目背景", "立项依据", "项目实施单位",
            "项目概况", "项目简介", "建设内容", "建设规模",
        ],
        "资金管理": [
            "预算", "资金", "支出", "费用", "成本", "拨款",
            "到位资金", "资金来源", "投资总额", "实际支出",
        ],
        "组织管理": [
            "组织机构", "管理流程", "制度建设", "领导小组",
            "项目管理", "实施主体", "责任单位", "招投标",
        ],
        "实施过程": [
            "实施周期", "进度", "完工", "竣工验收", "开工",
            "施工", "采购", "合同签订", "执行情况",
        ],
        "绩效目标": [
            "绩效目标", "产出指标", "效益指标", "满意度",
            "质量指标", "时效指标", "数量指标", "成本指标",
            "社会效益", "经济效益", "生态效益", "可持续影响",
        ],
    }

    # 模板套话关键词（会被降权）
    TEMPLATE_PATTERNS = [
        r'特此说明',
        r'以上情况属实',
        r'经研究决定',
        r'请领导审批',
        r'妥否.*请批示',
        r'按照.*有关规定',
    ]

    def __init__(
        self,
        use_semantic: bool = True,
        similarity_threshold: float = 0.65,
    ):
        """
        初始化索引器

        Args:
            use_semantic: 是否使用语义相似度（需要sentence-transformers）
            similarity_threshold: 语义匹配的相似度阈值
        """
        self.use_semantic = use_semantic and _HAS_SENTENCE_TRANSFORMERS
        self.similarity_threshold = similarity_threshold

        # 语料存储
        self.chunks: List[ContentChunk] = []
        self.index: Dict[str, IndexEntry] = {}  # chunk_id -> IndexEntry
        self.category_index: Dict[str, List[str]] = defaultdict(list)  # category -> chunk_ids
        self.contradictions: List[Dict[str, Any]] = []  # 内容矛盾检测结果
        self.raw_texts: Dict[str, str] = {}  # {filename: full_text}

        # 延迟加载语义模型
        self._embedding_model = None

    def _get_embedding_model(self):
        """延迟加载 embedding 模型"""
        if self._embedding_model is None and self.use_semantic:
            try:
                self._embedding_model = SentenceTransformer(
                    "paraphrase-multilingual-MiniLM-L12-v2"
                )
                logger.info("ContentIndexer: 语义模型加载成功")
            except Exception as e:
                logger.warning(f"语义模型加载失败: {e}，降级为关键词匹配")
                self.use_semantic = False
        return self._embedding_model

    def index(
        self,
        raw_texts: Dict[str, str],
        existing_categories: Optional[Dict[str, str]] = None,
    ) -> Dict[str, IndexEntry]:
        """
        对原始文本建立索引

        Args:
            raw_texts: {文件名: 完整文本内容}
            existing_categories: 已有的分类信息 {chunk_id: category}

        Returns:
            索引字典 {core_content_key: IndexEntry}
        """
        self.raw_texts = raw_texts
        self.chunks = []
        self.index = {}
        self.category_index = defaultdict(list)
        self.contradictions = []

        # 第1步：切分段落并创建chunk
        for filename, text in raw_texts.items():
            paragraphs = self._split_paragraphs(text)
            for para_idx, para in enumerate(paragraphs):
                if self._is_invalid(para):
                    continue

                chunk_id = self._generate_chunk_id(filename, para_idx, para)
                is_effective = not self._is_template_jargon(para)

                chunk = ContentChunk(
                    id=chunk_id,
                    text=para,
                    source_file=filename,
                    source_location=f"第{para_idx + 1}段",
                    category=existing_categories.get(chunk_id, "")
                    if existing_categories else "",
                    is_effective=is_effective,
                )
                self.chunks.append(chunk)

        logger.info(f"已切分 {len(self.chunks)} 个内容块 (来自 {len(raw_texts)} 个文件)")

        # 第2步：自动分类
        self._auto_categorize()

        # 第3步：建立核心内容 → 原文出处索引
        effective_chunks = [c for c in self.chunks if c.is_effective]
        for chunk in effective_chunks:
            entry = IndexEntry(
                core_content=self._summarize(chunk.text, max_len=120),
                source_refs=[f"{chunk.source_file}::{chunk.source_location}"],
                category=chunk.category or "未分类",
                confidence=self._calculate_confidence(chunk),
            )
            self.index[chunk.id] = entry
            self.category_index[chunk.category or "未分类"].append(chunk.id)

        # 第4步：语义关联（可选）
        if self.use_semantic and len(effective_chunks) >= 2:
            self._build_semantic_relations()

        # 第5步：矛盾检测
        self._detect_contradictions()

        logger.info(
            f"索引构建完成: 有效chunk={len(effective_chunks)}, "
            f"分类={len(self.category_index)}, 矛盾={len(self.contradictions)}"
        )
        return self.index

    def get_index(self) -> Dict[str, str]:
        """
        获取简化的索引映射：{核心内容: 原文出处}

        Returns:
            Dict[核心内容片段, 原文出处字符串]
        """
        result = {}
        for chunk_id, entry in self.index.items():
            source_str = "; ".join(entry.source_refs)
            key = f"[{entry.category}] {entry.core_content}"
            result[key] = source_str
        return result

    def get_index_structured(self) -> List[Dict[str, Any]]:
        """
        获取结构化的索引列表（用于Step2输出）

        Returns:
            [
                {
                    "序号": 1,
                    "核心内容摘要": "...",
                    "所属类别": "...",
                    "原文出处": "...",
                    "置信度": 0.95,
                    "备注": ""
                }
            ]
        """
        result = []
        for i, (chunk_id, entry) in enumerate(sorted(
            self.index.items(),
            key=lambda x: (x[1].category, x[0])
        ), 1):
            result.append({
                "序号": i,
                "核心内容摘要": entry.core_content,
                "所属类别": entry.category,
                "原文出处": "; ".join(entry.source_refs),
                "置信度": round(entry.confidence, 2),
                "备注": "",
            })
        return result

    def get_categorized_content(self) -> Dict[str, str]:
        """
        获取按类别整理的核心内容

        Returns:
            {类别名: 该类别所有核心内容的合并文本}
        """
        result = {}
        for category, chunk_ids in self.category_index.items():
            texts = []
            for cid in chunk_ids:
                chunk = self._find_chunk(cid)
                if chunk:
                    texts.append(chunk.text)
            result[category] = "\n\n".join(texts)
        return result

    def get_contradictions(self) -> List[Dict[str, Any]]:
        """获取检测到的内容矛盾"""
        return self.contradictions

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        搜索相关的内容块

        Args:
            query: 搜索关键词
            top_k: 返回结果数量

        Returns:
            相关结果列表
        """
        results = []

        # 关键词匹配
        query_lower = query.lower()
        for chunk in self.chunks:
            if not chunk.is_effective:
                continue
            score = self._keyword_match_score(query_lower, chunk.text.lower())
            if score > 0:
                results.append({
                    "chunk_id": chunk.id,
                    "text": chunk.text[:200],
                    "source": f"{chunk.source_file}::{chunk.source_location}",
                    "category": chunk.category,
                    "score": score,
                })

        # 按分数排序
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    # ─── 内部方法 ────────────────────────────────────

    def _split_paragraphs(self, text: str) -> List[str]:
        """将文本切分为段落"""
        if not text:
            return []
        # 按空行、换行符切分
        raw_paragraphs = re.split(r'\n\s*\n|\r\n\s*\r\n', text)
        # 过滤纯空行并清理首尾空白
        return [
            p.strip() for p in raw_paragraphs
            if p.strip() and len(p.strip()) > 5
        ]

    def _is_invalid(self, text: str) -> bool:
        """判断文本是否为无效内容"""
        for pattern in self.INVALID_PATTERNS:
            if re.search(pattern, text):
                return True
        # 过短的内容也视为无效
        if len(text.strip()) < 10:
            return True
        return False

    def _is_template_jargon(self, text: str) -> bool:
        """判断是否为模板套话"""
        for pattern in self.TEMPLATE_PATTERNS:
            if re.search(pattern, text):
                return True
        return False

    def _generate_chunk_id(self, filename: str, idx: int, text: str) -> str:
        """生成内容块的唯一ID"""
        raw = f"{filename}_{idx}_{text[:50]}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _auto_categorize(self):
        """自动为内容块分类"""
        for chunk in self.chunks:
            if chunk.category:
                continue  # 已有分类，跳过

            text = chunk.text
            scores = {}
            for category, keywords in self.CATEGORY_KEYWORDS.items():
                score = sum(1 for kw in keywords if kw in text)
                if score > 0:
                    scores[category] = score

            if scores:
                # 选择得分最高的类别
                best_category = max(scores, key=scores.get)
                chunk.category = best_category
            else:
                chunk.category = "其他"

    def _calculate_confidence(self, chunk: ContentChunk) -> float:
        """计算内容块的置信度"""
        base = 0.7
        # 长度适中加分
        text_len = len(chunk.text)
        if 50 <= text_len <= 500:
            base += 0.1
        # 有明确的分类加分
        if chunk.category and chunk.category != "其他":
            base += 0.1
        # 不是模板套话加分
        if not self._is_template_jargon(chunk.text):
            base += 0.1
        return min(base, 1.0)

    def _summarize(self, text: str, max_len: int = 120) -> str:
        """生成文本摘要（简单截取）"""
        text = text.strip()
        if len(text) <= max_len:
            return text
        # 找到合适的截断位置
        truncated = text[:max_len]
        # 尝试在句号处截断
        last_period = truncated.rfind("。")
        last_comma = truncated.rfind("，")
        cut_pos = max(last_period, last_comma, max_len - 10)
        return truncated[:cut_pos + 1] + "..." if cut_pos > 0 else truncated + "..."

    def _keyword_match_score(self, query: str, text: str) -> float:
        """计算关键词匹配得分"""
        query_terms = query.split()
        if not query_terms:
            return 0.0

        # 完全匹配
        if query in text:
            return 1.0

        # 部分匹配
        matches = sum(1 for term in query_terms if term in text)
        if matches == 0:
            return 0.0

        return matches / len(query_terms) * 0.8

    def _find_chunk(self, chunk_id: str) -> Optional[ContentChunk]:
        """根据ID查找chunk"""
        for chunk in self.chunks:
            if chunk.id == chunk_id:
                return chunk
        return None

    def _build_semantic_relations(self):
        """使用语义相似度建立chunk间的关联"""
        model = self._get_embedding_model()
        if model is None:
            return

        effective_chunks = [c for c in self.chunks if c.is_effective]
        if len(effective_chunks) < 2:
            return

        texts = [c.text for c in effective_chunks]
        try:
            embeddings = model.encode(texts, convert_to_tensor=True)
            for i in range(len(embeddings)):
                for j in range(i + 1, len(embeddings)):
                    similarity = float(st_util.cos_sim(embeddings[i], embeddings[j]))
                    if similarity >= self.similarity_threshold:
                        effective_chunks[i].related_chunks.append(effective_chunks[j].id)
                        effective_chunks[j].related_chunks.append(effective_chunks[i].id)
        except Exception as e:
            logger.warning(f"语义关联构建失败: {e}")

    def _detect_contradictions(self):
        """检测内容中的矛盾"""
        # 按类别分组检测
        for category, chunk_ids in self.category_index.items():
            if len(chunk_ids) < 2:
                continue

            chunks = [self._find_chunk(cid) for cid in chunk_ids]
            chunks = [c for c in chunks if c is not None]

            # 检测数值矛盾（资金额、日期等）
            self._detect_numeric_contradictions(chunks, category)
            # 检测事实矛盾（项目状态等）
            self._detect_factual_contradictions(chunks, category)

    def _detect_numeric_contradictions(
        self, chunks: List[ContentChunk], category: str
    ):
        """检测数值矛盾"""
        # 提取所有数值
        number_pattern = re.compile(
            r'(\d+(?:\.\d+)?)\s*(万元|元|亿|百万|千万|%)?'
        )
        num_map: Dict[str, List[Tuple[float, ContentChunk]]] = {}

        for chunk in chunks:
            matches = number_pattern.findall(chunk.text)
            for num_str, unit in matches:
                try:
                    value = float(num_str)
                    key = f"{unit}" if unit else "数值"
                    if key not in num_map:
                        num_map[key] = []
                    num_map[key].append((value, chunk))
                except ValueError:
                    continue

        # 检测同一单位下数值差异过大的情况
        for unit, values in num_map.items():
            if len(values) >= 2:
                vals = [v[0] for v in values]
                max_val = max(vals)
                min_val = min(vals)
                # 如果最大值是最小值的2倍以上，可能存在矛盾
                if min_val > 0 and max_val / min_val >= 2:
                    self.contradictions.append({
                        "type": "数值矛盾",
                        "category": category,
                        "description": f"同一类数据差异较大 ({unit})",
                        "values": [f"{v[0]}{unit}" for v in values[:3]],
                        "sources": [
                            f"{v[1].source_file}::{v[1].source_location}"
                            for v in values[:3]
                        ],
                    })

    def _detect_factual_contradictions(
        self, chunks: List[ContentChunk], category: str
    ):
        """检测事实矛盾"""
        status_keywords = {
            "已完成": True,
            "已完工": True,
            "已验收": True,
            "进行中": False,
            "在建": False,
            "尚未完成": False,
            "未完成": False,
            "已开工": True,
            "尚未开工": False,
        }

        found_statuses: Dict[str, List[ContentChunk]] = {}
        for chunk in chunks:
            for kw in status_keywords:
                if kw in chunk.text:
                    if kw not in found_statuses:
                        found_statuses[kw] = []
                    found_statuses[kw].append(chunk)

        # 检查是否存在互斥的状态描述
        contradictory_pairs = []
        for kw1 in found_statuses:
            for kw2 in found_statuses:
                if kw1 != kw2 and kw1 in status_keywords and kw2 in status_keywords:
                    if status_keywords[kw1] != status_keywords[kw2]:
                        contradictory_pairs.append((kw1, kw2))

        for kw1, kw2 in contradictory_pairs[:3]:
            self.contradictions.append({
                "type": "事实矛盾",
                "category": category,
                "description": f"项目状态描述不一致: 「{kw1}」vs「{kw2}」",
                "sources": [
                    f"{found_statuses[kw1][0].source_file}::{found_statuses[kw1][0].source_location}",
                    f"{found_statuses[kw2][0].source_file}::{found_statuses[kw2][0].source_location}",
                ],
            })


# ==================== 便捷函数 ====================

def build_content_index(
    raw_texts: Dict[str, str],
    use_semantic: bool = True,
) -> Dict[str, str]:
    """
    便捷函数：对原始文本建立内容索引

    Args:
        raw_texts: {文件名: 文本内容}
        use_semantic: 是否使用语义相似度

    Returns:
        {核心内容: 原文出处} 索引字典
    """
    indexer = ContentIndexer(use_semantic=use_semantic)
    indexer.index(raw_texts)
    return indexer.get_index()


def categorize_content(
    raw_texts: Dict[str, str],
) -> Dict[str, str]:
    """
    便捷函数：对原始文本进行分类整理

    Args:
        raw_texts: {文件名: 文本内容}

    Returns:
        {类别: 该类别下的内容合并文本}
    """
    indexer = ContentIndexer()
    indexer.index(raw_texts)
    return indexer.get_categorized_content()