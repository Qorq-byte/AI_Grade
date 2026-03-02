"""
14步绩效评价报告生成 — 默认Prompt模板系统
========================================
基于 langchain_core.prompts.ChatPromptTemplate 构建
支持三种温度档位变体（发散/普通/严谨）及动态上下文注入
每个步骤定义 SystemMessage + HumanMessage 模板
生产环境优先从数据库 prompt_templates 表加载; 此处为默认兜底模板
"""
from typing import Dict, List, Optional, Any
from enum import Enum
from langchain_core.prompts import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
)
from langchain_core.messages import SystemMessage, HumanMessage


# ==================== 温度档位枚举 ====================

class TemperatureProfile(str, Enum):
    """模型温度配置档位"""
    CREATIVE = "发散"     # temperature=0.9, top_p=0.95
    NORMAL = "普通"       # temperature=0.7, top_p=0.9
    PRECISE = "严谨"      # temperature=0.3, top_p=0.5

    @property
    def temperature(self) -> float:
        return {self.CREATIVE: 0.9, self.NORMAL: 0.7, self.PRECISE: 0.3}[self]

    @property
    def top_p(self) -> float:
        return {self.CREATIVE: 0.95, self.NORMAL: 0.9, self.PRECISE: 0.5}[self]


# ==================== 14步元信息 ====================

STEP_META: Dict[int, Dict[str, str]] = {
    0:  {"name": "初始化与文件解析", "short_name": "init"},
    1:  {"name": "整理所需资料清单", "short_name": "cost_list"},
    2:  {"name": "提取核心有效信息+内容索引", "short_name": "effective_data"},
    3:  {"name": "构建绩效评价指标体系", "short_name": "indicator_system"},
    4:  {"name": "生成各指标分值", "short_name": "indicator_scores"},
    5:  {"name": "生成各指标评分标准", "short_name": "scoring_standards"},
    6:  {"name": "生成评价指标体系表", "short_name": "evaluation_table"},
    7:  {"name": "生成指标得分及分析", "short_name": "indicator_analysis"},
    8:  {"name": "总结主要经验做法", "short_name": "experience"},
    9:  {"name": "分析存在问题及原因", "short_name": "problems"},
    10: {"name": "提出改进建议", "short_name": "suggestions"},
    11: {"name": "综合评价分析及结论", "short_name": "comprehensive_eval"},
    12: {"name": "生成基本情况", "short_name": "basic_info"},
    13: {"name": "生成评价工作简介", "short_name": "work_intro"},
    14: {"name": "组装最终报告", "short_name": "final_report"},
}


# ==================== 系统级通用指令基架 ====================

_UNIVERSAL_ROLE = """你是一位资深财政绩效评价专家，在中国政府绩效管理领域拥有20年以上实务经验。
你精通《项目支出绩效评价管理办法》（财预〔2020〕10号）及其他相关财政法规。
你的工作风格：{style_instruction}"""

_STYLE_INSTRUCTIONS: Dict[TemperatureProfile, str] = {
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
        "所有结论必须有明确的因果关系链条。"
    ),
}

_LANGUAGE_STYLE_INSTRUCTIONS = {
    "中立": (
        "语言风格：保持中立、客观的表述。使用「存在不足」「有待加强」「尚有提升空间」等中性措辞。"
        "不夸大问题的严重性，也不回避问题的存在。平衡正面与负面评价。"
    ),
    "尖锐": (
        "语言风格：使用尖锐、直截了当的表述。使用「严重不足」「亟需整改」「问题突出」「存在重大缺陷」等强力措辞。"
        "明确指出问题的严重性和紧迫性，不回避矛盾，直击要害。"
    ),
    "委婉": (
        "语言风格：使用委婉、建设性的表述。使用「建议关注」「可进一步优化」「在实际执行过程中发现」「从更好推进工作角度考虑」等委婉措辞。"
        "以建议的口吻表达问题，强调改进方向而非错误本身。"
    ),
}

