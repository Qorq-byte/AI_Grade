/**
 * 前端转义与事件绑定的加固回归（批次 13）。
 *
 * 起因：`shared/ui.js` 的 esc 用 textContent→innerHTML 实现，只转义 `& < >`。
 * 这个返回值大量被塞进 HTML 属性（data-name、title 等），值里一个双引号就能提前闭合属性；
 * 而拼进内联 onclick 的 JS 字符串字面量时，一个 `'` 就让按钮静默失效。
 *
 * 这里用真实源码跑，不复制实现。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function loadUi() {
    const ctx = {
        console,
        document: {
            // esc 已改成纯正则实现，但文件里其它函数仍会碰 DOM —— 给个最小桩
            createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {} }),
            getElementById: () => null,
            head: { appendChild() {} },
            body: { appendChild() {} },
            addEventListener() {},
        },
        setTimeout: () => 0,
        clearTimeout() {},
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(root, 'frontend/shared/ui.js'), 'utf8'),
        ctx,
        { filename: 'ui.js' }
    );
    return ctx;
}

test('esc 必须转义双引号，否则 data-*/title 属性会被提前闭合', () => {
    const { esc } = loadUi();
    assert.equal(esc('a"b'), 'a&quot;b');
    assert.equal(esc('带"引号"的名字'), '带&quot;引号&quot;的名字');
});

test('esc 也转义单引号，文本节点里显示不受影响', () => {
    const { esc } = loadUi();
    assert.equal(esc("o'brien"), 'o&#39;brien');
});

test('esc 原有的 & < > 行为不变', () => {
    const { esc } = loadUi();
    assert.equal(esc('<img src=x>'), '&lt;img src=x&gt;');
    assert.equal(esc('a & b'), 'a &amp; b');
    // & 必须最先替换，否则会把后面生成的实体再转一遍
    assert.equal(esc('a & <b>'), 'a &amp; &lt;b&gt;');
});

test('空值不返回 "null"/"undefined" 字符串', () => {
    const { esc } = loadUi();
    assert.equal(esc(null), '');
    assert.equal(esc(undefined), '');
    assert.equal(esc(''), '');
    assert.equal(esc(0), '0');
});

test('管理端的 esc 与共享层保持一致', () => {
    const adminSrc = fs.readFileSync(path.join(root, 'frontend/admin/app.js'), 'utf8');
    const match = adminSrc.match(/function esc\(s\)\{([\s\S]*?)\}\r?\n/);
    assert.ok(match, 'admin 的 esc 没找到');
    for (const entity of ['&quot;', '&#39;', '&lt;', '&gt;', '&amp;']) {
        assert.ok(match[1].includes(entity), `admin 的 esc 缺少 ${entity}`);
    }
});
