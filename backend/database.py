"""
数据库连接和会话管理
使用SQLAlchemy ORM操作MySQL数据库
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from loguru import logger
from backend.config import settings

# 创建数据库引擎
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=max(20, settings.DB_POOL_SIZE * 2),
    pool_timeout=30,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,
    echo=False,  # 生产环境不输出SQL
)

# 创建会话工厂
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# 创建基类
Base = declarative_base()


def get_db():
    """
    获取数据库会话
    用于FastAPI依赖注入
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    初始化数据库
    创建所有表
    """
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("数据库表创建成功")
        # create_all 只建新表，不给已存在的表补列；老库升级靠这段幂等补丁（批次 24）
        from backend.utils.schema_patches import apply_schema_patches
        apply_schema_patches(engine)
    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")
        raise


def test_connection():
    """
    测试数据库连接
    """
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        logger.info("数据库连接测试成功")
        return True
    except Exception as e:
        logger.error(f"数据库连接测试失败: {str(e)}")
        return False