_QUALITY_RULES = """【⚠️ 严禁编造 — 最高优先级规则】
1. ❌ 绝对禁止编造项目名称、金额、时间、人员、部门名称等任何具体信息
2. ❌ 绝对禁止在无资料支撑的情况下填充完整报告内容
3. ✅ 所有数据和结论必须**逐条**来源于提供的项目资料原文
4. ✅ 每一段输出必须引用具体的资料来源（文件名/段落位置）
5. ✅ 文件中缺失的信息，必须明确标注「⚠️ 现有资料中未提供该信息，无法判断」
6. ✅ 如果整体资料严重不足，请在第一行输出：「⚠️ 警告：现有资料严重不足，以下分析基于有限信息，可靠性较低」

【格式规范】
7. 层次分明：一级/二级/三级指标的逻辑关系清晰
8. 表述客观：使用第三方评价口吻，不使用「我」「我们」等第一人称
9. 数据精确：所有数字保留两位小数（仅当有原始数据时）
10. 源可追溯：关键结论应标注来源于哪份文件/哪个段落"""

# ==================== 14步Prompt定义 ====================

# ─── Step 1: 整理所需资料清单 ───────────────────────────

STEP1_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价项目组的资料管理专员。请基于项目的基本信息和已上传资料，
整理出该项目绩效评价所需的**全部资料清单**，供项目单位准备。

【具体要求】
1. 将资料按类别分组：前期工作资料、实施过程资料、组织管理资料、资金投向资料、绩效目标资料等
2. 每项资料说明其用途（为什么需要这份资料）
3. 标注哪些资料已提供、哪些缺失待补充
4. 对已提供资料进行基础完备性判断（是否缺页、是否模糊不清、是否为正式盖章版）
5. 清单格式应清晰，便于项目单位按清单逐项准备

【输入数据说明】
将收到的项目基础信息和已上传文件的提取文本。对于未提供的文件类型请明确标记为「待补充」。
"""

STEP1_HUMAN_TEMPLATE = """## 项目基本信息
- 项目名称：{project_name}
- 项目类型：{project_type}

## 已上传文件列表
{uploaded_files_summary}

## 已提取的原始文本（前8000字）
{raw_text_preview}

请整理该项目的完整资料清单。"""

# ─── Step 2: 提取核心有效信息+内容索引 ─────────────────

STEP2_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价项目组的资料分析专员。请从原始项目资料中提取**核心有效信息**，
并按类别整理，同时建立「核心内容 ↔ 原文出处」的双向索引。

【具体要求】
1. 排除无效信息：去除文件中的页眉页脚、空白行、重复段落、模板性空话
2. 按类别整理核心内容：项目概况、组织管理、资金投入与使用、实施内容、绩效目标等
3. 对每一段核心内容标注原文出处（文件名+段落位置）
4. 识别内容中存在的矛盾或逻辑不一致处，标注为待核实项
5. 对时间节点进行统一排序梳理
6. 输出「核心内容索引表」,格式：
   | 序号 | 核心内容摘要 | 所属类别 | 原文出处（文件名+段落） | 备注 |

{step1_context_placeholder}
"""

STEP2_HUMAN_TEMPLATE = """## 项目名称
{project_name}

## 上一步产出的资料清单概要
{cost_list_summary}

## 全部原始文本（请逐段分析）
{raw_text_full}

请提取核心有效信息并建立内容索引。"""

# ─── Step 3: 构建绩效评价指标体系 ─────────────────────

STEP3_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价指标体系设计师。请基于项目核心资料，构建一套完整的**三级绩效评价指标体系**。

【指标设计原则】
1. 全面覆盖：指标体系应覆盖项目的决策、过程、产出、效益四个维度
2. 层次分明：一级指标≤6个, 二级指标≤20个, 三级指标可按需细化
3. 逻辑自洽：三级指标是二级指标的具体化, 二级指标是一级指标的展开
4. 可衡量性：每个指标应当是可量化或可定性评价的
5. 相关性：指标必须与项目目标和资金用途直接相关
6. 合规性：符合财预〔2020〕10号文的要求

【输出格式】
请以严格JSON格式输出指标体系，结构如下：
```json
{{
  "indicator_type": "项目支出指标体系",
  "level1_indicators": [
    {{
      "id": "L1_01",
      "level": 1,
      "name": "一级指标名称",
      "order": 1,
      "score": 权重分值,
      "children": [
        {{
          "id": "L2_01_01",
          "level": 2,
          "name": "二级指标名称",
          "order": 1,
          "parent_id": "L1_01",
          "score": 权重分值,
          "children": [
            {{
              "id": "L3_01_01_01",
              "level": 3,
              "name": "三级指标名称",
              "order": 1,
              "parent_id": "L2_01_01",
              "score": 权重分值,
              "explanation": "指标解释",
              "scoring_standard": "评分标准说明",
              "children": []
            }}
          ]
        }}
      ]
    }}
  ]
}}
```

