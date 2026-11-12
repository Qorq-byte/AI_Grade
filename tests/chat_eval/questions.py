"""聊天评测问题集（批次 42）。

每条只做一件事：**这段上下文里，回答这个问题需要的那句话在不在**。
判定是字符串包含，不做语义判断 —— 这是刻意的：
- 语义判断要么靠人（不可重复），要么靠模型（等于换个模型的主观意见）；
- 「上下文里没有」是硬事实，能稳定地卡住回归（切块坏了、预算砍多了、检索退化了）。

`kind`：
- `report`：必须命中 `expect` 里的**全部**锚点；
- `chitchat`：不该出现【相关章节】（问天气也翻报告，才是真问题）。
"""

CASES = [
    # ── 报告基本信息 ──
    {"id": "Q01", "kind": "report", "question": "这个项目叫什么名字？",
     "expect": ["城东片区雨污分流改造项目"], "why": "项目名是最基本的指代。"},
    {"id": "Q02", "kind": "report", "question": "综合得分是多少？",
     "expect": ["92"], "why": "正文里的得分要能取到。"},
    {"id": "Q03", "kind": "report", "question": "按指标重新算是多少分？",
     "expect": ["86.0"], "why": "程序汇总与正文不一致时，上下文必须两个数都给出。"},

    # ── 扣分与指标 ──
    {"id": "Q04", "kind": "report", "question": "扣分项在哪？",
     "expect": ["设备采购完成率不足"], "why": "「扣分点在哪」是最高频的问题。"},
    {"id": "Q05", "kind": "report", "question": "设备采购扣了多少分？",
     "expect": ["扣 3 分"], "why": "扣分要能落到具体分数。"},
    {"id": "Q06", "kind": "report", "question": "设备采购完成率怎么样？",
     "expect": ["26 台"], "why": "扣分依据要能看到实际数量。"},
    {"id": "Q07", "kind": "report", "question": "指标体系里有哪些三级指标？",
     "expect": ["管网改造完成率", "群众满意度"], "why": "指标名是评分讨论的锚点。"},
    {"id": "Q08", "kind": "report", "question": "指标分值怎么分配的？",
     "expect": ["40/30/30"], "why": "分值分配是判断「指标重要不重要」的依据。"},

    # ── 问题与原因 ──
    {"id": "Q09", "kind": "report", "question": "资金为什么没到位？",
     "expect": ["拨付流程较长"], "why": "原因分析要能被检索到，而不是只给「资金到位率低」。"},
    {"id": "Q10", "kind": "report", "question": "资金到位率是多少？",
     "expect": ["78%"], "why": "数字要能直接取。"},

    # ── 经验与建议 ──
    {"id": "Q11", "kind": "report", "question": "主要做法有哪些？",
     "expect": ["打包实施"], "why": "经验做法是报告第五章的核心。"},
    {"id": "Q12", "kind": "report", "question": "取得了什么成效？",
     "expect": ["内涝点由 7 处降至 2 处"], "why": "成效要能取证，不能只有形容词。"},
    {"id": "Q13", "kind": "report", "question": "有什么建议？",
     "expect": ["纳入年初预算"], "why": "建议要能落到具体动作。"},

    # ── 结构与可导出性 ──
    {"id": "Q14", "kind": "report", "question": "哪一章还是空的？",
     "expect": ["REPORT_CHAPTER_MISSING"], "why": "缺章必须能说清是哪一章。"},
    {"id": "Q15", "kind": "report", "question": "这份报告能直接导出吗？",
     "expect": ["阻断"], "why": "能不能导出取决于 blocking 问题，不能靠猜。"},
    {"id": "Q16", "kind": "report", "question": "报告里有没有合同类证据？",
     "expect": ["施工总承包合同.pdf"], "why": "证据索引要能回答「有没有这类材料」。"},

    # ── 项目背景 ──
    {"id": "Q17", "kind": "report", "question": "项目什么时候立项的？",
     "expect": ["2023 年立项"], "why": "背景信息来自步骤 12 的正文。"},
    {"id": "Q18", "kind": "report", "question": "评价范围是什么？",
     "expect": ["2024 年 1 月至 2025 年 6 月"], "why": "评价范围来自步骤 13 的正文。"},

    {"id": "Q21", "kind": "report", "question": "评价范围是什么？",
     "expect": ["2024 年 1 月至 2025 年 6 月"], "why": "单句事实，曾在真模型抽读里被漏掉（L09）。"},

    # ── 数字与事实（扩样本，批次 47） ──
    {"id": "Q22", "kind": "report", "question": "评价等级是什么？",
     "expect": ["良好"], "why": "等级与得分要能对应。"},
    {"id": "Q23", "kind": "report", "question": "总投资是多少？",
     "expect": ["4860"], "why": "投资规模是所有资金指标的分母。"},
    {"id": "Q24", "kind": "report", "question": "市级财政拨款多少？",
     "expect": ["3400"], "why": "资金来源要能取。"},
    {"id": "Q25", "kind": "report", "question": "计划采购多少台设备？",
     "expect": ["30 台"], "why": "计划值是扣分判断的基准。"},
    {"id": "Q26", "kind": "report", "question": "实际验收多少台设备？",
     "expect": ["26 台"], "why": "实际值要能和计划值对上。"},
    {"id": "Q27", "kind": "report", "question": "管网改造完成率是多少？",
     "expect": ["85%"], "why": "核心产出指标。"},
    {"id": "Q28", "kind": "report", "question": "群众满意度得了几分？",
     "expect": ["26 分"], "why": "三级指标得分。"},
    {"id": "Q29", "kind": "report", "question": "内涝点治理的成效如何？",
     "expect": ["7 处降至 2 处"], "why": "成效要能取证。"},
    {"id": "Q30", "kind": "report", "question": "项目什么时候开工？",
     "expect": ["2024 年开工"], "why": "时间线来自报告第一章。"},
    {"id": "Q31", "kind": "report", "question": "第三方监理做了什么？",
     "expect": ["按月核对"], "why": "经验做法里的具体动作。"},
    {"id": "Q32", "kind": "report", "question": "资金拨付的建议是什么？",
     "expect": ["按季度拨付"], "why": "建议要落到具体节奏。"},
    {"id": "Q33", "kind": "report", "question": "评价用了哪些方法？",
     "expect": ["资料审查"], "why": "评价工作开展情况。"},
    {"id": "Q34", "kind": "report", "question": "有没有资金类证据？",
     "expect": ["资金拨付台账"], "why": "证据索引要能回答材料类型。"},
    {"id": "Q35", "kind": "report", "question": "报告缺哪一章？",
     "expect": ["五、项目经验"], "why": "缺章要能被点名。"},
    {"id": "Q36", "kind": "report", "question": "配套资金应该怎么安排？",
     "expect": ["配套资金"], "why": "建议里的关键对象。"},
    {"id": "Q37", "kind": "report", "question": "评价范围覆盖到什么时候？",
     "expect": ["2025 年 6 月"], "why": "单句事实的另一种问法。"},

    # ── 与报告无关：不该翻章节 ──
    {"id": "Q38", "kind": "chitchat", "question": "今天星期几？",
     "why": "与报告无关的时间问题。"},
    {"id": "Q39", "kind": "chitchat", "question": "帮我写一首诗",
     "why": "创作类请求不该翻报告。"},
    {"id": "Q40", "kind": "chitchat", "question": "你能做什么？",
     "why": "能力询问不该触发章节检索。"},
    {"id": "Q19", "kind": "chitchat", "question": "你好",
     "why": "打招呼不该触发章节检索。"},
    {"id": "Q20", "kind": "chitchat", "question": "帮我订一张明天去北京的机票",
     "why": "与报告无关的请求不该翻报告。"},
]
