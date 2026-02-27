"""
文件保留与数据清理服务
- 工作流完成后90天自动清除文件和数据库记录
- 支持手动触发清理
- 支持单个用户/项目清理
"""
import os
import shutil
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func
from loguru import logger

from backend.models.user_models import (
    User, Project, ProjectFile, WorkflowSession, Report, TokenUsage,
    OperationLog, Notification, AiConversation, AiMessage,
)


class CleanupService:
    """文件保留与数据清理服务"""

    RETENTION_DAYS = 90  # 默认保留90天

    @staticmethod
    def cleanup_expired_files(db: Session, retention_days: int = None) -> Dict:
        """
        清理过期的工作流文件和数据记录
        
        Args:
            db: 数据库会话
            retention_days: 保留天数，默认90天
        
        Returns:
            清理统计结果
        """
        days = retention_days or CleanupService.RETENTION_DAYS
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

        stats = {
            "retention_days": days,
            "cutoff_date": cutoff_date.isoformat(),
            "deleted_files": 0,
            "deleted_sessions": 0,
            "deleted_reports": 0,
            "freed_disk_bytes": 0,
            "errors": [],
        }

        try:
            # 1. 查找已过期的工作流会话（completed/failed + 超过保留期）
            expired_sessions = (
                db.query(WorkflowSession)
                .filter(
                    WorkflowSession.status.in_(["completed", "failed"]),
                    WorkflowSession.completed_at.isnot(None),
                    WorkflowSession.completed_at < cutoff_date,
                )
                .order_by(WorkflowSession.completed_at)
                .all()
            )

            expired_session_ids = [s.session_id for s in expired_sessions]
            expired_user_ids = list(set(s.user_id for s in expired_sessions))
            deleted_user_count = len(expired_user_ids)

            if not expired_session_ids:
                logger.info("没有找到过期的文件需要清理")
                return stats

            logger.info(
                f"开始清理: {len(expired_sessions)} 个过期会话 "
                f"(保留期{days}天, 截止日期{cutoff_date.strftime('%Y-%m-%d')})"
            )

            # 2. 清理关联文件（磁盘 + 数据库）
            expired_files = (
                db.query(ProjectFile)
                .filter(ProjectFile.created_at < cutoff_date)
                .all()
            )

