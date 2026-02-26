"""
RAG服务 - 检索增强生成
使用ChromaDB进行向量存储和检索
"""
import os
from typing import List, Dict, Optional
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    Docx2txtLoader,
    UnstructuredExcelLoader
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from loguru import logger

from backend.config import settings


class RAGService:
    """RAG服务类"""
    
    def __init__(self, collection_name: str = "performance_evaluation"):
        """
        初始化RAG服务
        
        Args:
            collection_name: ChromaDB集合名称
        """
        self.collection_name = collection_name
        self.chroma_path = settings.CHROMA_DB_PATH
        
        # 确保目录存在
        os.makedirs(self.chroma_path, exist_ok=True)
        
        # 初始化嵌入模型（使用中文支持的模型）
        self.embeddings = HuggingFaceEmbeddings(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            model_kwargs={'device': 'cpu'}
        )
        
        # 初始化向量存储
        self.vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.chroma_path
        )
        
        # 文本分割器
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]
        )
        
        logger.info(f"RAG服务初始化完成: collection={collection_name}")
    
    def load_documents(self, file_paths: List[str]) -> List[Document]:
        """
        加载文档
        
        Args:
            file_paths: 文件路径列表
            
        Returns:
            文档列表
        """
        documents = []
        
        for file_path in file_paths:
            try:
                ext = os.path.splitext(file_path)[1].lower()
                
