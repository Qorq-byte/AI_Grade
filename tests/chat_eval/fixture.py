"""评测用的固定报告（批次 42）。

为什么要一份写死的报告而不是读 `state_backups/`：
- 备份是运行产物、不进 git，换台机器就没有；
- 备份里的内容每次跑工作流都不一样，"命中率"会跟着变，没法比前后。

所以这里造一份**结构真实**的报告：三级指标、分章正文、证据编号、一处占位符、
一处"正文分数与程序汇总不一致"。20 条问题就是围绕它设计的。
"""

PROJECT = "城东片区雨污分流改造项目"

INDICATOR_SYSTEM = {
    "level1_indicators": [{
        "id": "l1", "name": "产出与效益", "level": 1, "score": 100,
        "children": [{
            "id": "l2", "name": "项目产出", "level": 2, "score": 100,
            "children": [
                {"id": "i1", "name": "管网改造完成率", "level": 3, "score": 40,
                 "explanation": "实际改造长度 / 计划改造长度", "scoring_standard": "每低 5 个百分点扣 2 分"},
                {"id": "i2", "name": "设备采购完成率", "level": 3, "score": 30,
                 "explanation": "设备到场并验收的比例", "scoring_standard": "每低 10 个百分点扣 3 分"},
                {"id": "i3", "name": "群众满意度", "level": 3, "score": 30,
                 "explanation": "问卷调查满意及以上占比", "scoring_standard": "低于 90% 每 5 个百分点扣 2 分"},
            ],
        }],
    }],
}

SCORE_MAP = {"i1": {"actual_score": 34}, "i2": {"actual_score": 26},
             "i3": {"actual_score": 26}}

EVIDENCE = {
    "7": [
        {"id": "P1", "source": "施工总承包合同.pdf", "locator": "第 12 行",
         "kind": "project", "text": "合同价款 4612.35 万元，工期 420 日历天"},
        {"id": "P2", "source": "监理月报（2025-06）.pdf", "locator": "第 8 行",
         "kind": "project", "text": "设备到场验收 26 台，计划 30 台"},
    ],
    "9": [
        {"id": "P3", "source": "资金拨付台账.xlsx", "locator": "第 3 行",
         "kind": "project", "text": "2025 年二季度资金到位率 78%"},
    ],
}

STEP_CONTENTS = {
    3: "指标体系：一级「产出与效益」，二级「项目产出」，三级为管网改造完成率、"
       "设备采购完成率、群众满意度，分值 40/30/30。",
    7: (
        "一、总体得分情况\n\n"
        "本项目综合得分 86 分，其中管网改造完成率 34 分、设备采购完成率 26 分、"
        "群众满意度 26 分。\n\n"
        "二、扣分项\n\n"
        "设备采购完成率不足，计划 30 台仅验收 26 台，扣 3 分；"
        "管网改造完成率 85%，扣 2 分。"
    ),
    8: (
        "一、主要做法\n\n"
        "一是把雨污分流与老旧小区改造打包实施，减少重复开挖；"
        "二是引入第三方监理按月核对设备到场数量。\n\n"
        "二、成效\n\n"
        "主城区内涝点由 7 处降至 2 处。"
    ),
    9: (
        "一、存在的问题\n\n"
        "资金到位率偏低，2025 年二季度仅 78%。\n\n"
        "二、原因分析\n\n"
        "拨付流程较长，区级配套资金在下半年才下达，影响执行进度。"
    ),
    10: (
        "一、建议\n\n"
        "建议区财政将配套资金纳入年初预算，按季度拨付；"
        "对设备采购设置到场节点考核。"
    ),
    11: "综合得分 92 分，评价等级为良好。",
    12: "项目基本情况：2023 年立项，2024 年开工，总投资 4860 万元，其中市级财政 3400 万元。",
    13: "评价范围：2024 年 1 月至 2025 年 6 月的项目支出与产出。",
}

FINAL_REPORT = """# 城东片区雨污分流改造项目绩效评价报告

## 一、项目基本情况

2023 年立项，2024 年开工，总投资 4860 万元。

## 二、评价工作开展情况

采用资料审查、现场核查与问卷相结合的方式。

## 三、指标体系

一级「产出与效益」，三级指标 3 项。

## 四、指标得分及分析

综合得分 92 分。

## 五、项目经验

（本部分内容尚未生成，请先完成对应步骤后重新组装）

## 六、存在的问题

资金到位率偏低。

## 七、建议

配套资金纳入年初预算。
"""


def state() -> dict:
    return {
        "session_id": "chat-eval-session",
        "project_name": PROJECT,
        "indicator_type": "项目支出指标体系",
        "indicator_system": INDICATOR_SYSTEM,
        "indicator_score_map": dict(SCORE_MAP),
        "score_summary": {"total_score": 92, "total_full": 100, "score_100": 92,
                          "grade": "良好", "complete": True,
                          "scored_count": 3, "total_count": 3},
        "completed_steps": list(range(1, 15)),
        "step_evidence": {key: [dict(item) for item in value] for key, value in EVIDENCE.items()},
        "step_results": {step: {"content": text, "version": 1} for step, text in STEP_CONTENTS.items()},
        "final_report": FINAL_REPORT,
        "warnings": [],
    }
