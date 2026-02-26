"""
文件处理服务
支持Word、Excel、PDF、图片等格式的解析和文本提取
"""
import os
from typing import List, Dict, Optional
from loguru import logger
from PIL import Image
import pytesseract


class FileProcessor:
    """文件处理器"""
    
    @staticmethod
    def process_file(file_path: str) -> Dict:
        """
        处理单个文件，提取文本内容
        
        Args:
            file_path: 文件路径
            
        Returns:
            包含文本和元数据的字典
        """
        try:
            ext = os.path.splitext(file_path)[1].lower()
            filename = os.path.basename(file_path)
            
            result = {
                "filename": filename,
                "file_path": file_path,
                "file_type": ext,
                "content": "",
                "success": False,
                "error": None
            }
            
            if ext == '.pdf':
                result["content"] = FileProcessor._extract_pdf(file_path)
            elif ext == '.docx':
                result["content"] = FileProcessor._extract_docx(file_path)
            elif ext in ['.xlsx', '.xls']:
                result["content"] = FileProcessor._extract_excel(file_path)
            elif ext in ['.jpg', '.jpeg', '.png', '.bmp']:
                result["content"] = FileProcessor._extract_image(file_path)
            elif ext == '.txt':
                result["content"] = FileProcessor._extract_txt(file_path)
            elif ext == '.csv':
                result["content"] = FileProcessor._extract_csv(file_path)
            else:
                result["error"] = f"不支持的文件格式: {ext}"
                return result
            
            result["success"] = True
            result["content_length"] = len(result["content"])
            logger.info(f"成功处理文件: {filename}")
            
            return result
            
        except Exception as e:
            logger.error(f"处理文件失败 {file_path}: {str(e)}")
            return {
                "filename": os.path.basename(file_path),
                "file_path": file_path,
                "success": False,
                "error": str(e)
            }
    
    @staticmethod
    def _extract_pdf(file_path: str) -> str:
        """提取PDF文本"""
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text
        except Exception as e:
            raise Exception(f"PDF提取失败: {str(e)}")
    
    @staticmethod
    def _extract_docx(file_path: str) -> str:
        """提取Word文档文本"""
        try:
            import docx
            doc = docx.Document(file_path)
            text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
            return text
        except Exception as e:
            raise Exception(f"Word提取失败: {str(e)}")
    
    @staticmethod
    def _extract_excel(file_path: str) -> str:
        """提取Excel文本"""
        try:
            import openpyxl
            workbook = openpyxl.load_workbook(file_path)
            text = ""
            for sheet in workbook.worksheets:
                text += f"\n=== {sheet.title} ===\n"
                for row in sheet.iter_rows(values_only=True):
                    text += "\t".join([str(cell) if cell is not None else "" for cell in row]) + "\n"
            return text
        except Exception as e:
            raise Exception(f"Excel提取失败: {str(e)}")
    
    @staticmethod
    def _extract_image(file_path: str) -> str:
        """提取图片文本（OCR）"""
        try:
            image = Image.open(file_path)
            text = pytesseract.image_to_string(image, lang='chi_sim+eng')
            return text
        except Exception as e:
            raise Exception(f"图片OCR失败: {str(e)}")
    
    @staticmethod
    def _extract_txt(file_path: str) -> str:
        """提取TXT文本"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            # 尝试其他编码
            with open(file_path, 'r', encoding='gbk') as f:
                return f.read()
    
    @staticmethod
    def _extract_csv(file_path: str) -> str:
        """提取CSV文本，保留表格格式"""
        try:
            lines = []
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    lines.append(line.strip())
            return "\n".join(lines)
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='gbk') as f:
                return "\n".join(line.strip() for line in f)
    
    @staticmethod
    def process_multiple_files(file_paths: List[str]) -> List[Dict]:
        """
        批量处理文件
        
        Args:
            file_paths: 文件路径列表
            
        Returns:
            处理结果列表
        """
        results = []
        for file_path in file_paths:
            result = FileProcessor.process_file(file_path)
            results.append(result)
        
        success_count = sum(1 for r in results if r["success"])
        logger.info(f"批量处理完成: 成功{success_count}/{len(results)}")
        
        return results
    
    @staticmethod
    def extract_key_info(text: str, info_type: str) -> str:
        """
        从文本中提取关键信息（简单版本，实际应使用LLM）
        
        Args:
            text: 原始文本
            info_type: 信息类型（如'cost', 'performance', 'organization'等）
            
        Returns:
            提取的关键信息
        """
        # 这里只是一个占位符，实际应该调用LLM进行智能提取
        # 返回前500个字符作为示例
        return text[:500] if text else ""
