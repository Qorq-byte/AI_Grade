"""缺失类结论的引用清理（批次 52 起，批次 53 改分句口径）。

B51 的引用抽读发现：报告写「缺立项批复」时，会顺手标一个 `[P1-3-1]`——
而那个编号指向的是项目概况或章节标题。文本证据只能证明「有什么」，不能证明「缺什么」，
这类编号等于误引。

先试了提示词（在 `_QUALITY_RULES` 加一条「缺失类结论不标编号」），
结果**更糟**：缺失表述带编号的比例从 6% 涨到 15%——规则提醒了「缺失」这个词，
模型反而更爱在那里挂编号。所以改成确定性后处理。

判定口径：**按句子**（。；！？换行）切，句子里出现「缺/未提供/…」就把该句里的
证据编号去掉。比"60 字窗口"稳：长句不漏、跨句不误删；
不按逗号切——中文里「缺 X，……（[P1]）」的引用常落在逗号之后，按逗号切会漏判。
"""
from __future__ import annotations

import re
from typing import Tuple

#: 句子边界（保留分隔符，便于原样拼回）。不切逗号，见模块说明。
SENTENCE_SPLIT = re.compile(r"([。；！？\n])")
#: 缺失类表述。
ABSENCE = re.compile(r"(?:缺|未提供|未取得|未见|未附)")
#: 证据编号（只认方括号形式）。
EVIDENCE_ID = re.compile(r"\[(P\d+-\d+-\d+)\]")


def strip_absence_ids(text: str) -> Tuple[str, int]:
    """去掉「缺失类结论」里的证据编号，返回（新文本, 去掉的个数）。

    只删 `[P1-x-y]` 本身（连同方括号），不删周围的字；
    `（[P1-3-1]对应佐证缺失）` 会变成 `（对应佐证缺失）`。
    """
    parts = SENTENCE_SPLIT.split(text or "")
    removed = 0
    # 偶数下标是句子本体，奇数是分隔符
    for index in range(0, len(parts), 2):
        clause = parts[index]
        if clause and ABSENCE.search(clause):
            parts[index], count = EVIDENCE_ID.subn("", clause)
            removed += count
    return "".join(parts), removed
