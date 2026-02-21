"""
LLM模型配置和辅助函数
提供模型信息、能力检测和配置管理
"""
from typing import Dict, List, Optional
from loguru import logger


# 支持的模型列表
SUPPORTED_MODELS = {
    "openai": {
        "models": ["gpt-3.5-turbo", "gpt-4", "gpt-4-turbo", "gpt-4o"],
        "provider": "OpenAI",
        "config_key": "OPENAI_API_KEY"
    },
    "anthropic": {
        "models": ["claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-2.1"],
        "provider": "Anthropic",
        "config_key": "ANTHROPIC_API_KEY"
    },
    "qwen": {
        "models": ["qwen-turbo", "qwen-plus", "qwen-max", "qwen-long"],
        "provider": "阿里云通义千问",
        "config_key": "DASHSCOPE_API_KEY"
    },
    "deepseek": {
        "models": ["deepseek-chat", "deepseek-coder", "deepseek-v4-flash"],
        "provider": "DeepSeek",
        "config_key": "DEEPSEEK_API_KEY"
    }
}


def get_supported_models() -> List[Dict[str, str]]:
    """
    获取所有支持的模型列表
    
    Returns:
        模型信息列表
    """
    models = []
    for provider, info in SUPPORTED_MODELS.items():
        for model_name in info["models"]:
            models.append({
                "model": model_name,
                "provider": info["provider"],
                "category": provider
            })
    return models


def get_model_provider(model_name: str) -> Optional[str]:
    """
    根据模型名称获取提供商
    
    Args:
        model_name: 模型名称
        
    Returns:
        提供商名称，如果未找到返回None
    """
    model_lower = model_name.lower()
    
    if "qwen" in model_lower:
        return "qwen"
    elif "deepseek" in model_lower:
        return "deepseek"
    elif "gpt" in model_lower or "openai" in model_lower:
        return "openai"
    elif "claude" in model_lower or "anthropic" in model_lower:
        return "anthropic"
    else:
        return None


def is_model_supported(model_name: str) -> bool:
    """
    检查模型是否支持
    
    Args:
        model_name: 模型名称
        
    Returns:
        是否支持
    """
    return get_model_provider(model_name) is not None


def get_model_info(model_name: str) -> Optional[Dict]:
    """
    获取模型详细信息
    
    Args:
        model_name: 模型名称
        
