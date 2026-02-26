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
    
    def get_conversation_history(
        self,
        session_id: str,
        limit: int = 50
    ) -> List[Dict[str, str]]:
        """获取对话历史（格式化为LLM可用格式）"""
        memories = self.db.query(ShortTermMemory).filter(
            ShortTermMemory.session_id == session_id
        ).order_by(
            ShortTermMemory.created_at.asc()
        ).limit(limit).all()
        
        return [
            {"role": m.role, "content": m.content}
            for m in memories
        ]
    
    def clear_old_short_term_memories(
        self,
        session_id: str,
        keep_latest: int = 50
    ):
        """清理旧的短期记忆，保留最新的N条"""
        memories = self.db.query(ShortTermMemory).filter(
            ShortTermMemory.session_id == session_id
        ).order_by(
            ShortTermMemory.created_at.desc()
        ).all()
        
        if len(memories) > keep_latest:
            old_memories = memories[keep_latest:]
            for memory in old_memories:
                self.db.delete(memory)
            self.db.commit()
            logger.info(f"清理会话 {session_id} 的 {len(old_memories)} 条旧记忆")
    
    # ==================== 长期记忆管理 ====================
    
    def add_long_term_memory(
        self,
        session_id: str,
        memory_type: str,
        content: str,
        category: Optional[str] = None,
        importance_score: float = 0.5
    ) -> LongTermMemory:
        """添加长期记忆"""
        memory = LongTermMemory(
            session_id=session_id,
            memory_type=memory_type,
            content=content,
            category=category,
            importance_score=importance_score,
            created_at=datetime.now(timezone.utc)
        )
        self.db.add(memory)
        self.db.commit()
        self.db.refresh(memory)
        logger.info(f"添加长期记忆: type={memory_type}, importance={importance_score}")
        return memory
    
    def get_relevant_long_term_memories(
        self,
        session_id: str,
        memory_type: Optional[str] = None,
        category: Optional[str] = None,
        min_importance: float = 0.3,
        limit: int = 10
    ) -> List[LongTermMemory]:
        """获取相关的长期记忆"""
        query = self.db.query(LongTermMemory).filter(
            LongTermMemory.session_id == session_id,
            LongTermMemory.is_active == True,
            LongTermMemory.importance_score >= min_importance
        )
        
        if memory_type:
            query = query.filter(LongTermMemory.memory_type == memory_type)
        if category:
            query = query.filter(LongTermMemory.category == category)
        
        memories = query.order_by(
            LongTermMemory.importance_score.desc(),
            LongTermMemory.last_accessed_at.desc()
        ).limit(limit).all()
        
