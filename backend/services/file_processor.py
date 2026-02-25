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
    
