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
    
    try:
        # LangChain ChatOpenAI 的 response_metadata
        if hasattr(response, 'response_metadata'):
            meta = response.response_metadata
            if 'token_usage' in meta:
                usage = meta['token_usage']
                result["prompt_tokens"] = usage.get('prompt_tokens', 0) or 0
                result["completion_tokens"] = usage.get('completion_tokens', 0) or 0
                result["total_tokens"] = usage.get('total_tokens', 0) or 0
                return result
        
        # 尝试从 usage_metadata 获取
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            if usage:
                result["prompt_tokens"] = usage.get('input_tokens', 0) or 0
                result["completion_tokens"] = usage.get('output_tokens', 0) or 0
                result["total_tokens"] = usage.get('total_tokens', 0) or 0
                return result
        
        # 尝试从 additional_kwargs 获取
        if hasattr(response, 'additional_kwargs'):
            ak = response.additional_kwargs
            if 'usage' in ak:
                u = ak['usage']
                result["prompt_tokens"] = u.get('prompt_tokens', 0) or 0
                result["completion_tokens"] = u.get('completion_tokens', 0) or 0
                result["total_tokens"] = u.get('total_tokens', 0) or 0
                return result
    except Exception as e:
        logger.info("提取token用量失败（非关键）: model={} error={}", 
                     getattr(response, 'model', 'unknown'), str(e))
    
    return result


def get_user_token_usage(
    db: Session,
    user_id: int,
    days: int = 30
) -> Dict[str, Any]:
    """
    获取用户的token使用统计
    
    Args:
        db: 数据库会话
        user_id: 用户ID
        days: 统计最近N天
    
    Returns:
        统计结果字典
    """
    from backend.models.user_models import User, TokenUsage
    from sqlalchemy import func, text
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"total_tokens": 0, "daily_usage": [], "by_model": []}
    
    # 每日使用统计
    daily = (
        db.query(
            func.date(TokenUsage.created_at).label('date'),
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.count(TokenUsage.id).label('calls'),
        )
        .filter(
            TokenUsage.user_id == user_id,
            TokenUsage.created_at >= text("DATE_SUB(NOW(), INTERVAL :days DAY)")
        )
        .params(days=days)
        .group_by(func.date(TokenUsage.created_at))
        .order_by(func.date(TokenUsage.created_at).desc())
        .all()
    )
    
    # 按模型统计
    by_model = (
        db.query(
            TokenUsage.model_name,
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.count(TokenUsage.id).label('calls'),
        )
        .filter(TokenUsage.user_id == user_id)
        .group_by(TokenUsage.model_name)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .all()
    )
    
    return {
        "total_tokens": user.tokens_used or 0,
        "daily_usage": [
            {"date": str(d.date), "tokens": d.tokens, "calls": d.calls}
            for d in daily
        ],
        "by_model": [
            {"model": m.model_name, "tokens": m.tokens, "calls": m.calls}
            for m in by_model
        ],
    }