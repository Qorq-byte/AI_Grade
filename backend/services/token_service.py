"""
Token消耗记录服务
每次LLM调用后自动记录token使用量
"""
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from loguru import logger


def record_token_usage(
    db: Session,
    user_id: int,
    model_name: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    session_id: Optional[str] = None,
    project_id: Optional[int] = None,
    step_number: Optional[int] = None,
    cost: float = 0.0,
) -> int:
    """
    记录一次LLM调用的token消耗
    
    Args:
        db: 数据库会话
        user_id: 用户ID
        model_name: 使用的模型名称
        prompt_tokens: 输入token数
        completion_tokens: 输出token数
        total_tokens: 总token数
        session_id: 工作流会话ID
        project_id: 项目ID
        step_number: 当前步骤号
        cost: 成本(元)
    
    Returns:
        TokenUsage记录的ID
    """
    from backend.models.user_models import TokenUsage, User
    
    try:
        # 1. 记录TokenUsage详细记录
        record = TokenUsage(
            user_id=user_id,
            session_id=session_id,
            project_id=project_id,
            step_number=step_number,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=cost,
            created_at=datetime.now(timezone.utc),
        )
        db.add(record)
        db.flush()  # 获取ID但不提交
        
        # 2. 更新用户的累计token消耗
        if total_tokens > 0:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.tokens_used = (user.tokens_used or 0) + total_tokens
        
        # 3. 更新WorkflowSession的token统计
        if session_id:
            from backend.models.user_models import WorkflowSession
            ws = db.query(WorkflowSession).filter(
                WorkflowSession.session_id == session_id
            ).first()
            if ws:
                ws.token_used = (ws.token_used or 0) + total_tokens
        
        db.commit()
        logger.debug(
            f"Token记录: user={user_id}, model={model_name}, "
            f"prompt={prompt_tokens}, completion={completion_tokens}, total={total_tokens}"
        )
        return record.id
        
    except Exception as e:
        logger.error("记录token消耗失败: {}", str(e))
        try:
            db.rollback()
        except Exception:
            pass
        return -1


def extract_token_usage_from_response(response: Any) -> Dict[str, int]:
    """
    从LangChain响应对象中提取真实token用量
    
    Args:
        response: LLM调用返回的响应对象
    
    Returns:
        dict with prompt_tokens, completion_tokens, total_tokens
    """
    result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    
