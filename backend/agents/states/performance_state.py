"""
绩效评价工作流完整状态定义
涵盖客户端14步功能所需的所有状态字段
"""
from typing import TypedDict, List, Dict, Any, Optional, Annotated, Literal
from datetime import datetime
from enum import Enum


# ==================== 枚举定义 ====================

class ProjectTypeEnum(str, Enum):
    """项目类型"""
    DEPARTMENT = "部门评价"
    FINANCIAL = "财政评价"


class IndicatorTypeEnum(str, Enum):
    """指标体系类型"""
    PROJECT_EXPENDITURE = "项目支出指标体系"
    DEPARTMENT_OVERALL = "部门整体指标体系"
    LOWER_GOVERNMENT = "下级政府整体运行指标体系"
    SPECIAL_BOND = "专项债指标体系"
    SOCIAL_SECURITY = "社保基金指标体系"
    CUSTOM = "自定义"


class TemperatureProfileEnum(str, Enum):
    """模型温度配置档位"""
    CREATIVE = "发散"     # temperature=0.9, top_p=0.95
    NORMAL = "普通"       # temperature=0.7, top_p=0.9
    PRECISE = "严谨"      # temperature=0.3, top_p=0.5


class LanguageStyleEnum(str, Enum):
    """语言风格"""
    NEUTRAL = "中立"
    SHARP = "尖锐"
    EUPHEMISTIC = "委婉"


class StepStatusEnum(str, Enum):
    """步骤状态"""
    PENDING = "pending"        # 等待执行
    RUNNING = "running"        # 执行中
    AWAITING_APPROVAL = "awaiting_approval"  # 等待用户确认
    COMPLETED = "completed"    # 已完成
    SKIPPED = "skipped"        # 已跳过
    ERROR = "error"            # 执行出错


class ComparisonModeEnum(str, Enum):
    """模型模式"""
    SINGLE = "single"          # 单模型
    MULTI_COMPARE = "multi_compare"  # 多模型对比
    MULTI_DIALOGUE = "multi_dialogue"  # 对比后单独对话


# ==================== 基础数据类型 ====================

class FileInfo(TypedDict):
    """文件信息"""
    file_id: int
    original_name: str
    file_type: str            # pdf, docx, xlsx, jpg, png
    file_path: str
    file_size: int
    upload_type: str          # "图片/PDF" 或 "Word文档"
    extracted_text: str
    extraction_status: str    # pending, success, error
    expires_at: datetime


class IndicatorInfo(TypedDict, total=False):
    """指标信息"""
    id: str                           # 唯一标识
    level: int                        # 1/2/3/4级
    name: str                         # 指标名称
    parent_id: Optional[str]          # 父级指标ID
    order: int                        # 排序序号
    score: Optional[float]            # 分值
    explanation: Optional[str]        # 指标解释
    scoring_standard: Optional[str]   # 评分标准
    score_obtained: Optional[float]   # 实际得分
    score_reason: Optional[str]       # 得分/扣分原因
    source: Optional[str]             # 来源：system_generated/user_imported/template
    children: Optional[List[Any]]     # 子指标列表（递归结构）


class IndicatorSystem(TypedDict):
    """完整指标体系"""
    indicator_type: str               # 指标体系类型
    template_id: Optional[int]        # 使用的模板ID（如有）
    level1_indicators: List[IndicatorInfo]
    metadata: Dict[str, Any]          # 额外元数据


class ScoreValidationDetail(TypedDict):
    """分值校验详情"""
    dimension: int                    # 校验维度 1-4
    passed: bool
    message: str
    details: Optional[str]


class ScoreValidationResult(TypedDict):
    """分值校验结果"""
    overall_passed: bool
    validations: List[ScoreValidationDetail]
    warnings: List[str]


class StepResult(TypedDict):
    """单步骤执行结果"""
    step_number: int
    step_name: str
    status: str                       # StepStatusEnum
    content: str                      # 最终产出内容（文本/Markdown/JSON）
    raw_outputs: Dict[str, str]       # 各模型的原始输出 {model_name: output}
    comparison_results: Optional[List[Any]]  # 多模型对比结果
    selected_model: str               # 最终选择的模型
    version: int                      # 版本号
    created_at: datetime
    updated_at: datetime


class QuestionnaireItem(TypedDict):
    """调查问卷题目"""
    id: str
    index: int
    question: str                     # 问题文本
    related_indicator_id: str         # 关联的指标ID
    related_indicator_name: str       # 关联的指标名称
    scale_levels: List[str]           # 里克特量表等级
    question_type: str                # likert / open_ended
    created_by: str                   # system / user


class ModelComparisonResult(TypedDict):
    """多模型对比结果"""
    step_number: int
    step_name: str
    models_used: List[str]
    individual_outputs: Dict[str, str]
    comparison_analysis: str          # 对比分析总结
    selected_best: Optional[str]      # 用户选择的最佳模型


class StepControl(TypedDict):
    """步骤控制状态"""
    current_step: int
    total_steps: int
    step_statuses: Dict[int, str]     # step_number -> StepStatusEnum
    can_proceed: bool                 # 是否可以进入下一步
    requires_approval: bool           # 是否需要人工确认
    approval_step: Optional[int]      # 等待确认的步骤号
    comparison_mode: str              # ComparisonModeEnum
    temperature_profile: str          # TemperatureProfileEnum
    language_style: str               # LanguageStyleEnum


class SystemConfig(TypedDict):
    """系统运行时配置"""
    enabled_models: List[str]
    default_model: str
    rag_enabled: bool
    rag_collection_name: str


# ==================== 主状态 ====================

