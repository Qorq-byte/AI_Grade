"""客户端标记与事件绑定的静态守卫（批次 13）。

`client/app.js` 原来有 22 处把数据拼进内联 onclick 的 JS 字符串字面量，其中
`exportExistingReport('sid','word','pn')` 的第三个参数是**用户填的项目名** ——
名字里一个单引号就让「导出/删除」三个按钮同时失效，一个双引号还能提前闭合属性。

这批把带数据的入口统一改成 `data-act` + 事件委托。下面几条防止它长回去。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "frontend" / "client" / "index.html").read_text(encoding="utf-8")
JS_PATH = ROOT / "frontend" / "client" / "app.js"
JS = JS_PATH.read_text(encoding="utf-8")


def test_script_is_extracted_and_referenced():
    """页面 no-store、脚本可缓存：这 150KB 不该每次访问都重下。"""
    assert '/static/client/app.js' in HTML
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", HTML)
    leftover = [b for b in inline if b.strip()]
    assert not leftover, f"index.html 里还残留 {len(leftover)} 段内联脚本"
    assert JS_PATH.is_file()


def test_no_inline_handler_passes_a_string_literal():
    """值不能既在 JS 字符串字面量里、又参与拼接。

    判据：同一个 handler 里同时出现 `\\'`（转义单引号）和 `'+`（拼接收尾）。
    - `f('+sn+')`           只有拼接，参数是数字 —— 安全
    - `f(\\'reports\\')`   只有字面量，参数是常量 —— 安全
    - `f(\\'rcard_'+sid+'\\')` 两者都有 —— 值被拼进了字面量，一个引号就断
    """
    handlers = re.findall(r'onclick="([^"]*)"', JS)
    bad = [h for h in handlers if "\\'" in h and "'+" in h]
    assert not bad, f"仍有内联 handler 把值拼进 JS 字面量：{len(bad)} 处，例如 {bad[:2]}"


def test_every_data_act_has_a_handler():
    declared = set(re.findall(r"'([a-z-]+)':function\(el\)", JS))
    used = set(re.findall(r'data-act="([a-z-]+)"', JS))
    assert used, "一个 data-act 都没找到，是不是改回内联写法了？"
    assert used <= declared, f"这些动作没有对应处理函数：{sorted(used - declared)}"


def test_dispatcher_is_the_single_action_entry():
    """data-act 只能有一个调度入口。

    页面里另有「点空白处收起用户菜单」的 document 监听，那是另一种职责，不算重复。
    """
    dispatchers = JS.count("closest('[data-act]')")
    assert dispatchers == 1, f"data-act 调度器应该只有一个，实际 {dispatchers} 个"


def test_user_supplied_fields_are_escaped_in_templates():
    """项目名会直接进 innerHTML —— 不转义就是存储型 XSS。"""
    assert "'>'+pn+'</div>'" not in JS, "项目名未经 esc 就拼进了标记"
    assert "\">'+esc(pn)+'</div>'" in JS


def test_esc_covers_quotes_in_shared_layer():
    ui = (ROOT / "frontend" / "shared" / "ui.js").read_text(encoding="utf-8")
    body = re.search(r"function esc\(text\)\{?([\s\S]*?)\n    \}", ui)
    assert body, "shared/ui.js 的 esc 没找到"
    for entity in ("&quot;", "&#39;"):
        assert entity in body.group(1), f"shared/ui.js 的 esc 缺少 {entity}"
