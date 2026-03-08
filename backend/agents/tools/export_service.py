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

        # 封面页
        if include_title_page:
            cls._add_title_page(doc, title or file_name, style)
            doc.add_page_break()

        # 解析Markdown内容并添加
        cls._render_content(doc, content, style)

        doc.save(file_path)
        logger.info(f"Word文档已导出: {file_path}")
        return file_path

    @classmethod
    def export_indicator_table_to_word(
        cls,
        indicator_system: Dict[str, Any],
        file_name: str,
        style: ExportStyle,
        base_dir: str = ".",
        title: Optional[str] = None,
    ) -> str:
        """
        导出指标体系表格到Word

        表格列：一级指标、二级指标、三级指标、分值、指标解释、评分标准
        """
        export_dir = cls.ensure_export_dir(base_dir)
        safe_name = cls._sanitize_filename(file_name)
        file_path = os.path.join(export_dir, f"{safe_name}.docx")

        doc = Document()
        cls._setup_page(doc, style)
        cls._setup_default_font(doc, style)

        # 标题
        if title:
            cls._add_heading(doc, title, style, level=0)

        # 构建指标体系表格
        level1_indicators = indicator_system.get("level1_indicators", [])
        rows_data = []
        for l1 in level1_indicators:
            l1_name = f"{l1.get('name', '')}（{l1.get('score', '')}分）"
            for l2 in (l1.get("children", []) or []):
                l2_name = f"{l2.get('name', '')}（{l2.get('score', '')}分）"
                l3_list = l2.get("children", []) or []
                if l3_list:
                    for l3 in l3_list:
                        rows_data.append([
                            l1_name,
                            l2_name,
                            l3.get("name", ""),
                            str(l3.get("score", "")),
                            l3.get("explanation", "") or "",
                            l3.get("scoring_standard", "") or "",
                        ])
                        l1_name = ""  # 合并单元格用（简化处理）
                        l2_name = ""
                else:
                    rows_data.append([
                        l1_name, l2_name, "", "", "", ""
                    ])
                    l1_name = ""

        # 创建表格
        headers = ["一级指标", "二级指标", "三级指标", "分值", "指标解释", "评分标准"]
        cls._create_formatted_table(doc, headers, rows_data, style)

        doc.save(file_path)
        logger.info(f"指标体系表格已导出: {file_path}")
        return file_path

    @classmethod
    def export_score_table_to_word(
        cls,
        score_data: List[Dict[str, Any]],
        file_name: str,
        style: ExportStyle,
        base_dir: str = ".",
        title: Optional[str] = None,
    ) -> str:
        """
        导出评分表到Word

        表格列：一级指标、二级指标、三级指标、分值、得分、扣分原因
        """
        export_dir = cls.ensure_export_dir(base_dir)
        safe_name = cls._sanitize_filename(file_name)
        file_path = os.path.join(export_dir, f"{safe_name}.docx")

        doc = Document()
        cls._setup_page(doc, style)
        cls._setup_default_font(doc, style)

        if title:
            cls._add_heading(doc, title, style, level=0)

        headers = ["一级指标", "二级指标", "三级指标", "分值", "得分", "扣分原因"]
        rows_data = []
        for item in score_data:
            rows_data.append([
                item.get("level1_name", ""),
                item.get("level2_name", ""),
                item.get("level3_name", ""),
                str(item.get("score", "")),
                str(item.get("score_obtained", "")),
                item.get("score_reason", "") or "",
            ])

        cls._create_formatted_table(doc, headers, rows_data, style)

        doc.save(file_path)
        logger.info(f"评分表已导出: {file_path}")
        return file_path

    @classmethod
    def export_to_pdf(
        cls,
        content: str,
        file_name: str,
        style: ExportStyle,
        base_dir: str = ".",
        title: Optional[str] = None,
    ) -> str:
        """
        导出PDF文档（reportlab 实现，中文使用内置 STSong-Light 字体）
        """
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
        )

        cls._register_pdf_font()

        export_dir = cls.ensure_export_dir(base_dir)
        safe_name = cls._sanitize_filename(file_name)
        file_path = os.path.join(export_dir, f"{safe_name}.pdf")

        doc = SimpleDocTemplate(
            file_path,
            pagesize=A4,
            topMargin=style.page_margin_top * cm,
            bottomMargin=style.page_margin_bottom * cm,
            leftMargin=style.page_margin_left * cm,
            rightMargin=style.page_margin_right * cm,
            title=title or file_name,
        )

        body = ParagraphStyle(
            "Body", fontName=cls.PDF_FONT, fontSize=style.body_font_size,
            leading=style.body_font_size * style.line_spacing,
            spaceAfter=style.paragraph_spacing,
        )
        h1 = ParagraphStyle("H1", parent=body, fontSize=style.heading1_font_size,
                            leading=style.heading1_font_size * 1.3, spaceBefore=14, spaceAfter=8)
        h2 = ParagraphStyle("H2", parent=body, fontSize=style.heading2_font_size,
                            leading=style.heading2_font_size * 1.3, spaceBefore=10, spaceAfter=6)
        h3 = ParagraphStyle("H3", parent=body, fontSize=style.body_font_size + 1,
                            leading=(style.body_font_size + 1) * 1.3, spaceBefore=8, spaceAfter=4)
        title_style = ParagraphStyle(
            "DocTitle", fontName=cls.PDF_FONT, fontSize=style.title_font_size,
            leading=style.title_font_size * 1.4, alignment=1, spaceAfter=24,
        )
        center = ParagraphStyle("Center", parent=body, alignment=1)

        elements = []

        # 封面页
        if title:
            elements.append(Spacer(1, 6 * cm))
            elements.append(Paragraph(cls._md_escape(title), title_style))
            elements.append(Spacer(1, 1 * cm))
            elements.append(Paragraph(datetime.now().strftime("%Y年%m月%d日"), center))
            elements.append(PageBreak())

        # 渲染 Markdown 内容
        lines = content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            stripped = line.strip()
            if not stripped:
                i += 1
                continue

            if stripped.startswith("# "):
                elements.append(Paragraph(cls._md_escape(stripped[2:]), h1))
            elif stripped.startswith("## "):
                elements.append(Paragraph(cls._md_escape(stripped[3:]), h2))
            elif stripped.startswith("### "):
                elements.append(Paragraph(cls._md_escape(stripped[4:]), h3))
            elif stripped.startswith("|") and i + 1 < len(lines) and "---" in lines[i + 1]:
                # Markdown 表格
                table_lines = [stripped]
                i += 2  # 跳过表头分隔行
                while i < len(lines) and lines[i].strip().startswith("|"):
                    table_lines.append(lines[i].strip())
                    i += 1
                elements.extend(cls._pdf_table(table_lines, style))
                continue
            elif stripped.startswith(("- ", "* ")) or re.match(r"^\d+\.\s", stripped):
                text = re.sub(r"^(\d+\.\s)", "• ", stripped)
                text = re.sub(r"^(-\s|\*\s)", "• ", text)
                elements.append(Paragraph(
                    "&nbsp;&nbsp;" + cls._md_escape(text), body))
            else:
                elements.append(Paragraph(cls._md_escape(cls._md_plain(stripped)), body))
            i += 1

        doc.build(elements)
        logger.info(f"PDF文档已导出: {file_path}")
        return file_path

    PDF_FONT = "STSong-Light"
    _pdf_font_registered = False

    @classmethod
    def _register_pdf_font(cls):
        """注册中文字体（幂等）"""
        if cls._pdf_font_registered:
            return
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont(cls.PDF_FONT))
        cls._pdf_font_registered = True

    @staticmethod
    def _md_escape(text: str) -> str:
        """转义XML特殊字符，避免Paragraph解析错误"""
        return (
            text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
        )

    @staticmethod
    def _md_plain(text: str) -> str:
        """去除Markdown强调标记（CID字体不支持加粗，避免出现星号）"""
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        return text

    @classmethod
    def _pdf_table(cls, lines: List[str], style: ExportStyle) -> List[Any]:
        """将Markdown表格行转换为 reportlab Table flowable"""
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Table, TableStyle, Paragraph, Spacer

        rows = []
        for line in lines:
            if re.match(r"^\|?[\s:|-]+\|?$", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
        if not rows:
            return []

        cell_style = ParagraphStyle(
            "Cell", fontName=cls.PDF_FONT, fontSize=style.table_font_size,
            leading=style.table_font_size * 1.3,
        )
        header_style = ParagraphStyle(
            "CellHeader", parent=cell_style, textColor=colors.HexColor("#003366"),
        )
        data = []
        for r_i, row in enumerate(rows):
            data.append([
                Paragraph(cls._md_escape(cls._md_plain(c)), header_style if r_i == 0 else cell_style)
                for c in row
            ])

        # 固定列宽：避免表格总宽超过可用宽度导致 negative availWidth 布局错误
        from reportlab.lib.units import cm
        usable_width = 21.0 * cm - (style.page_margin_left + style.page_margin_right) * cm
        col_count = max(len(r) for r in rows) or 1
        col_widths = [usable_width / col_count] * col_count

        table = Table(data, colWidths=col_widths, hAlign="CENTER")
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(
                "#%02X%02X%02X" % style.table_header_bg)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return [table, Spacer(1, 8)]

    # ─── 内部方法 ────────────────────────────────────

    @classmethod
    def _setup_page(cls, doc: Document, style: ExportStyle):
        """设置页面边距"""
        section = doc.sections[0]
        section.top_margin = Cm(style.page_margin_top)
        section.bottom_margin = Cm(style.page_margin_bottom)
        section.left_margin = Cm(style.page_margin_left)
        section.right_margin = Cm(style.page_margin_right)

    @classmethod
    def _setup_default_font(cls, doc: Document, style: ExportStyle):
        """设置文档默认字体"""
        style_obj = doc.styles['Normal']
        font = style_obj.font
        font.name = style.font_name
        font.size = Pt(style.body_font_size)
        # 设置中文字体
        rPr = style_obj.element.get_or_add_rPr()
        rFonts = OxmlElement('w:rFonts')
        rFonts.set(qn('w:eastAsia'), style.font_name)
        rPr.insert(0, rFonts)

    @classmethod
    def _add_title_page(cls, doc: Document, title: str, style: ExportStyle):
        """添加封面页"""
        # 空行占位
        for _ in range(6):
            doc.add_paragraph()

        # 主标题
        title_para = doc.add_paragraph()
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_run = title_para.add_run(title)
        title_run.font.size = Pt(style.title_font_size)
        title_run.font.name = style.font_name
        title_run.font.color.rgb = RGBColor(*style.title_color)
        title_run.bold = True
        rPr = title_run._element.get_or_add_rPr()
        rFonts = OxmlElement('w:rFonts')
        rFonts.set(qn('w:eastAsia'), style.font_name)
        rPr.insert(0, rFonts)

        doc.add_paragraph()

        # 日期
        date_para = doc.add_paragraph()
        date_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        date_run = date_para.add_run(datetime.now().strftime("%Y年%m月%d日"))
        date_run.font.size = Pt(14)
        date_run.font.name = style.font_name

    @classmethod
    def _add_heading(
        cls, doc: Document, text: str, style: ExportStyle, level: int = 1
    ):
        """添加标题"""
        heading = doc.add_heading(text, level=level)
        for run in heading.runs:
            run.font.name = style.font_name
            run.font.color.rgb = RGBColor(*style.heading_color)
            rPr = run._element.get_or_add_rPr()
            rFonts = OxmlElement('w:rFonts')
            rFonts.set(qn('w:eastAsia'), style.font_name)
            rPr.insert(0, rFonts)

    @classmethod
    def _render_content(cls, doc: Document, content: str, style: ExportStyle):
        """渲染Markdown内容到Word文档"""
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            # 标题
            if line.startswith('# '):
                cls._add_heading(doc, line[2:], style, level=1)
            elif line.startswith('## '):
                cls._add_heading(doc, line[3:], style, level=2)
            elif line.startswith('### '):
                cls._add_heading(doc, line[4:], style, level=3)
            # 表格
            elif line.startswith('|') and '---' in lines[i + 1] if i + 1 < len(lines) else False:
                table_lines = [line]
                i += 1
                # 分隔行
                if i < len(lines):
                    table_lines.append(lines[i])
                    i += 1
                # 数据行
                while i < len(lines) and lines[i].strip().startswith('|'):
                    table_lines.append(lines[i])
                    i += 1
                cls._render_table(doc, table_lines, style)
                continue
            else:
                para = doc.add_paragraph(line)
                para_format = para.paragraph_format
                para_format.line_spacing = style.line_spacing
                para_format.space_after = Pt(style.paragraph_spacing)

            i += 1

    @classmethod
    def _render_table(cls, doc: Document, lines: List[str], style: ExportStyle):
        """渲染Markdown表格"""
        if len(lines) < 2:
            return

        rows = []
        for line in lines:
            if line.strip().startswith('|---'):
                continue
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            rows.append(cells)

        if not rows:
            return

        headers = rows[0] if rows else []
        data = rows[1:] if len(rows) > 1 else []

        cls._create_formatted_table(doc, headers, data, style)

    @classmethod
    def _create_formatted_table(
        cls,
        doc: Document,
        headers: List[str],
        data: List[List[str]],
        style: ExportStyle,
    ):
        """创建格式化表格"""
        if not headers:
            return

        cols = len(headers)
        table = doc.add_table(rows=1 + len(data), cols=cols)
        table.style = 'Table Grid'
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        # 表头
        header_cells = table.rows[0].cells
        for j, header in enumerate(headers):
            cell = header_cells[j]
            cell.text = header
            cls._format_cell(cell, style, is_header=True)

        # 数据行
        for i, row_data in enumerate(data):
            row_cells = table.rows[i + 1].cells
            for j, value in enumerate(row_data):
                if j >= cols:
                    break
                cell = row_cells[j]
                cell.text = value if value else ""
                cls._format_cell(cell, style, is_header=False)

    @classmethod
    def _format_cell(
        cls, cell, style: ExportStyle, is_header: bool = False
    ):
        """格式化单元格"""
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = Pt(2)
            paragraph.paragraph_format.space_after = Pt(2)
            for run in paragraph.runs:
                run.font.size = Pt(style.table_font_size)
                run.font.name = style.font_name
                if is_header:
                    run.font.color.rgb = RGBColor(*style.table_header_color)
                    run.bold = True

        # 设置表头背景色
        if is_header:
            tc = cell._tc
            tcPr = tc.get_or_add_tcPr()
            shading = OxmlElement('w:shd')
            shading.set(qn('w:fill'), cls._rgb_to_hex(style.table_header_bg))
            shading.set(qn('w:val'), 'clear')
            tcPr.append(shading)

    @classmethod
    def _sanitize_filename(cls, name: str) -> str:
        """清理文件名中的非法字符"""
        illegal_chars = r'[\\/*?:"<>|]'
        safe = re.sub(illegal_chars, '_', name)
        return safe.strip()[:200]

    @classmethod
    def _rgb_to_hex(cls, rgb: Tuple[int, int, int]) -> str:
        """RGB转十六进制"""
        return f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


# 便捷函数
def export_report(
    content: str,
    project_name: str,
    report_type: str = "绩效评价报告",
    style_mode: str = "classic",
    custom_style: Optional[Dict[str, Any]] = None,
    format: str = "word",
    base_dir: str = ".",
) -> str:
    """
    便捷导出函数

    Args:
        content: 报告内容
        project_name: 项目名称
        report_type: 报告类型（用于文件命名）
        style_mode: 排版模式 "classic" 或 "custom"
        custom_style: 自定义样式配置
        format: 导出格式 "word" 或 "pdf"
        base_dir: 基础目录

    Returns:
        导出文件路径
    """
    if style_mode == "classic" or not custom_style:
        style = ExportStyle.classic()
    else:
        style = ExportStyle.from_dict(custom_style)

    file_name = f"{project_name}{report_type}"

    if format == "pdf":
        return ExportService.export_to_pdf(content, file_name, style, base_dir, title=report_type)
    else:
        return ExportService.export_to_word(content, file_name, style, base_dir, title=report_type)