class PerformanceState(TypedDict, total=False):
    """
    绩效评价工作流主状态
    使用 total=False 允许所有字段可选，逐步填充
    """

    # ── 基础信息 ──
    session_id: str
    user_id: str
    project_id: Optional[int]
    project_name: str
    project_type: str                   # ProjectTypeEnum
    created_at: datetime

    # ── 文件管理 ──
    uploaded_files: List[FileInfo]      # 所有上传文件
    image_pdf_files: List[FileInfo]     # 图片/PDF类文件（上传窗口1）
    word_files: List[FileInfo]          # Word文档类文件（上传窗口2）
    file_paths: List[str]
    indicator_type: str
    use_rag: bool
    questionnaire_ready: bool
    deduction_points: List[Dict[str, Any]]
    _step9_deduction_context: str
    raw_text: str                       # 所有文件提取后的原始文本
    all_raw_texts: Dict[str, str]       # 按文件名的原始文本映射 {filename: text}
    raw_line_counts: Dict[str, int]     # 步骤7弱定位用：{文件名(去上传前缀): 非空行数}，轻量投影便于进备份
    document_coverage: Dict[str, Any]   # 提示词资料覆盖度（总字数/使用字数/未引用块/是否已压缩）

    # ── 步骤控制 ──
    step_control: StepControl

    # ── 14步产出物 ──
    step_results: Dict[int, StepResult]    # step_number -> StepResult
    require_approval: bool
    approvals: Dict[str, Any]
    review_steps: List[int]
    step_evidence: Dict[str, Any]
    completed_steps: List[int]

    # ── 各步骤中间数据（结构化存储） ──
    cost_list: str                         # Step1: 资料清单
    cost_list_items: List[Dict[str, Any]]  # Step1: 资料清单结构化列表

    effective_data: str                    # Step2: 有效项目资料（核心内容）
    effective_data_categorized: Dict[str, str]  # Step2: 分类后的核心内容
    content_index: Dict[str, str]          # Step2: 内容索引 {核心内容片段: 原文出处}

    indicator_system: IndicatorSystem      # Step3: 完整指标体系
    # Step3: scoring_standard 里夹带资料核对结论的审计计数（只记账，不改原文）
    standard_purity: Dict[str, Any]

    indicator_scores: Dict[str, float]     # Step4: 分值分配 {indicator_id: score}
    score_validation: ScoreValidationResult  # Step4: 分值校验结果

    scoring_standards: Dict[str, str]      # Step5: 评分标准 {indicator_id: standard}

    evaluation_table: str                  # Step6: 评价指标体系表（Markdown/HTML表格）

    indicator_analysis: str                # Step7: 指标分析内容
    score_table: str                       # Step7: 最终评分表

    experience_summary: str                # Step8: 经验做法

    problems_list: List[Dict[str, Any]]    # Step9: 问题列表 [{"problem": "", "reason": ""}]
    problems_analysis: str                 # Step9: 问题及原因分析全文

    suggestions: str                       # Step10: 建议内容
    suggestions_list: List[Dict[str, Any]] # Step10: 建议结构化列表

    comprehensive_evaluation: str          # Step11: 综合评价分析及结论
    evaluation_grade: str                  # Step11: 评价等级
    evaluation_total_score: float          # Step11: 总分

    basic_info: str                        # Step12: 基本情况
    basic_info_modules: Dict[str, str]     # Step12: 各模块内容
    module_names: Dict[str, str]           # Step12: 模块名映射 {key: name}

    work_introduction: str                 # Step13: 绩效评价工作开展情况
    work_intro_modules: Dict[str, str]     # Step13: 各模块内容

    final_report: str                      # Step14: 最终报告
    final_report_modules: Dict[str, str]   # Step14: 报告各模块内容
    report_module_order: List[str]         # Step14: 模块排列顺序
    report_section_order: List[Dict[str, Any]]  # Step14: 自定义章节顺序
    consistency: Dict[str, Any]            # Step14: 步骤间一致性检查结果（纯程序，不调模型）
    score_disposition: Dict[str, Any]      # Step7: 逐指标评分处置台账（已落地/模型待核实/引用无法定位…）

    # ── 前端交互写入的扩展状态（通过update_state持久化） ──
    indicator_score_history: List[Dict[str, Any]]
    indicator_score_map: Dict[str, Dict[str, Any]]  # Step7: 逐指标评分
    score_summary: Dict[str, Any]                   # Step11: 评分汇总
    confirmed_deduction_points: List[Dict[str, Any]]  # Step9: 已确认扣分点
    manual_problems: List[Dict[str, Any]]            # Step9: 手动添加问题
    logic_verification: str                          # Step10: 逻辑校对结果
    format_check_result: str                         # Step14: 格式校对结果
    export_style: Dict[str, Any]                     # 导出样式自定义

    # ── 调查问卷（Step6附加） ──
    questionnaire_needed: bool
    questionnaire_items: List[QuestionnaireItem]

    # ── 多模型对比 ──
    comparison_history: Dict[int, ModelComparisonResult]  # step_number -> comparison

    # ── 系统配置 ──
    system_config: SystemConfig

    # ── 提示词与知识库 ──
    active_prompts: Dict[int, str]        # step_number -> prompt_content
    active_knowledge_ids: List[int]       # 当前使用的知识库条目ID

    # ── 错误与日志 ──
    errors: List[str]
    warnings: List[str]
    execution_log: List[Dict[str, Any]]   # 执行日志

    # ── Token统计 ──
    token_usage: Dict[str, int]           # model_name -> total_tokens_used
    token_records: List[Dict[str, Any]]   # 详细的token使用记录