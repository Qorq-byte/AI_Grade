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
        
    Returns:
        模型信息字典
    """
    provider = get_model_provider(model_name)
    if not provider:
        return None
    
    provider_info = SUPPORTED_MODELS.get(provider)
    if not provider_info:
        return None
    
    return {
        "model": model_name,
        "provider": provider_info["provider"],
        "category": provider,
        "config_key": provider_info["config_key"]
    }


def validate_api_key(provider: str, api_key: str) -> bool:
    """
    验证API密钥是否有效（简单验证）

    Args:
        provider: 提供商名称
        api_key: API密钥

    Returns:
        是否有效
    """
    if not api_key or api_key.strip() == "":
        logger.warning(f"{provider} API密钥未配置")
        return False

    # 简单的格式验证
    if len(api_key) < 10:
        logger.warning(f"{provider} API密钥格式可能不正确")
        return False

    return True


def get_model_credentials(model_name: str) -> dict:
    """
    根据模型名称返回对应的API密钥和base_url
    统一管理所有LLM提供商的凭证，避免硬编码

    Args:
        model_name: 模型名称

    Returns:
        包含 api_key 和 base_url 的字典
    """
    from backend.config import settings
    import os

    provider = get_model_provider(model_name)

    if provider == "deepseek":
        return {
            "api_key": os.environ.get("DEEPSEEK_API_KEY", "") or settings.DEEPSEEK_API_KEY,
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", "") or settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1",
        }
    elif provider == "openai":
        return {
            "api_key": os.environ.get("OPENAI_API_KEY", "") or settings.OPENAI_API_KEY,
            "base_url": "https://api.openai.com/v1",
        }
    elif provider == "anthropic":
        return {
            "api_key": os.environ.get("ANTHROPIC_API_KEY", "") or settings.ANTHROPIC_API_KEY,
            "base_url": "https://api.anthropic.com",
        }
    else:
        # 默认使用通义千问（DashScope 兼容模式）
        return {
            "api_key": os.environ.get("DASHSCOPE_API_KEY", "") or settings.DASHSCOPE_API_KEY,
            "base_url": os.environ.get("DASHSCOPE_BASE_URL", "") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }


if __name__ == "__main__":
    # 测试
    print("支持的模型列表:")
    for model in get_supported_models():
        print(f"  - {model['model']} ({model['provider']})")
    
    print("\n模型检测测试:")
    test_models = ["gpt-4", "claude-3-sonnet", "qwen-plus", "deepseek-chat"]
    for model in test_models:
        info = get_model_info(model)
        if info:
            print(f"  {model}: {info['provider']}")
        else:
            print(f"  {model}: 不支持")
