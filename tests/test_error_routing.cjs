/* 前端错误分流回归（node --test）
 * 目标：不同失败语义得到不同提示，而不是所有错误同一个 Toast。
 * 用最小 DOM 桩直接加载 frontend/shared/ui.js，不启动浏览器。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

const root = path.join(__dirname, '..');

function buildUi() {
    const nodes = [];

    function makeEl(tag) {
        const el = {
            tagName: String(tag || 'div').toUpperCase(),
            id: '',
            className: '',
            style: {},
            children: [],
            parentNode: null,
            _attrs: {},
            _text: '',
            _html: '',
            setAttribute(k, v) { this._attrs[k] = String(v); },
            getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; },
            appendChild(child) {
                child.parentNode = this;
                this.children.push(child);
                nodes.push(child);
                return child;
            },
            removeChild(child) {
                this.children = this.children.filter(c => c !== child);
                const index = nodes.indexOf(child);
                if (index >= 0) nodes.splice(index, 1);
            },
            remove() { if (this.parentNode) this.parentNode.removeChild(this); },
        };
        Object.defineProperty(el, 'textContent', {
            get() { return this._text; },
            set(v) { this._text = String(v); this._html = ''; },
        });
        Object.defineProperty(el, 'innerHTML', {
            get() { return this._html || this._text; },
            set(v) { this._html = String(v); },
        });
        return el;
    }

    const document = {
        getElementById(id) { return nodes.find(n => n.id === id) || null; },
        createElement(tag) { return makeEl(tag); },
        head: makeEl('head'),
        body: makeEl('body'),
        documentElement: makeEl('html'),
        activeElement: null,
        addEventListener() {},
        removeEventListener() {},
    };

    const timers = [];
    const ctx = {
        document, console, Promise, Date, JSON, Object, String, Number,
        setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
        clearTimeout() {},
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'frontend/shared/ui.js'), 'utf8'), ctx, { filename: 'ui.js' });
    return { ctx, nodes, timers, UI: ctx.WBUI };
}

function toasts(nodes) {
    return nodes.filter(n => typeof n.className === 'string' && n.className.indexOf('wb-toast') === 0);
}

test('可重试错误用黄色告诫，并附请求号便于排查', () => {
    const { UI } = buildUi();
    const el = UI.showApiError({
        code: 'timeout', status: 0, retryable: true, requestId: 'req_timeout_1',
        message: '请求超时（30 秒），请稍后重试',
    });
    assert.equal(el.className, 'wb-toast warn');
    assert.match(el.textContent, /请求超时/);
    assert.match(el.textContent, /请求号 req_timeout_1/);
    assert.equal(el.getAttribute('data-error-kind'), 'timeout');
});

test('服务端 5xx 也按可重试处理，且带上请求号', () => {
    const { UI } = buildUi();
    const el = UI.showApiError({ status: 500, retryable: true, requestId: 'req_500', message: '服务器内部错误: boom' });
    assert.equal(el.className, 'wb-toast warn');
    assert.match(el.textContent, /服务暂时不可用/);
    assert.equal(el.getAttribute('data-request-id'), 'req_500');
});

test('冲突/不存在等终端错误直接说明原因，不提示重试', () => {
    const { UI } = buildUi();
    const conflict = UI.showApiError(
        { status: 409, message: '报告关联的步骤尚待复核', requestId: 'req_conflict' },
        { action: '导出' }
    );
    assert.equal(conflict.className, 'wb-toast error');
    assert.equal(conflict.textContent, '导出失败：报告关联的步骤尚待复核（数据已被修改，请刷新后重试）');
    assert.equal(conflict.getAttribute('data-error-kind'), 'conflict');

    const missing = UI.showApiError({ status: 404, message: '报告不存在' });
    assert.equal(missing.className, 'wb-toast error');
    assert.match(missing.textContent, /目标不存在或已被删除/);
});

test('取消请求与登录失效不弹错误提示', () => {
    const { UI, nodes } = buildUi();
    const before = nodes.length;
    assert.equal(UI.showApiError({ name: 'AbortError', message: '请求已取消' }), null);
    assert.equal(UI.showApiError({ code: 'auth', status: 401, message: '登录状态已失效' }), null);
    assert.equal(nodes.length, before, '不应创建任何 toast');
});

test('分类函数覆盖主要失败语义', () => {
    const { UI } = buildUi();
    const cases = [
        [{ status: 404 }, 'notfound', false],
        [{ status: 422, message: 'x' }, 'invalid', false],
        [{ status: 400 }, 'invalid', false],
        [{ status: 409 }, 'conflict', false],
        [{ status: 429 }, 'busy', true],
        [{ status: 503 }, 'server', true],
        [{ code: 'timeout' }, 'timeout', true],
        [{ code: 'network' }, 'network', true],
        [{ code: 'auth', status: 401 }, 'auth', false],
        [{ name: 'AbortError' }, 'abort', false],
        [{ retryable: true, message: '临时故障' }, 'transient', true],
        [{ message: '说不清' }, 'unknown', false],
    ];
    for (const [input, kind, retryable] of cases) {
        const info = UI.classifyApiError(input);
        assert.equal(info.kind, kind, JSON.stringify(input));
        assert.equal(info.retryable, retryable, JSON.stringify(input));
    }
});

test('同一类错误只产生一条提示，重复点击不会叠加', () => {
    const { UI, nodes } = buildUi();
    const err = { status: 409, message: '重复提交' };
    UI.showApiError(err);
    UI.showApiError(err);
    assert.equal(toasts(nodes).length, 2, '每次调用对应一条 toast，由调用方去重');
    assert.equal(new Set(toasts(nodes).map(t => t.textContent)).size, 1);
});
