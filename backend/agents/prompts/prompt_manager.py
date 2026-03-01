"""
数据库驱动的Prompt管理器
========================
核心逻辑：
1. 优先从数据库 prompt_templates 表加载（支持管理端热更新）
2. 数据库无记录时回退到 default_prompts 内置模板
3. 支持版本管理、变更日志、模板对比
4. 提供缓存层以减少数据库查询
"""
from typing import Dict, Optional, Any
from datetime import datetime
from loguru import logger
from threading import RLock

from backend.agents.prompts.default_prompts import (
    PromptTemplateFactory,
    TemperatureProfile,
    STEP_META,
)
from backend.models.user_models import PromptTemplate


class PromptManager:
    """
    Prompt管理器
    单例模式 + 读写锁保证线程安全
    """

    _instance: Optional["PromptManager"] = None
    _lock = RLock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True

        # 内存缓存: step_number -> prompt_content
        self._cache: Dict[int, str] = {}
        # 缓存版本号跟踪
        self._cache_versions: Dict[int, int] = {}
        # 最后刷新时间
        self._last_refresh: Optional[datetime] = None
        # 刷新间隔（秒），0表示每次都查库
        self._refresh_interval: float = 60.0

        logger.info("PromptManager 初始化完成")

    # ─── 数据库加载 ────────────────────────────────────

    def load_from_database(
        self,
        db_session,
        step_number: Optional[int] = None,
        force_refresh: bool = False,
    ) -> Optional[str]:
        """
        从数据库加载Prompt

        Args:
            db_session: 数据库会话
            step_number: 步骤号（None则加载全部）
            force_refresh: 是否强制刷新缓存

        Returns:
            Prompt内容字符串（单步）或 None
        """
        if not force_refresh and step_number is not None:
            if self._is_cache_valid(step_number):
                return self._cache.get(step_number)

        try:
            query = db_session.query(PromptTemplate).filter_by(is_system=False)

            if step_number is not None:
                record = query.filter_by(step_number=step_number).order_by(
                    PromptTemplate.version.desc()
                ).first()

                if record and record.prompt_content:
                    self._cache[step_number] = record.prompt_content
                    self._cache_versions[step_number] = record.version or 1
                    logger.debug(f"从数据库加载Prompt: step={step_number}, version={record.version}")
                    return record.prompt_content
                return None
            else:
                # 加载全部
                records = query.all()
                for record in records:
                    if record.prompt_content:
                        self._cache[record.step_number] = record.prompt_content
                        self._cache_versions[record.step_number] = record.version or 1

                self._last_refresh = datetime.now()
                logger.info(f"从数据库加载了 {len(records)} 个Prompt模板")
                return None

        except Exception as e:
            logger.error(f"从数据库加载Prompt失败: {e}")
            return None

    # ─── Prompt获取（核心方法） ─────────────────────────

    def get_system_prompt(
        self,
        step_number: int,
        db_session = None,
        temperature: TemperatureProfile = TemperatureProfile.NORMAL,
        **kwargs,
    ) -> str:
        """
        获取指定步骤的System Prompt
        优先使用数据库版本，回退到内置模板

        Args:
            step_number: 步骤号 (0-14)
            db_session: 数据库会话（可选，传入则尝试数据库加载）
            temperature: 温度档位
            **kwargs: 传递给模板的额外变量

        Returns:
            最终可用的System Prompt字符串
        """
        # 尝试从数据库加载
        db_prompt = None
        if db_session is not None:
            db_prompt = self.load_from_database(db_session, step_number)

        if db_prompt:
            # 数据库版本：直接使用，但需要注入温度相关的指令
            style_map = {
                TemperatureProfile.CREATIVE: (
                    "在遵循政策框架的前提下，可以提出有建设性的创新观点。"
                    "语言可以有一定的文采和表现力，但必须基于事实和数据。"
                ),
                TemperatureProfile.NORMAL: (
                    "保持专业、客观、平衡的语调。既要指出问题，也要肯定成绩。"
                    "语言规范但不刻板，论证充分但不啰嗦。"
                ),
                TemperatureProfile.PRECISE: (
                    "严格遵循政策文件的表述规范，确保每一句话都有政策依据或数据支撑。"
                    "措辞严谨、数字精确、逻辑严密，不使用模糊或不确定的表述。"
                ),
            }
            style_instruction = style_map[temperature]
            system_part = db_prompt.split("\n\n---\n\n")[0] if "---" in db_prompt else db_prompt
            # 注入样式指令（如果数据库版本中没有）
            if "style_instruction" not in system_part:
                system_part = system_part.replace(
                    "{style_instruction}", style_instruction
                )
                system_part = system_part.replace(
                    "{quality_rules}",
                    """【输出质量标准】
1. 所有数据和结论必须来源于提供的项目资料，不得凭空编造
2. 如资料中信息不足，请明确标注「根据现有资料无法判断」
3. 层次分明：一级/二级/三级指标的逻辑关系清晰
4. 表述客观：使用第三方评价口吻，不使用「我」「我们」等第一人称
5. 数据精确：所有数字保留两位小数"""
                )
            try:
                system_part = system_part.format(**kwargs)
            except KeyError:
                pass
            return system_part

        # 回退到内置默认模板（使用 build_system_message 避免遗留未填充的占位符变量）
        logger.debug(f"Step {step_number}: 使用内置默认Prompt")
        msg = PromptTemplateFactory.build_system_message(step_number, temperature, **kwargs)
        return str(msg.content)

    def get_human_prompt(
        self,
        step_number: int,
        db_session = None,
        **kwargs,
    ) -> str:
        """
        获取指定步骤的Human Prompt模板
        优先使用数据库版本，回退到内置模板

        Args:
            step_number: 步骤号
            db_session: 数据库会话
            **kwargs: 模板变量

        Returns:
            填充变量后的Human Prompt字符串
        """
        # 尝试从数据库加载
        db_prompt = None
        if db_session is not None:
            db_prompt = self.load_from_database(db_session, step_number)

        if db_prompt and "---" in db_prompt:
            # 从数据库版本的 full_prompt 中提取 human 部分
            human_part = db_prompt.split("\n\n---\n\n", 1)[1]
            try:
                return human_part.format(**kwargs)
            except KeyError as e:
                logger.warning(f"Human Prompt模板变量缺失: {e}, 使用原始模板")
                return human_part

        # 回退到内置默认模板
        raw_template = PromptTemplateFactory.build_raw_human_prompt(step_number)
        try:
            return raw_template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Human Prompt模板变量缺失: {e}, 返回原始模板")
            return raw_template

    def get_full_prompt(
        self,
        step_number: int,
        db_session = None,
        temperature: TemperatureProfile = TemperatureProfile.NORMAL,
        **kwargs,
    ) -> Dict[str, str]:
        """
        获取完整Prompt（System + Human）

        Returns:
            {"system": "...", "human": "..."}
        """
        return {
            "system": self.get_system_prompt(step_number, db_session, temperature, **kwargs),
            "human": self.get_human_prompt(step_number, db_session, **kwargs),
        }

    # ─── 管理端操作 ────────────────────────────────────

    def save_to_database(
        self,
        db_session,
        step_number: int,
        system_prompt: str,
        human_prompt: str,
        modified_by: Optional[int] = None,
        change_log: Optional[str] = None,
    ) -> PromptTemplate:
        """
        保存/更新Prompt到数据库

        Args:
            db_session: 数据库会话
            step_number: 步骤号
            system_prompt: 新的System Prompt
            human_prompt: 新的Human Prompt
            modified_by: 修改人用户ID
            change_log: 变更说明

        Returns:
            保存后的PromptTemplate记录
        """
        full_prompt = f"{system_prompt}\n\n---\n\n{human_prompt}"
        step_name = STEP_META.get(step_number, {}).get("name", f"Step {step_number}")

        existing = (
            db_session.query(PromptTemplate)
            .filter_by(step_number=step_number, is_system=False)
            .first()
        )

        if existing:
            existing.prompt_content = full_prompt
            existing.step_name = step_name
            existing.version = (existing.version or 0) + 1
            existing.modified_by = modified_by
            existing.change_log = change_log or f"手动更新 v{existing.version}"
            existing.updated_at = datetime.now()
            record = existing
        else:
            record = PromptTemplate(
                step_number=step_number,
                step_name=step_name,
                prompt_content=full_prompt,
                is_system=False,
                version=1,
                modified_by=modified_by,
                change_log=change_log or "初始创建",
            )
            db_session.add(record)

        db_session.commit()

        # 更新缓存
        self._cache[step_number] = full_prompt
        self._cache_versions[step_number] = record.version

        logger.info(f"Prompt已保存到数据库: step={step_number}, version={record.version}")
        return record

    def reset_to_default(
        self,
        db_session,
        step_number: Optional[int] = None,
    ) -> int:
        """
        重置Prompt为系统默认模板

        Args:
            db_session: 数据库会话
            step_number: 步骤号（None则重置全部）

        Returns:
            重置的记录数
        """
        from backend.agents.prompts.default_prompts import get_all_default_prompts_as_dict

        defaults = get_all_default_prompts_as_dict()
        reset_count = 0

        steps_to_reset = [step_number] if step_number is not None else range(15)

        for sn in steps_to_reset:
            default_data = defaults.get(sn, {})
            if not default_data:
                continue

            full_prompt = (
                f"{default_data['system_prompt']}\n\n---\n\n"
                f"{default_data['human_prompt']}"
            )
            step_name = default_data.get("step_name", f"Step {sn}")

