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

