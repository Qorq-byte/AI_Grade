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
    
    @property
    def DATABASE_URL(self) -> str:
        """获取MySQL数据库连接URL"""
        encoded_password = quote_plus(self.MYSQL_PASSWORD)
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{encoded_password}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
        )
    
    # JWT Configuration
    JWT_SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24小时
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # CORS Configuration
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173,http://localhost:8000"
    
    # Model Parameters
    DEFAULT_MODEL: str = "deepseek-chat"  # 与 .env.example / README 保持一致
    TEMPERATURE: float = 0.7
    TOP_P: float = 0.9
    MAX_TOKENS: int = 4000
    
    # ─── 连接池配置 ───
    DB_POOL_SIZE: int = 10
    DB_POOL_RECYCLE: int = 3600
    
    class Config:
        env_file = ".env"
        case_sensitive = True
    
    def get_allowed_origins(self) -> List[str]:
        """获取CORS允许的源列表"""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",")]

    def validate_required_settings(self) -> list[str]:
        """校验生产环境必需的配置项，返回缺失项的列表"""
        issues = []
        if not self.JWT_SECRET_KEY:
            issues.append("JWT_SECRET_KEY 未配置")
        if not self.MYSQL_PASSWORD:
            issues.append("MYSQL_PASSWORD 未配置")
        if not self.DASHSCOPE_API_KEY and not self.OPENAI_API_KEY and not self.DEEPSEEK_API_KEY:
            issues.append("至少需要一个LLM API Key")
        return issues


# 创建全局配置实例
settings = Settings()
