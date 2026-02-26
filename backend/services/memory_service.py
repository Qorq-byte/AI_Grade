"""
记忆管理服务
负责短期记忆和长期记忆的存储、检索和管理
"""
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from loguru import logger
import json

from backend.models.memory_models import (
    UserSession,
    ShortTermMemory,
    LongTermMemory,
    ConversationSummary
)


class MemoryService:
    """记忆服务类"""
    
    def __init__(self, db: Session):
        self.db = db
    
    # ==================== 会话管理 ====================
    
    def create_session(self, session_id: str, user_id: Optional[str] = None) -> UserSession:
        """创建新会话"""
        session = UserSession(
            session_id=session_id,
            user_id=user_id,
            created_at=datetime.now(timezone.utc),
            is_active=True
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        logger.info(f"创建新会话: {session_id}")
        return session
    
    def get_session(self, session_id: str) -> Optional[UserSession]:
        """获取会话"""
        return self.db.query(UserSession).filter(
            UserSession.session_id == session_id
        ).first()
    
    def close_session(self, session_id: str):
        """关闭会话"""
        session = self.get_session(session_id)
        if session:
            session.is_active = False
            session.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            logger.info(f"关闭会话: {session_id}")
    
    # ==================== 短期记忆管理 ====================
    
    def add_short_term_memory(
        self,
        session_id: str,
        role: str,
        content: str,
        tokens: int = 0
    ) -> ShortTermMemory:
        """添加短期记忆（对话消息）"""
        memory = ShortTermMemory(
            session_id=session_id,
            role=role,
            content=content,
            tokens=tokens,
            created_at=datetime.now(timezone.utc)
        )
        self.db.add(memory)
        self.db.commit()
        self.db.refresh(memory)
        return memory
    
    def get_recent_memories(
        self,
        session_id: str,
        limit: int = 20
    ) -> List[ShortTermMemory]:
        """获取最近的短期记忆"""
        return self.db.query(ShortTermMemory).filter(
            ShortTermMemory.session_id == session_id
        ).order_by(
            ShortTermMemory.created_at.desc()
        ).limit(limit).all()
    
