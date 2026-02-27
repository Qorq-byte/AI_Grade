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
                
                if ext == '.pdf':
                    loader = PyPDFLoader(file_path)
                elif ext == '.docx':
                    loader = Docx2txtLoader(file_path)
                elif ext == '.xlsx' or ext == '.xls':
                    loader = UnstructuredExcelLoader(file_path, mode="elements")
                elif ext == '.txt':
                    loader = TextLoader(file_path, encoding='utf-8')
                else:
                    logger.warning(f"不支持的文件格式: {ext}")
                    continue
                
                docs = loader.load()
                documents.extend(docs)
                logger.info(f"成功加载文档: {file_path}")
                
            except Exception as e:
                logger.error(f"加载文档失败 {file_path}: {str(e)}")
        
        return documents
    
    def add_documents(self, documents: List[Document], metadata: Optional[Dict] = None):
        """
        添加文档到向量数据库
        
        Args:
            documents: 文档列表
            metadata: 额外元数据
        """
        # 分割文档
        split_docs = self.text_splitter.split_documents(documents)
        
        # 添加元数据
        if metadata:
            for doc in split_docs:
                doc.metadata.update(metadata)
        
        # 添加到向量存储
        self.vectorstore.add_documents(split_docs)
        logger.info(f"添加了 {len(split_docs)} 个文档块")
    
    def add_texts(self, texts: List[str], metadatas: Optional[List[Dict]] = None):
        """
        添加文本到向量数据库
        
        Args:
            texts: 文本列表
            metadatas: 元数据列表
        """
        self.vectorstore.add_texts(texts, metadatas)
        logger.info(f"添加了 {len(texts)} 个文本")
    
    def search(
        self,
        query: str,
        k: int = 5,
        filter_dict: Optional[Dict] = None
    ) -> List[Dict]:
        """
        检索相关文档
        
        Args:
            query: 查询文本
            k: 返回结果数量
            filter_dict: 过滤条件
            
        Returns:
            相关文档列表
        """
        try:
            results = self.vectorstore.similarity_search(
                query=query,
                k=k,
                filter=filter_dict
            )
            
