"""管理端暗色主题的静态守卫（批次 9 / P3-9）。

暗色主题最容易出的不是「不好看」，而是**一半深一半浅**：某个组件写了死颜色，
在暗色下变成一块刺眼的白。没法在测试里跑浏览器，就用静态检查把三类问题钉住：

1. `:root` 里出现自引用（`--bg:var(--bg-soft)`）—— 之前一次批量替换真的踩过，
   变量立刻变成空值，整站颜色崩掉，而且不报错；
2. 用了 `var(--x)` 但 `:root` 没定义 —— 静默失效；
3. `[data-theme="dark"]` 少定义了某个 token —— 那一项在暗色下仍是浅色。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "frontend" / "admin" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "frontend" / "admin" / "app.js").read_text(encoding="utf-8")


def _block(name: str) -> str:
    """取出 `:root{...}` 或 `[data-theme="dark"]{...}` 的声明体。"""
    if name == "root":
        m = re.search(r":root\{([^}]*)\}", HTML)
    else:
        m = re.search(r'\[data-theme="dark"\]\{([^}]*)\}', HTML)
    assert m, f"找不到 {name} 变量块"
    return m.group(1)


def _declared(body: str) -> dict:
    out = {}
    for item in body.split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip()
        if key.startswith("--"):
            out[key] = value.strip()
    return out


ROOT_VARS = _declared(_block("root"))
DARK_VARS = _declared(_block("dark"))


def test_nothing_is_self_referential():
    """`--bg:var(--bg-soft)` 这种会让变量直接失效，且浏览器不报错。"""
    for key, value in ROOT_VARS.items():
        assert value != f"var({key})", f"{key} 自引用"
    for key, value in DARK_VARS.items():
        assert value != f"var({key})", f"暗色 {key} 自引用"


def test_stylesheet_does_not_talk_to_itself_through_vars():
    """`--bg:var(--bg)` 之外，`--a:var(--b)` 而 `--b:var(--a)` 同样会成环。"""
    def resolves(key, depth=0):
        assert depth < 10, f"{key} 变量链成环"
        value = ROOT_VARS.get(key)
        if not value:
            return
        m = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())
        if m:
            resolves(m.group(1), depth + 1)

    for key in ROOT_VARS:
        resolves(key)


def test_every_var_used_is_defined():
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", HTML))
    missing = {u for u in used if u not in ROOT_VARS}
    assert not missing, f"用了但没定义：{sorted(missing)}"


def test_dark_block_covers_every_token():
    """暗色少定义一个 token，那一项在暗色下就还是浅色 —— 一块白斑。"""
    missing = set(ROOT_VARS) - set(DARK_VARS)
    assert not missing, f"暗色缺这些变量：{sorted(missing)}"


def test_no_hardcoded_page_backgrounds_left():
    """大面积背景必须先 token 化，否则暗色下一定露白。"""
    for pattern in ("background:white", "background:#fff;", "background:#ffffff"):
        assert HTML.count(pattern) == 0, f"index.html 仍有 {pattern}"
        assert APP_JS.count(pattern) == 0, f"app.js 仍有 {pattern}"


def test_theme_is_applied_before_first_paint():
    """主题必须在样式表之前挂到 <html>，否则会先闪一下白再变黑。"""
    head = HTML[: HTML.index("<style>")]
    assert "admin_theme" in head
    assert 'setAttribute("data-theme"' in head or "setAttribute('data-theme'" in head


def test_toggle_is_wired():
    assert 'id="theme-btn"' in HTML
    assert "function toggleTheme" in APP_JS
    assert "addEventListener('click',toggleTheme)" in APP_JS


def test_app_js_styles_use_tokens_not_literals():
    """脚本里拼内联样式最容易漏 —— 这里只盯住最大面积的那几种。"""
    for literal in ("background:white", "background:#f8f9fa", "background:#fff5f5"):
        assert literal not in APP_JS, f"app.js 内联样式仍有 {literal}"


# ═══════════════════════════════════════════════════════
#  对比度：没有浏览器也能验「读不读得清」
# ═══════════════════════════════════════════════════════

def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [
    ("--text", "--card", 7.0),          # 正文，要能长时间看
    ("--text", "--bg", 7.0),
    ("--text2", "--card", 4.5),
    ("--muted", "--card", 4.0),         # 次要说明
    ("--success-fg", "--success-bg", 4.5),
    ("--danger-fg", "--danger-bg", 4.5),
    ("--warning-fg", "--warning-bg", 4.5),
    ("--info-fg", "--info-bg", 4.5),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_stays_readable(theme):
    """暗色最常见的翻车是「深底深字」，靠计算对比度就能提前发现。"""
    palette = ROOT_VARS if theme == "light" else DARK_VARS
    for fg_key, bg_key, minimum in PAIRS:
        fg, bg = palette[fg_key], palette[bg_key]
        assert fg.startswith("#") and bg.startswith("#")
        ratio = _contrast(fg, bg)
        assert ratio >= minimum, f"{theme} {fg_key} on {bg_key} 只有 {ratio:.2f}:1（要求 {minimum}）"
