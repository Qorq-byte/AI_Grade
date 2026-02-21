"""
共享操作日志工具 — 消除 client_routes / admin_routes / auth_routes 中的重复 _log_operation。
"""
from sqlalchemy.orm import Session
from loguru import logger
from backend.models.user_models import OperationLog


def log_operation(
    db: Session,
    user_id: int,
    username: str,
    action: str,
    resource_type: str = None,
    resource_id: str = None,
    detail: str = None,
    ip_address: str = None,
):
    """写入操作日志（自带 commit / rollback）"""
    try:
        log = OperationLog(
            user_id=user_id,
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            ip_address=ip_address,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"记录操作日志失败: {e}")