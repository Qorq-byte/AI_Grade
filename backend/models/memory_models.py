"""
记忆相关的数据模型
包含短期记忆、长期记忆和会话管理
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class UserSession(Base):
    """用户会话表"""
    __tablename__ = "user_sessions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True)
    user_id = Column(String(100), nullable=True, index=True)  # 可选的用户ID
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    # 关系
    short_term_memories = relationship("ShortTermMemory", back_populates="session", cascade="all, delete-orphan")
    long_term_memories = relationship("LongTermMemory", back_populates="session", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<UserSession(id={self.id}, session_id={self.session_id})>"


class ShortTermMemory(Base):
    """
    短期记忆表
    存储最近的对话历史，用于维持上下文
    """
    __tablename__ = "short_term_memories"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), ForeignKey("user_sessions.session_id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # 'user' 或 'assistant'
    content = Column(Text, nullable=False)
    tokens = Column(Integer, default=0)  # token数量
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    session = relationship("UserSession", back_populates="short_term_memories")
    
    # 索引
    __table_args__ = (
        Index('idx_session_created', 'session_id', 'created_at'),
    )
    
    def __repr__(self):
        return f"<ShortTermMemory(id={self.id}, role={self.role}, tokens={self.tokens})>"


class LongTermMemory(Base):
    """
    长期记忆表
    存储从对话中提取的重要信息和总结
    """
    __tablename__ = "long_term_memories"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), ForeignKey("user_sessions.session_id"), nullable=False, index=True)
    memory_type = Column(String(50), nullable=False)  # 'fact', 'preference', 'summary', 'knowledge'
    category = Column(String(100), nullable=True)  # 记忆分类
    content = Column(Text, nullable=False)
    importance_score = Column(Float, default=0.5)  # 重要性评分 0-1
    access_count = Column(Integer, default=0)  # 访问次数
    last_accessed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    # 关系
    session = relationship("UserSession", back_populates="long_term_memories")
    
    # 索引
    __table_args__ = (
        Index('idx_memory_type', 'memory_type'),
        Index('idx_importance', 'importance_score'),
        Index('idx_category', 'category'),
    )
    
    def __repr__(self):
        return f"<LongTermMemory(id={self.id}, type={self.memory_type}, importance={self.importance_score})>"


class ConversationSummary(Base):
    """
    对话总结表
    定期生成的对话摘要，用于压缩上下文
    """
    __tablename__ = "conversation_summaries"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), ForeignKey("user_sessions.session_id"), nullable=False, index=True)
    summary_content = Column(Text, nullable=False)
    start_message_id = Column(Integer, nullable=True)  # 总结覆盖的起始消息ID
    end_message_id = Column(Integer, nullable=True)  # 总结覆盖的结束消息ID
    tokens_saved = Column(Integer, default=0)  # 节省的token数
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    session = relationship("UserSession")
    
    def __repr__(self):
        return f"<ConversationSummary(id={self.id}, tokens_saved={self.tokens_saved})>"
