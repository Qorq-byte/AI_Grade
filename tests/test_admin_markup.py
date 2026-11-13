"""管理端标记/委托的静态守卫（批次 10 / P3-3）。

把字符串拼进 `onclick="foo(1,'名字')"` 是这里唯一的**正确性**风险：引号和反斜杠
都要手工转义，漏一个整行按钮就静默失效（不会报错，只是点了没反应）。

这批把这类写法统一换成 `data-act` + 一张调度表。下面几条防止它悄悄长回来。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "admin" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "admin" / "index.html").read_text(encoding="utf-8")


def test_no_string_literal_inside_inline_handlers():
    """`onclick="f('+id+',\\'名字\\')"` 这种写法一律不许再出现。"""
    bad = re.findall(r'onclick="[^"]*\\\'', APP_JS)
    assert not bad, f"仍有内联 handler 传字符串字面量：{bad[:3]}"


def test_no_manual_quote_escaping():
    """手工 replace 转义是同一个病的另一种写法。"""
    assert '.replace(/\\\\/g,"\\\\\\\\")' not in APP_JS
    assert """replace(/'/g,"\\\\'")""" not in APP_JS


def test_every_data_act_has_a_handler():
    """拼错一个 data-act 就是一个按钮点了没反应，而且不报错。"""
    declared = set(re.findall(r"'([a-z-]+)':function\(el\)", APP_JS))
    used = set(re.findall(r'data-act="([a-z-]+)"', APP_JS))
    assert used, "一个 data-act 都没找到，是不是改回内联写法了？"
    assert used <= declared, f"这些动作没有对应处理函数：{sorted(used - declared)}"


def test_no_leftover_ad_hoc_data_attributes():
    """迁移要迁干净：三套 data-* 并存时，最容易出现「改了一处漏一处」。"""
    for legacy in ('data-action="', 'data-wf="', 'data-wfhas'):
        assert legacy not in APP_JS, f"还残留 {legacy}"


def test_single_click_dispatcher():
    """多个 document 级 click 监听器会互相抢事件，维护时也难判断谁在管。"""
    listeners = APP_JS.count("document.addEventListener('click'")
    assert listeners == 1, f"有 {listeners} 个全局 click 监听器，应当只有一个调度入口"
