"""
文件保留与数据清理服务
- 工作流完成后90天自动清除文件和数据库记录
- 支持手动触发清理
- 支持单个用户/项目清理
"""
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
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
    def purge_user_disk_artifacts(user_id: int, root: Optional[Path] = None) -> Dict:
        """删掉某个用户在磁盘上留下的上传件与导出件（只管磁盘，DB 由调用方负责）。

        注销用户时最容易漏的就是这一步：库里的记录删干净了，`uploads/` 和
        `backend/exports/` 里那堆文件还在。结果是「已删除」变成一句空话 ——
        空间没释放，用户数据也仍然躺在盘上。

        命名约定（写入端见 client_routes 的上传逻辑）：
        - 上传件：`uploads/u{user_id}_{uuid}_{原文件名}`
        - 导出件：`backend/exports/{user_id}/exports/*`
        两处都按「用户 id 精确匹配」，不做模糊 glob，避免误删别人的文件。
        """
        stats: Dict = {"files": 0, "bytes": 0, "dirs": 0, "errors": []}
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            stats["errors"].append(f"非法 user_id: {user_id!r}")
            return stats

        root = Path(root) if root is not None else Path(__file__).resolve().parent.parent.parent  # 项目根

        upload_dir = root / "uploads"
        if upload_dir.is_dir():
            for f in upload_dir.glob(f"u{uid}_*"):
                try:
                    if not f.is_file():
                        continue
                    size = f.stat().st_size
                    f.unlink()
                    stats["files"] += 1
                    stats["bytes"] += size
                except OSError as exc:
                    stats["errors"].append(f"删除上传文件失败 {f.name}: {exc}")

        export_root = root / "backend" / "exports"
        export_dir = export_root / str(uid)
        try:
            # 双保险：必须是 export_root 的直接子目录，防止路径拼接出界
            if export_dir.is_dir() and export_dir.parent.resolve() == export_root.resolve():
                shutil.rmtree(export_dir)
                stats["dirs"] += 1
        except OSError as exc:
            stats["errors"].append(f"删除导出目录失败 {export_dir}: {exc}")

        if stats["files"] or stats["dirs"]:
            logger.info(
                "已清理用户 {} 的磁盘残留: 文件 {} 个 / {:.2f} MB / 导出目录 {} 个",
                uid, stats["files"], stats["bytes"] / 1024 / 1024, stats["dirs"],
            )
        return stats

    @staticmethod
    def _cleanup_upload_dirs(cutoff_date: datetime, stats: Dict) -> None:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for name in ("uploads", "backend/uploads", "frontend/uploads"):
            upload_dir = os.path.join(base_dir, name)
            for root, _, files in os.walk(upload_dir, topdown=False):
                for filename in files:
                    path = os.path.join(root, filename)
                    try:
                        info = os.stat(path)
                        if datetime.fromtimestamp(info.st_mtime, tz=timezone.utc) < cutoff_date:
                            os.remove(path)
                            stats["freed_disk_bytes"] += info.st_size
                            stats["deleted_files"] += 1
                    except OSError as exc:
                        stats["errors"].append(f"删除上传文件失败 {path}: {exc}")
                if root != upload_dir:
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass

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
                CleanupService._cleanup_upload_dirs(cutoff_date, stats)
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

            CleanupService._cleanup_upload_dirs(cutoff_date, stats)

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

            # 4. 清理过期Project（无关联文件的）
            expired_projects = db.query(Project).filter(
                Project.created_at < cutoff_date
            ).all()
            for proj in expired_projects:
                # 检查是否有关联的活跃工作流会话
                active_count = db.query(func.count(WorkflowSession.id)).filter(
                    WorkflowSession.user_id == proj.user_id,
                    WorkflowSession.project_name == proj.project_name,
                    WorkflowSession.status.in_(["created", "running", "paused"]),
                ).scalar() or 0
                if active_count == 0:
                    db.delete(proj)
                    stats["deleted_reports"] += 1

            db.commit()
            logger.info(
                f"文件清理完成: 释放磁盘 {stats['freed_disk_bytes'] / (1024*1024):.2f} MB, "
                f"删除文件 {stats['deleted_files']} 个"
            )

        except Exception as e:
            db.rollback()
            logger.error(f"文件清理失败: {e}")
            stats["errors"].append(f"整体清理失败: {e}")

        return stats

    @staticmethod
    def cleanup_user_data(db: Session, user_id: int) -> Dict:
        """
        清理指定用户的所有过期数据
        
        Args:
            db: 数据库会话
            user_id: 用户ID
        
        Returns:
            清理统计
        """
        stats = {"user_id": user_id, "deleted_files": 0, "deleted_records": 0}

        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return {"error": "用户不存在"}

            cutoff_date = datetime.now(timezone.utc) - timedelta(
                days=CleanupService.RETENTION_DAYS
            )

            # 清理用户的过期文件
            files = db.query(ProjectFile).filter(
                ProjectFile.user_id == user_id,
                ProjectFile.created_at < cutoff_date,
            ).all()

            for pf in files:
                if pf.file_path and os.path.exists(pf.file_path):
                    try:
                        os.remove(pf.file_path)
                        stats["deleted_files"] += 1
                    except OSError:
                        pass
                db.delete(pf)

            # 清理过期会话
            expired = db.query(WorkflowSession).filter(
                WorkflowSession.user_id == user_id,
                WorkflowSession.status.in_(["completed", "failed"]),
                WorkflowSession.completed_at < cutoff_date,
            ).all()

            for ws in expired:
                db.delete(ws)
                stats["deleted_records"] += 1

            db.commit()
            logger.info(f"用户 {user_id} 数据清理完成: {stats}")

        except Exception as e:
            db.rollback()
            logger.error(f"用户 {user_id} 数据清理失败: {e}")
            stats["error"] = str(e)

        return stats

    @staticmethod
    def get_cleanup_preview(db: Session, retention_days: int = None) -> Dict:
        """
        预览即将被清理的数据量（不执行删除）
        
        Args:
            db: 数据库会话
            retention_days: 保留天数
        
        Returns:
            预览统计
        """
        days = retention_days or CleanupService.RETENTION_DAYS
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

        expired_sessions_count = db.query(func.count(WorkflowSession.id)).filter(
            WorkflowSession.status.in_(["completed", "failed"]),
            WorkflowSession.completed_at.isnot(None),
            WorkflowSession.completed_at < cutoff_date,
        ).scalar() or 0

        expired_files_count = db.query(func.count(ProjectFile.id)).filter(
            ProjectFile.created_at < cutoff_date,
        ).scalar() or 0

        expired_reports_count = db.query(func.count(Report.id)).filter(
            Report.created_at < cutoff_date,
        ).scalar() or 0

        # 估算磁盘空间
        files = db.query(ProjectFile.file_path).filter(
            ProjectFile.created_at < cutoff_date,
        ).all()

        estimated_size = 0
        for (fp,) in files:
            if fp and os.path.exists(fp):
                try:
                    estimated_size += os.path.getsize(fp)
                except OSError:
                    pass

        return {
            "retention_days": days,
            "cutoff_date": cutoff_date.isoformat(),
            "expired_sessions": expired_sessions_count,
            "expired_files": expired_files_count,
            "expired_reports": expired_reports_count,
            "estimated_disk_mb": round(estimated_size / (1024 * 1024), 2),
        }

    @staticmethod
    def cleanup_failed_workflows(db: Session, max_age_hours: int = 24) -> int:
        """
        清理卡住的失败工作流（超过指定小时仍未completed的running状态）
        
        Args:
            db: 数据库会话
            max_age_hours: 最大允许的运行时间（小时）
        
        Returns:
            清理的工作流数量
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        stuck = db.query(WorkflowSession).filter(
            WorkflowSession.status.in_(["running", "paused"]),
            WorkflowSession.created_at < cutoff,
        ).all()

        count = 0
        for ws in stuck:
            ws.status = "failed"
            ws.completed_at = datetime.now(timezone.utc)
            count += 1

        if count > 0:
            db.commit()
            logger.info(f"清理了 {count} 个卡住的工作流")

        return count