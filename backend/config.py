"""
配置管理模块
加载环境变量并提供统一的配置访问接口
"""
from pydantic_settings import BaseSettings
from typing import List
from dotenv import load_dotenv
from urllib.parse import quote_plus
import os

# 加载环境变量
load_dotenv()

# 确保默认数据库名称，防止被系统环境变量覆盖
os.environ.setdefault("MYSQL_DATABASE", "ai_grade_db")


class Settings(BaseSettings):
    """应用配置类"""
    
    # Application Settings
    APP_NAME: str = "云海睿评 - AI绩效评价报告生成系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # LLM API Configuration
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    DASHSCOPE_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    
    # LLM API Base URLs
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    
    # ─── 自定义模型列表（逗号分隔） ───
    QWEN_MODELS: str = "qwen3-max"
    DEEPSEEK_MODELS: str = ""
    OPENAI_MODELS: str = ""
    ANTHROPIC_MODELS: str = ""

    # LangSmith Configuration
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "ai-grade-performance-evaluation"
    LANGCHAIN_ENDPOINT: str = "https://api.smith.langchain.com"
    
    # Database Configuration
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "ai_grade_db"
    CHROMA_DB_PATH: str = "./backend/data/chroma_db"
    REDIS_URL: str = "redis://localhost:6379/0"
    
