"""
文档导出服务
支持PDF和Word格式导出，经典排版和自定义排版模式
"""
import os
import io
import re
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from loguru import logger

from docx import Document
from docx.shared import Pt, Inches, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


class ExportStyle:
    """排版样式配置"""

    def __init__(
        self,
        font_name: str = "微软雅黑",
        title_font_size: int = 22,
        heading1_font_size: int = 16,
        heading2_font_size: int = 14,
        body_font_size: int = 12,
        table_font_size: int = 10,
        line_spacing: float = 1.5,
        paragraph_spacing: float = 6,
        page_margin_top: float = 2.54,
        page_margin_bottom: float = 2.54,
        page_margin_left: float = 3.17,
        page_margin_right: float = 3.17,
        title_color: Tuple[int, int, int] = (0, 51, 102),
        heading_color: Tuple[int, int, int] = (0, 51, 102),
        table_header_color: Tuple[int, int, int] = (0, 51, 102),
        table_header_bg: Tuple[int, int, int] = (220, 230, 241),
    ):
        self.font_name = font_name
        self.title_font_size = title_font_size
        self.heading1_font_size = heading1_font_size
        self.heading2_font_size = heading2_font_size
        self.body_font_size = body_font_size
        self.table_font_size = table_font_size
        self.line_spacing = line_spacing
        self.paragraph_spacing = paragraph_spacing
        self.page_margin_top = page_margin_top
        self.page_margin_bottom = page_margin_bottom
        self.page_margin_left = page_margin_left
        self.page_margin_right = page_margin_right
        self.title_color = title_color
        self.heading_color = heading_color
        self.table_header_color = table_header_color
        self.table_header_bg = table_header_bg

    @classmethod
    def classic(cls) -> "ExportStyle":
        """经典排版模式"""
        return cls()

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "ExportStyle":
        """从字典创建（自定义排版）"""
        return cls(
            font_name=config.get("font_name", "微软雅黑"),
            title_font_size=config.get("title_font_size", 22),
            heading1_font_size=config.get("heading1_font_size", 16),
            heading2_font_size=config.get("heading2_font_size", 14),
            body_font_size=config.get("body_font_size", 12),
            table_font_size=config.get("table_font_size", 10),
            line_spacing=config.get("line_spacing", 1.5),
            paragraph_spacing=config.get("paragraph_spacing", 6),
            page_margin_top=config.get("page_margin_top", 2.54),
            page_margin_bottom=config.get("page_margin_bottom", 2.54),
            page_margin_left=config.get("page_margin_left", 3.17),
            page_margin_right=config.get("page_margin_right", 3.17),
        )


class ExportService:
    """文档导出服务"""

    EXPORT_DIR = "exports"

    @classmethod
    def ensure_export_dir(cls, base_dir: str = ".") -> str:
        """确保导出目录存在"""
        export_path = os.path.join(base_dir, cls.EXPORT_DIR)
        os.makedirs(export_path, exist_ok=True)
        return export_path

    @classmethod
    def export_to_word(
        cls,
        content: str,
        file_name: str,
        style: ExportStyle,
        base_dir: str = ".",
        title: Optional[str] = None,
        include_title_page: bool = True,
    ) -> str:
        """
        导出Word文档

        Args:
            content: Markdown或纯文本内容
            file_name: 输出文件名（不含扩展名）
            style: 排版样式
            base_dir: 基础目录
            title: 文档标题（可选）
            include_title_page: 是否包含封面页

        Returns:
            导出文件的完整路径
        """
        export_dir = cls.ensure_export_dir(base_dir)
        safe_name = cls._sanitize_filename(file_name)
        file_path = os.path.join(export_dir, f"{safe_name}.docx")

        doc = Document()

        # 页面设置
        cls._setup_page(doc, style)

        # 设置默认字体
        cls._setup_default_font(doc, style)

