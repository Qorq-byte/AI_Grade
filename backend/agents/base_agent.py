"""
BaseAgent - Agent基类
提供统一的Agent接口，支持多模型、长短期记忆管理
"""
from typing import Optional, List, Dict, Any
from abc import ABC, abstractmethod
from langchain_core.language_models import BaseLanguageModel
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_community.chat_models import ChatTongyi
from loguru import logger
import uuid
import time

from backend.config import settings
from backend.services.memory_service import MemoryService
from backend.database import SessionLocal
from backend.utils.llm_helper import get_model_provider, validate_api_key


class BaseAgent(ABC):
    """
    Agent基类
    所有具体Agent都应继承此类
    """
    
    def __init__(
        self,
        session_id: Optional[str] = None,
        model_name: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        user_id: Optional[str] = None
    ):
        """
        初始化Agent
        
        Args:
            session_id: 会话ID，如果不提供则自动生成
            model_name: 模型名称
            temperature: 温度参数
            top_p: Top-P参数
            max_tokens: 最大token数
            user_id: 用户ID（可选）
        """
        # 会话管理
        self.session_id = session_id or str(uuid.uuid4())
        self.user_id = user_id
        
        # 模型配置
        self.model_name = model_name or settings.DEFAULT_MODEL
        self.temperature = temperature if temperature is not None else settings.TEMPERATURE
        self.top_p = top_p if top_p is not None else settings.TOP_P
        self.max_tokens = max_tokens or settings.MAX_TOKENS
        
        # 初始化LLM
        self.llm = self._initialize_llm()
        
        # 初始化数据库会话和记忆服务
        self.db_session = SessionLocal()
        self.memory_service = MemoryService(self.db_session)
        
        # 创建或获取会话
        self._ensure_session()
        
        logger.info(f"BaseAgent初始化完成: session_id={self.session_id}, model={self.model_name}")
    
    def _initialize_llm(self) -> BaseLanguageModel:
        """
        初始化语言模型
        根据model_name选择不同的LLM提供商
        
        支持的模型：
        - OpenAI: gpt-3.5-turbo, gpt-4, gpt-4-turbo
        - Anthropic: claude-3-opus, claude-3-sonnet, claude-3-haiku
        - 通义千问: qwen-turbo, qwen-plus, qwen-max, qwen-long
        - DeepSeek: deepseek-chat, deepseek-coder
        """
        try:
            model_lower = self.model_name.lower()
            provider = get_model_provider(self.model_name)
            
            # 通义千问系列
            if provider == "qwen":
                if not validate_api_key("通义千问", settings.DASHSCOPE_API_KEY):
                    raise ValueError("通义千问API密钥未配置或无效")
                
                llm = ChatTongyi(
                    model=self.model_name,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    dashscope_api_key=settings.DASHSCOPE_API_KEY
                )
                logger.info(f"使用通义千问模型: {self.model_name}")
            
            # DeepSeek系列
            elif provider == "deepseek":
                if not validate_api_key("DeepSeek", settings.DEEPSEEK_API_KEY):
                    raise ValueError("DeepSeek API密钥未配置或无效")
                
                llm = ChatOpenAI(
                    model=self.model_name,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                    openai_api_key=settings.DEEPSEEK_API_KEY,
                    openai_api_base=settings.DEEPSEEK_BASE_URL
                )
                logger.info(f"使用DeepSeek模型: {self.model_name}")
            
            # OpenAI系列
            elif provider == "openai":
                if not validate_api_key("OpenAI", settings.OPENAI_API_KEY):
                    raise ValueError("OpenAI API密钥未配置或无效")
                
                llm = ChatOpenAI(
                    model=self.model_name,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                    openai_api_key=settings.OPENAI_API_KEY
                )
                logger.info(f"使用OpenAI模型: {self.model_name}")
            
            # Anthropic系列
            elif provider == "anthropic":
                if not validate_api_key("Anthropic", settings.ANTHROPIC_API_KEY):
                    raise ValueError("Anthropic API密钥未配置或无效")
                
                llm = ChatAnthropic(
                    model=self.model_name,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                    anthropic_api_key=settings.ANTHROPIC_API_KEY
                )
                logger.info(f"使用Anthropic模型: {self.model_name}")
            
            else:
                # 默认使用OpenAI
                if not validate_api_key("OpenAI", settings.OPENAI_API_KEY):
                    raise ValueError("OpenAI API密钥未配置或无效")
                
                llm = ChatOpenAI(
                    model=self.model_name,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                    openai_api_key=settings.OPENAI_API_KEY
                )
                logger.warning(f"未知模型类型，使用OpenAI默认模型: {self.model_name}")
            
            return llm
            
        except Exception as e:
            logger.error(f"LLM初始化失败: {str(e)}")
            raise
    
    def _ensure_session(self):
        """确保会话存在"""
        session = self.memory_service.get_session(self.session_id)
        if not session:
            self.memory_service.create_session(
                session_id=self.session_id,
                user_id=self.user_id
            )
    
    def chat(
        self,
        message: str,
        use_memory: bool = True
    ) -> Dict[str, Any]:
        """
        与Agent对话
        
        Args:
            message: 用户消息
            use_memory: 是否使用记忆
            
        Returns:
            包含回复和元数据的字典
        """
        start_time = time.time()
        
        try:
            # 1. 保存用户消息到短期记忆
            if use_memory:
                self.memory_service.add_short_term_memory(
                    session_id=self.session_id,
                    role="user",
                    content=message,
                    tokens=self._estimate_tokens(message)
                )
            
            # 2. 获取对话历史
            conversation_history = []
            if use_memory:
                conversation_history = self.memory_service.get_conversation_history(
                    session_id=self.session_id,
                    limit=50
                )
                
                # 获取相关的长期记忆
                long_term_memories = self.memory_service.get_relevant_long_term_memories(
                    session_id=self.session_id,
                    limit=5
                )
                
                # 如果有长期记忆，添加到系统提示中
                if long_term_memories:
                    memory_context = "\n".join([
                        f"- {m.content}" for m in long_term_memories
                    ])
                    system_prompt = f"相关背景信息:\n{memory_context}"
                else:
                    system_prompt = None
            else:
                system_prompt = None
            
            # 3. 构建消息列表
            messages = self._build_messages(
                user_message=message,
                history=conversation_history,
                system_prompt=system_prompt
            )
            
            # 4. 调用LLM生成回复（带超时控制）
            try:
                response = self.llm.invoke(messages, config={"timeout": 120})
            except Exception as llm_err:
                timeout_msg = "LLM调用超时" if "timeout" in str(llm_err).lower() else f"LLM调用失败: {str(llm_err)}"
                logger.error(f"LLM调用异常: {timeout_msg}")
                return {
                    "success": False,
                    "message": timeout_msg,
                    "response_time": time.time() - start_time,
                    "error": str(llm_err),
                    "tokens_used": 0
                }
            assistant_message = response.content
            
            # 5. 保存助手回复到短期记忆
            if use_memory:
                self.memory_service.add_short_term_memory(
                    session_id=self.session_id,
                    role="assistant",
                    content=assistant_message,
                    tokens=self._estimate_tokens(assistant_message)
                )
            
            # 6. 计算响应时间
            response_time = time.time() - start_time
            
            logger.info(f"对话完成: response_time={response_time:.2f}s")
            
            return {
                "success": True,
                "session_id": self.session_id,
                "message": assistant_message,
                "model": self.model_name,
                "response_time": response_time,
                "tokens_used": self._estimate_tokens(message) + self._estimate_tokens(assistant_message)
            }
            
        except Exception as e:
            logger.error(f"对话失败: {str(e)}")
            return {
                "success": False,
                "session_id": self.session_id,
                "message": f"错误: {str(e)}",
                "error": str(e)
            }
    
    def _build_messages(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        system_prompt: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """
        构建消息列表
        
        Args:
            user_message: 当前用户消息
            history: 对话历史
            system_prompt: 系统提示
            
        Returns:
            格式化的消息列表
        """
        messages = []
        
        # 添加系统提示
        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })
        
        # 添加对话历史
        messages.extend(history)
        
        # 添加当前用户消息
        messages.append({
            "role": "user",
            "content": user_message
        })
        
        return messages
    
    def _estimate_tokens(self, text: str) -> int:
        """
        估算文本的token数量
        简单估算：中文约1.5字符/token，英文约4字符/token
        """
        if not text:
            return 0
        
        # 简化估算
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
        return max(estimated_tokens, 1)
    
    def clear_memory(self, keep_long_term: bool = True):
        """
        清理记忆
        
        Args:
            keep_long_term: 是否保留长期记忆
        """
        if keep_long_term:
            # 只清理短期记忆
            self.memory_service.clear_old_short_term_memories(
                session_id=self.session_id,
                keep_latest=0
            )
            logger.info(f"已清理会话 {self.session_id} 的短期记忆")
        else:
            # 关闭会话
            self.memory_service.close_session(self.session_id)
            logger.info(f"已关闭会话 {self.session_id}")
    
    def get_memory_stats(self) -> Dict:
        """获取记忆统计信息"""
        return self.memory_service.get_memory_stats(self.session_id)
    
    def switch_model(
        self,
        model_name: str,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None
    ):
        """
        切换模型
        
        Args:
            model_name: 新模型名称
            temperature: 新温度参数
            top_p: 新Top-P参数
        """
        self.model_name = model_name
        if temperature is not None:
            self.temperature = temperature
        if top_p is not None:
            self.top_p = top_p
        
        # 重新初始化LLM
        self.llm = self._initialize_llm()
        logger.info(f"模型已切换到: {self.model_name}")
    
    def close(self):
        """关闭Agent，释放资源"""
        if self.db_session:
            self.db_session.close()
        logger.info(f"Agent已关闭: session_id={self.session_id}")
    
    def __del__(self):
        """析构函数"""
        self.close()
