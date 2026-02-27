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

            for pf in expired_files:
                try:
                    if pf.file_path and os.path.exists(pf.file_path):
                        file_size = os.path.getsize(pf.file_path)
                        os.remove(pf.file_path)
                        stats["freed_disk_bytes"] += file_size
                        stats["deleted_files"] += 1
                except OSError as e:
                    stats["errors"].append(f"删除文件失败 {pf.file_path}: {e}")

            # 数据库删除文件记录
            db.query(ProjectFile).filter(
                ProjectFile.created_at < cutoff_date
            ).delete(synchronize_session=False)

            # 删除上传目录中的过期文件
            upload_dirs = ["uploads", "backend/uploads", "frontend/uploads"]
            for upload_dir_name in upload_dirs:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)
                )))
                upload_dir = os.path.join(base_dir, upload_dir_name)
                if not os.path.exists(upload_dir):
                    continue
                for root, dirs, files in os.walk(upload_dir, topdown=False):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        try:
                            mtime = datetime.fromtimestamp(
                                os.path.getmtime(fpath), tz=timezone.utc
                            )
                            if mtime < cutoff_date:
                                file_size = os.path.getsize(fpath)
                                os.remove(fpath)
                                stats["freed_disk_bytes"] += file_size
                                stats["deleted_files"] += 1
                        except OSError as e:
                            stats["errors"].append(f"删除上传文件失败 {fpath}: {e}")
                    # 删除空目录
                    if not os.listdir(root):
                        try:
                            os.rmdir(root)
                        except OSError:
                            pass

            # 3. 清理数据库记录

            # 3a. 删除TokenUsage记录
            db.query(TokenUsage).filter(
                TokenUsage.created_at < cutoff_date
            ).delete(synchronize_session=False)

            # 3b. 删除过期会话的Notification
            if expired_user_ids:
                db.query(Notification).filter(
                    Notification.created_at < cutoff_date
                ).delete(synchronize_session=False)

            # 3c. 删除过期OperationLog
            db.query(OperationLog).filter(
                OperationLog.created_at < cutoff_date
            ).delete(synchronize_session=False)

            # 3d. 删除过期WorkflowSession
            if expired_session_ids:
                from backend.models.user_models import ReportModuleSequence
                try:
                    db.query(ReportModuleSequence).filter(
                        ReportModuleSequence.session_id.in_(expired_session_ids)
                    ).delete(synchronize_session=False)
                except Exception:
                    pass

            db.query(WorkflowSession).filter(
                WorkflowSession.completed_at.isnot(None),
                WorkflowSession.completed_at < cutoff_date,
            ).delete(synchronize_session=False)

            # 3e. 删除过期Report
            db.query(Report).filter(
                Report.created_at < cutoff_date
            ).delete(synchronize_session=False)

