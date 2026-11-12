"""缺失类结论的引用清理（批次 52）。

B51 的引用抽读发现：报告写「缺立项批复」时，会顺手标一个 `[P1-3-1]`——
而那个编号指向的是项目概况或章节标题。文本证据只能证明「有什么」，不能证明「缺什么」，
这类编号等于误引。

先试了提示词（在 `_QUALITY_RULES` 加一条「缺失类结论不标编号」），
结果**更糟**：缺失表述带编号的比例从 6% 涨到 15%——规则提醒了「缺失」这个词，
模型反而更爱在那里挂编号。

所以改成确定性后处理：组装报告时，把「缺/未提供/…」同句里 60 字内的证据编号去掉。
只删括号里的编号，不动文字；正向结论的引用原样保留。
"""
from __future__ import annotations

import re
from typing import Tuple

#: 与 `tests/rag_eval/citation_audit.py` 同一套判定：缺失表述后 60 字内出现编号。
ABSENCE_WITH_ID = re.compile(
    r"[^。\n]{0,60}(?:缺|未提供|未取得|未见|未附)[^。\n]{0,60}?\[(P\d+-\d+-\d+)\]"
)


def strip_absence_ids(text: str) -> Tuple[str, int]:
    """去掉「缺失类结论」里的证据编号，返回（新文本, 去掉的个数）。

    只删 `[P1-x-y]` 本身（连同方括号），不删周围的字；
    `（[P1-3-1]对应佐证缺失）` 会变成 `（对应佐证缺失）`。
    """
    def _strip_once(source: str) -> Tuple[str, int]:
        removed = 0

        def _replace(match: re.Match) -> str:
            nonlocal removed
            removed += 1
            whole = match.group(0)
            start = match.start(1) - match.start(0)   # 指向 '['
            end = match.end(1) - match.start(0)       # 指向 ']' 之后
            return whole[:start - 1] + whole[end + 1:]

        return ABSENCE_WITH_ID.sub(_replace, source), removed

    # 一句话里可能挂了多个编号：删掉一个后，剩下的还在 60 字窗口里，要再来一遍。
    text = text or ""
    total = 0
    for _ in range(10):
        text, removed = _strip_once(text)
        total += removed
        if not removed:
            break
    return text, total
