"""前端 DOM 契约检查（批次 49）。

**它不是浏览器测试**：不执行 JS、不验证布局与交互效果。
它只回答一个能在 CI 里稳定回答的问题：**页面里的"接线"有没有断**——

- `getElementById('x')` / `querySelector('#x')` 引用的 id，是否真的存在（标记里或 JS 模板里）；
- 内联 `onclick="fn(...)"` 调用的函数，是否真的定义了；
- 标记里是否有重复 id（重复时 `getElementById` 只返回第一个，第二个永远拿不到）；
- 几个关键交互的锚点是否还在。

为什么值得单独做：这些错都不会让页面报错，只会让某个按钮"点了没反应"。
B49 用这套检查找出并清掉了 2 个死钩子、3 个死函数。
"""
import re
from collections import Counter
from pathlib import Path

import pytest

HTML_PATH = Path(__file__).resolve().parent.parent / "frontend" / "client" / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")

_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.I)
MARKUP = _SCRIPT.sub("", HTML)
JS = "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", HTML, flags=re.I))

MARKUP_IDS = re.findall(r'\bid="([^"]+)"', MARKUP)
JS_IDS = set(re.findall(r'id="([^"]+)"', JS))
ALL_IDS = set(MARKUP_IDS) | JS_IDS

REFS = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", HTML))
REFS |= set(re.findall(r"querySelector\(['\"]#([A-Za-z0-9_-]+)['\"]\)", HTML))

_CONTROL = {"if", "for", "while", "switch", "return", "typeof", "alert", "confirm"}
HANDLERS = set()
for call in re.findall(r'on(?:click|change|input|keydown|submit)="([^"]+)"', HTML):
    match = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", call)
    if match:
        HANDLERS.add(match.group(1))

FUNCS = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", HTML))
FUNCS |= set(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*function", HTML))


def test_every_referenced_id_exists():
    missing = sorted(REFS - ALL_IDS)

    assert missing == [], f"这些 id 被取用但没有定义：{missing}"


def test_markup_has_no_duplicate_ids():
    duplicates = sorted(key for key, count in Counter(MARKUP_IDS).items() if count > 1)

    assert duplicates == [], f"标记里有重复 id（getElementById 只会拿到第一个）：{duplicates}"


def test_inline_handlers_are_defined():
    missing = sorted(HANDLERS - FUNCS - _CONTROL)

    assert missing == [], f"这些内联事件函数没有定义：{missing}"


@pytest.mark.parametrize("anchor", [
    "chatMessages", "promptTextarea", "aiModelSelect",
    "wfLayout", "wfFileZone", "wfProgress",
])
def test_key_anchors_still_exist(anchor):
    assert anchor in MARKUP_IDS


def test_comparison_panel_is_wired_into_the_step_panel():
    """面板本身是 JS 拼出来的，这里只查"有没有被挂到步骤面板上、有没有入口"。"""
    assert "inner+=buildComparisonPanel(sn)" in HTML
    assert "function buildComparisonPanel(sn)" in HTML
    assert 'onclick="selectCandidate(' in HTML
    assert 'onclick="showCandidateDiff(' in HTML
    assert 'onclick="showAllDiffs(' in HTML
