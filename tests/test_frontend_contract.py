"""前端 DOM 契约检查（批次 49；批次 55 扩到工作流页）。

**它不是浏览器测试**：不执行 JS、不验证布局与交互效果。
它只回答一个能在 CI 里稳定回答的问题：**页面里的"接线"有没有断**——

- `getElementById('x')` / `querySelector('#x')` 引用的 id，是否真的存在（标记里或 JS 模板里）；
- 内联 `onclick="fn(...)"` 调用的函数，是否真的定义了；
- 标记里是否有重复 id（重复时 `getElementById` 只返回第一个，第二个永远拿不到）；
- 几个关键交互的锚点是否还在。

为什么值得单独做：这些错都不会让页面报错，只会让某个按钮"点了没反应"。
B49 用这套检查找出并清掉了 2 个死钩子、3 个死函数。

批次 55 扩到 `frontend/workflow/index.html` 之后立刻又抓到一个：
`handleSSEEvent` 的 `token_update` 分支更新 `#token-used`，而这个元素从来不存在 ——
token 消耗一直没显示过，也没人发现。现在那个分支有了真实的落点。
"""
import re
from collections import Counter
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

PAGES = {
    "client": FRONTEND / "client" / "index.html",
    "workflow": FRONTEND / "workflow" / "index.html",
}

_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.I)


def analyze(path: Path) -> dict:
    """把一个页面的「接线」抽成集合，供各条断言使用。"""
    html = path.read_text(encoding="utf-8")
    markup = _SCRIPT.sub("", html)
    js = "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", html, flags=re.I))
    markup_ids = re.findall(r'\bid="([^"]+)"', markup)
    # JS 里"造出来"的 id：模板字符串里的 id="x"，以及 row.id = 'x' 这种属性赋值
    js_ids = set(re.findall(r'id="([^"]+)"', js))
    js_ids |= set(re.findall(r"\.id\s*=\s*['\"]([^'\"]+)['\"]", js))
    refs = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", html))
    refs |= set(re.findall(r"querySelector\(['\"]#([A-Za-z0-9_-]+)['\"]\)", html))
    handlers = set()
    for call in re.findall(r'on(?:click|change|input|keydown|submit)="([^"]+)"', html):
        match = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", call)
        if match:
            handlers.add(match.group(1))
    funcs = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    funcs |= set(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*function", html))
    return {"html": html, "markup_ids": markup_ids, "all_ids": set(markup_ids) | js_ids,
            "refs": refs, "handlers": handlers, "funcs": funcs}


ANALYSIS = {name: analyze(path) for name, path in PAGES.items()}
HTML = ANALYSIS["client"]["html"]
MARKUP_IDS = ANALYSIS["client"]["markup_ids"]

_CONTROL = {"if", "for", "while", "switch", "return", "typeof", "alert", "confirm"}


@pytest.mark.parametrize("page", PAGES, ids=list(PAGES))
def test_every_referenced_id_exists(page):
    data = ANALYSIS[page]
    missing = sorted(data["refs"] - data["all_ids"])

    assert missing == [], f"{page} 这些 id 被取用但没有定义：{missing}"


@pytest.mark.parametrize("page", PAGES, ids=list(PAGES))
def test_markup_has_no_duplicate_ids(page):
    duplicates = sorted(key for key, count in Counter(ANALYSIS[page]["markup_ids"]).items()
                        if count > 1)

    assert duplicates == [], f"{page} 标记里有重复 id（getElementById 只会拿到第一个）：{duplicates}"


@pytest.mark.parametrize("page", PAGES, ids=list(PAGES))
def test_inline_handlers_are_defined(page):
    data = ANALYSIS[page]
    missing = sorted(data["handlers"] - data["funcs"] - _CONTROL)

    assert missing == [], f"{page} 这些内联事件函数没有定义：{missing}"


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


def test_workflow_page_loads_the_step_module_before_using_it():
    """顺序错了页面就整个不工作（`WorkflowSteps` 未定义），而浏览器只在控制台报错。"""
    html = ANALYSIS["workflow"]["html"]
    shared = html.index("/static/shared/workflow-steps.js")
    inline = re.search(r"<script(?![^>]*src=)[^>]*>", html)

    assert inline is not None
    assert shared < inline.start()
    for fn in ("statesFromStatus", "mergeStatus", "shouldAutoFollow", "stepIcon"):
        assert f"WorkflowSteps.{fn}" in html, f"{fn} 没接上，模块白加了"


def test_workflow_step_list_has_its_anchors():
    ids = ANALYSIS["workflow"]["all_ids"]

    for anchor in ("step-list", "step-detail-panel", "step-detail-content",
                   "workflow-step-detail", "workflow-step-content", "token-used"):
        assert anchor in ids, anchor


def test_workflow_step_list_renders_from_step_states():
    """回归（批次 55）：只按 completedSteps 画，正在运行的那行一次重画就变 ⏳。

    用户报的现象就是它：运行中点别的步骤再点回来，运行中的步骤和后面一串
    都显示"未开始"—— 因为 pollStatus 每 5 秒调一次 renderStepList()。
    """
    html = ANALYSIS["workflow"]["html"]

    assert "const status = stepStates[n] || 'pending';" in html
    assert "completedSteps.has(n) ? 'completed'" not in html
