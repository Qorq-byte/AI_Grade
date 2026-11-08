# -*- coding: utf-8 -*-
"""Batch 11 冒烟：五类锚点在真实引用形态下的判定结果。

真实引用来自备份 b20e9f85 里 20 条 anchor_unresolved 的原文（模型写的是
提示词里 render_evidence 印的「文件名（提取文本第N行）」，被它简写成了
「文件名-第N段 / 文件名-第N部分」）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import evidence_anchor  # noqa: E402

MATERIAL = """# 项目资料

## 评价指标体系（节选）

| 指标编号 | 指标名称 | 分值 |
| --- | --- | --- |
| P1-14-1 | 资金支付率 | 5 |
| P1-2-1 | 项目立项规范性 | 3 |

## 项目正文

一、项目概况
本项目为某县 2024 年农村公路提升工程。

二、建设内容
改建道路 12.4 公里，含桥涵 3 座。

三、绩效目标
目标1：完成改建里程 12.4 公里。
目标2：工程质量合格率 100%。

四、资金安排
项目总投资 1,860 万元，其中省级补助 1,200 万元。

五、存在问题
部分路段征拆进度滞后。
"""

CHUNKS = [
    {"id": "P1-10-1", "source": "fe_wf_sample.txt", "locator": "fe_wf_sample.txt-第10段",
     "kind": "项目资料", "text": "项目总投资 1,860 万元"},
    {"id": "P1-1-1", "source": "fe_wf_sample.txt", "locator": "fe_wf_sample.txt-第1段",
     "kind": "项目资料", "text": "本项目为某县 2024 年农村公路提升工程"},
    {"id": "P1-3-1", "source": "fe_wf_sample.txt", "locator": "fe_wf_sample.txt-第3段",
     "kind": "项目资料", "text": "改建道路 12.4 公里"},
    {"id": "P2-10-1", "source": "kpi_table.xlsx", "locator": "kpi_table.xlsx-第10段",
     "kind": "项目资料", "text": "支付率 92% 的另一个文件第10段"},
]

IDS = ["P1-10-1", "P1-1-1", "P1-3-1"]
HEADINGS = evidence_anchor.material_headings(MATERIAL)
ORDINALS = evidence_anchor.section_ordinals(MATERIAL)

# 原文：真实 16 行样本（第 14 行是「截至2025年6月已支出3200万元，支出进度64%」）
RAW_TEXTS = {
    "u1_be8b81782a0e487da7cc7aa092057f63_fe_wf_sample.txt":
        "\n".join(["云海市政道路建设工程项目"] + [f"第{i}行内容" for i in range(2, 17)]),
}

CASES = [
    # (引用原文, 期望 ok, 期望 kind, 说明)
    ("P1-10-1", True, "evidence_id", "编号精确命中检索池"),
    ("P1-14-1", True, "material_id", "编号在材料里出现（evaluation_table 外露）"),
    ("fe_wf_sample.txt-第10段", True, "evidence_locator", "文件名+段号 → chunk"),
    ("fe_wf_sample.txt-第1段", True, "evidence_locator", "文件名+段号 → chunk"),
    ("fe_wf_sample.txt-第四部分", True, "section_ordinal", "文件名+中文序号 → 小节"),
    ("第3段", True, "evidence_locator", "只给段号且唯一命中"),
    ("第四部分", True, "section_ordinal", "只给中文序号"),
    ("fe_wf_sample.txt-第1段及第四部分", True, "evidence_locator", "复合引用取最强锚点"),
    ("P1-10-1、资料「四、资金安排」", True, "evidence_id", "编号优先于标题"),
    ("资料「三、绩效目标」", True, "heading", "引号内标题"),
    # 弱定位（Batch 12）：行号在原文范围内，但那一行不在检索块里
    ("fe_wf_sample.txt 第14行", True, "material_locator", "空格分隔的文件名+行号，原文 16 行"),
    ("fe_wf_sample.txt-第14段", True, "material_locator", "第14段不在检索块，但原文有这一行"),
    ("fe_wf_sample.txt-第99段", False, "none", "行号越界 → 拒绝"),
    ("第10段", False, "none", "段号命中两个文件 → 无法判断，拒绝"),
    ("other.txt-第10段", False, "none", "写了文件但对不上 → 拒绝，不退回唯一候选"),
    ("fe_wf_sample.txt", False, "none", "只给文件名、无行号 → 仍拒绝"),
    ("P9-99-9", False, "none", "编造编号"),
    ("第八部分", False, "none", "材料里没有第八部分"),
    ("", False, "none", "空引用"),
    ("None", False, "none", "占位词不算引用"),
    ("L3_01_01_03", False, "none", "指标体系 id 不是证据锚点"),
]

print("headings =", HEADINGS)
print("ordinals =", ORDINALS)
print("-" * 78)

fails = 0
for text, want_ok, want_kind, desc in CASES:
    got = evidence_anchor.resolve(
        text, evidence_ids=IDS, material=MATERIAL,
        headings=HEADINGS, chunks=CHUNKS, ordinals=ORDINALS,
        raw_texts=RAW_TEXTS,
    )
    flag = "OK " if (got.ok == want_ok and got.kind == want_kind) else "FAIL"
    if flag == "FAIL":
        fails += 1
    print(f"{flag} {text!r:38} ok={got.ok!s:5} kind={got.kind:16} "
          f"matched={got.matched}  # {desc}")

print("-" * 78)
print(f"通过 {len(CASES) - fails}/{len(CASES)}")
sys.exit(1 if fails else 0)