{step2_context_placeholder}
"""

STEP3_HUMAN_TEMPLATE = """## 项目名称
{project_name}

## 核心有效信息（已分类整理）
{effective_data_categorized}

## 上一步的内容索引表
{content_index_summary}

请构建该项目的完整三级绩效评价指标体系。"""

# ─── Step 4: 生成各指标分值 ───────────────────────────

STEP4_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价分值分配专家。请为上一步构建的指标体系**分配各指标分值**。
注意：分值需在Step5生成评分标准**之前**单独完成。

【分值分配铁律——四维度校验规则】
**维度一：总分约束**
- 所有一级指标分值总和 = 100分
- 所有二级指标分值总和 = 100分
- 所有三级指标分值总和 = 100分

**维度二：一级指标纵向一致**
- 每个一级指标分值 = 其下所有二级指标分值之和
- 每个一级指标分值 = 其下所有三级指标分值之和

**维度三：二级指标纵向一致**
- 每个二级指标分值 = 其下所有三级指标分值之和

**维度四：产出+效益权重建议**
- 产出类指标分值 + 效益类指标分值 建议 < 60分
- 如 ≥ 60分则触发预警，但如项目确实偏重产出效益，允许说明理由

【输出格式】
请以JSON格式输出分值分配：
```json
{{
  "scores": {{
    "L1_01": 20,
    "L2_01_01": 10,
    "L3_01_01_01": 5,
    ...
  }},
  "allocation_rationale": "分值分配的整体思路说明",
  "output_benefit_summary": "产出+效益指标分值合计及说明"
}}
```

【参考上下文】
已有指标体系、Step2核心内容。
"""

STEP4_HUMAN_TEMPLATE = """## 项目名称
{project_name}

## 已构建的指标体系（完整JSON）
{indicator_system_json}

## 项目核心内容摘要
{effective_data_summary}

请为每个最底层指标分配具体的分值，确保满足四维度校验规则。"""

# ─── Step 5: 生成各指标评分标准 ───────────────────────

STEP5_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价评分标准制定专家。请为每个**三级指标**（或二级指标，如无三级）
制定详细的评分标准。

【评分标准制定要求】
1. 每个指标分值为N分, 评分标准应细化为若干档次
2. 标准要具体、可操作、可验证
3. 明确各档次的得分条件和扣分规则
4. 每项扣分不超过该指标的总分值
5. 评分标准应与指标名称和解释保持一致

【格式】
对每个指标输出：
- 指标名称+分值
- 评分标准详细说明（含档次划分）
- 满分条件
- 扣分项及扣分幅度

{step4_context_placeholder}
"""

STEP5_HUMAN_TEMPLATE = """## 项目名称
{project_name}

## 指标体系及分值（完整JSON）
{indicator_system_with_scores_json}

## 项目核心信息
{effective_data_summary}

请为每个指标制定详细的评分标准。"""

# ─── Step 6: 生成评价指标体系表 ───────────────────────

STEP6_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价报告撰写专员。请将指标体系、分值、评分标准整合为一份**完整的评价指标体系表**。

【表格要求】
1. 表格列：一级指标、二级指标、三级指标、指标解释、分值、评分标准
2. 一级指标行应有明确的视觉区分（如加粗或背景色）
3. 层次结构清晰：相同的一级指标下所有行连续排列
4. 表格后附注：总分100分，各层级分数校验通过

【输出格式】
优先使用Markdown表格，同时附带结构化的JSON数据以便后续导出Word/PDF。

{step5_context_placeholder}
"""

STEP6_HUMAN_TEMPLATE = """## 项目名称
{project_name}

## 指标体系（含分值+评分标准，完整JSON）
{indicator_system_full_json}

请生成完整的评价指标体系表。"""

# ─── Step 7: 生成指标得分及分析 ───────────────────────

STEP7_SYSTEM_TEMPLATE = _UNIVERSAL_ROLE + """
{quality_rules}

【当前任务】
你是绩效评价评分专家。请基于评价指标体系、项目资料和评分标准，
为每个指标进行**模拟评分**并给出分析。

【评分规则】
1. 逐项对照评分标准进行打分
2. 得分必须有明确的依据（引用项目资料中的具体内容）
3. 扣分必须说明扣分原因和扣分幅度
4. 对项目资料中无法判断的指标，标注为「无法评价」并说明原因
5. 每个指标的得分不超过其满分值

