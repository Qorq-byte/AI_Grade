/* 模块切换与共享层联动回归（node --test）
 * 用最小 DOM 桩加载真实的 config/store/ui/api + client 页内联脚本，
 * 验证：hash 同步、单一可见性机制（不写 style.display）、TTL 内不重复拉取、切换骨架屏。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

const root = path.join(__dirname, '..');

function makeClassList() {
    const set = new Set();
    return {
        add: (...names) => names.forEach(n => set.add(n)),
        remove: (...names) => names.forEach(n => set.delete(n)),
        contains: n => set.has(n),
        toggle: n => (set.has(n) ? set.delete(n) : set.add(n)),
        get _set() { return set; },
    };
}

function makeElement(id, tag = 'div') {
    const el = {
        id,
        tagName: tag.toUpperCase(),
        className: '',
        _textContent: '',
        _innerHTML: '',
        hidden: false,
        style: {},
        dataset: {},
        checked: false,
        disabled: false,
        children: [],
        classList: makeClassList(),
        attributes: {},
        _listeners: {},
        setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'data-tab') this._tab = v; },
        getAttribute(k) { return this.attributes[k] === undefined ? null : this.attributes[k]; },
        addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
        removeEventListener() {},
        appendChild(child) { this.children.push(child); return child; },
        removeChild(child) { this.children = this.children.filter(c => c !== child); },
        remove() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        focus() {},
        click() {},
        get textContent() { return this._textContent; },
        set textContent(v) {
            this._textContent = String(v);
            this._innerHTML = this._textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            this.children = [];
        },
        get innerHTML() { return this._innerHTML; },
        set innerHTML(v) { this._innerHTML = String(v); this.children = []; },
    };
    return el;
}

function buildContext(fetchImpl) {
    const elements = new Map();
    const ids = ['dashboard', 'performance', 'reports', 'questionnaire', 'ai-chat', 'settings', 'help',
        'reportsGrid', 'questionnaireGrid', 'recentActivity', 'wfStepperList', 'promptTextarea',
        'statProjects', 'statCompleted', 'statDialogs', 'sidebarTokenCount', 'settingsTokens', 'editUsername'];
    ids.forEach(id => elements.set(id, makeElement(id)));

    const navTabs = ['dashboard', 'performance', 'reports', 'questionnaire', 'help'];
    const navLinks = navTabs.map(tab => {
        const el = makeElement('nav-' + tab, 'button');
        el.setAttribute('onclick', `switchTab('${tab}',this)`);
        return el;
    });

    const sections = navTabs.map(t => elements.get(t));

    const storage = (() => {
        const m = new Map();
        return {
            getItem: k => (m.has(k) ? m.get(k) : null),
            setItem: (k, v) => m.set(k, String(v)),
            removeItem: k => m.delete(k),
            key: i => Array.from(m.keys())[i] ?? null,
            get length() { return m.size; },
        };
    })();

    const document = {
        getElementById: id => elements.get(id) || null,
        createElement: tag => makeElement('', tag),
        head: makeElement('head'),
        body: makeElement('body'),
        documentElement: makeElement('html'),
        hidden: false,
        activeElement: null,
        addEventListener() {},
        removeEventListener() {},
        querySelector(sel) {
            const m = /\.nav-link\[data-tab="([^"]+)"\]/.exec(sel);
            if (m) return navLinks.find(l => l._tab === m[1]) || null;
            return null;
        },
        querySelectorAll(sel) {
            if (sel === '.nav-link') return navLinks;
            if (sel === '.content-section') return sections;
            return [];
        },
    };

    const history = {
        pushed: [],
        pushState(state, title, url) { this.pushed.push(url); location.hash = url; },
    };

    const location = { pathname: '/client', hash: '', search: '', href: '' };

    const ctx = {
        console, Promise, Date, JSON, Object, Math, Array, String, Number, RegExp,
        setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
        fetch: fetchImpl, AbortController, TextDecoder, FormData, URL, URLSearchParams, encodeURIComponent,
        sessionStorage: storage, localStorage: storage,
        document, history, location,
        addEventListener() {},
        innerWidth: 1280,
        crypto: { getRandomValues: arr => arr.fill(1) },
    };
    ctx.window = ctx;
    ctx.globalThis = ctx;
    vm.createContext(ctx);

    ['config', 'store', 'ui', 'api', 'workflow-controller'].forEach(name => {
        vm.runInContext(fs.readFileSync(path.join(root, `frontend/shared/${name}.js`), 'utf8'), ctx, { filename: name + '.js' });
    });

    const html = fs.readFileSync(path.join(root, 'frontend/client/index.html'), 'utf8');
    const inline = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
    vm.runInContext(inline, ctx, { filename: 'client-inline.js' });

    return { ctx, elements, navLinks, sections, storage, history, location };
}

function wait(ms = 5) { return new Promise(r => setTimeout(r, ms)); }

// Execute the real client/SSE handlers. Layout here is a small scroll-box stub;
// actual overflow, anchoring and page scrolling are checked with gstack browse.
function workflowContext() {
    const env = buildContext(async () => { throw new Error('unexpected request'); });
    const { ctx, elements } = env;
    for (let n = 1; n <= 14; n++) elements.set('wfStep' + n, makeElement('wfStep' + n));
    const panel = makeElement('wfContent');
    let body = null, stream = null, hint = null;
    Object.defineProperty(panel, 'innerHTML', {
        get() { return this._innerHTML; },
        set(value) {
            this._innerHTML = String(value);
            body = makeElement('body');
            body.clientHeight = 100;
            body.scrollTop = 0;
            const match = this._innerHTML.match(/class="[^"]*wf-stream-body[^"]*"[^>]*>([\s\S]*?)<\/div>/);
            stream = match ? makeElement('stream') : null;
            if (stream) stream.textContent = match[1];
            hint = this._innerHTML.includes('id="wfStreamHint"') ? makeElement('wfStreamHint') : null;
            if (hint) hint.remove = () => { hint = null; };
            Object.defineProperty(body, 'scrollHeight', { get: () => 200 + (stream?.textContent.length || 0) * 10 });
            body.querySelector = sel => sel === '.wf-stream-body' ? stream : sel === '#wfStreamHint' ? hint : null;
        },
    });
    panel.scrollTop = 0;
    panel.querySelector = sel => sel === '.wf-content-body' ? body : body?.querySelector(sel);
    elements.set('wfContent', panel);
    ctx.document.querySelectorAll = sel => sel === '.wf-step' ?
        [...elements.values()].filter(el => /^wfStep\d+$/.test(el.id)) : [];
    ctx.wfRunning = true;
    ctx.wfSessionId = 'workflow-test';
    const emit = (event, step, extra = {}) => ctx.handleSSEEvent({event, step_number: step, session_id: 'workflow-test', ...extra});
    return {...env, panel, emit, flush: () => ctx.wfDeltaRenderer.flushNow(),
        body: () => body, stream: () => stream};
}

test('切走后仍按步骤缓存，返回立即恢复完整增量并持续更新', () => {
    const e = workflowContext();
    e.emit('step_start', 4);
    e.emit('token_delta', 4, {delta: 'first'});
    e.flush();
    e.ctx.selectWfStep(5);
    e.emit('token_delta', 4, {delta: '-away'});
    e.flush();
    assert.equal(e.ctx.wfStepContents[4], 'first-away');
    assert.equal(e.ctx.wfStepContents[5], undefined);
    assert.match(e.panel.innerHTML, /该步骤尚未开始/);
    e.ctx.selectWfStep(4);
    assert.equal(e.stream().textContent, 'first-away');
    e.emit('token_delta', 4, {delta: '-back'});
    e.flush();
    assert.equal(e.stream().textContent, 'first-away-back');
});

test('首次 flush 之前切换也能立即读取已经收到的内容', () => {
    const e = workflowContext();
    e.emit('step_start', 8);
    e.ctx.selectWfStep(9);
    e.emit('token_delta', 8, {delta: 'already received'});
    e.ctx.selectWfStep(8);
    assert.equal(e.stream()?.textContent, 'already received');
    e.flush();
    assert.equal(e.stream().textContent, 'already received');
});

test('运行中未输出与尚未开始有各自的提示；进度重画保留选中步骤', () => {
    const e = workflowContext();
    e.emit('step_start', 4);
    assert.match(e.panel.innerHTML, /正在生成内容，请稍候/);
    e.ctx.selectWfStep(5);
    assert.match(e.panel.innerHTML, /该步骤尚未开始/);
    e.ctx.updateWfStepper();
    assert.ok(e.elements.get('wfStep5').classList.contains('active'));
    assert.equal(e.ctx.wfCurrentStep, 4);
});

test('其他步骤完成/开始不抢走用户选择；选中后续步骤即可实时显示', () => {
    const e = workflowContext();
    e.emit('step_start', 4);
    e.ctx.selectWfStep(10);
    e.emit('step_complete', 4, {content: 'final-four'});
    e.emit('step_start', 5);
    e.emit('token_delta', 5, {delta: 'five-live'});
    e.flush();
    assert.equal(e.ctx.wfSelectedStep, 10);
    assert.match(e.panel.innerHTML, /该步骤尚未开始/);
    e.ctx.selectWfStep(5);
    assert.equal(e.stream().textContent, 'five-live');
});

test('查看当前运行步时自动跟随后续步，开始事件不会丢弃提前到达的增量', () => {
    const e = workflowContext();
    e.emit('step_start', 4);
    e.emit('token_delta', 4, {delta: 'partial'});
    e.emit('step_complete', 4, {content: 'final-four'});
    e.emit('token_delta', 5, {delta: 'early-five'});
    e.emit('step_start', 5);
    e.flush();
    assert.equal(e.ctx.wfSelectedStep, 5);
    assert.equal(e.stream().textContent, 'early-five');
});

test('完成正文替换流式缓存，迟到的 delta 不重复追加到完成正文', () => {
    const e = workflowContext();
    e.emit('step_start', 4);
    e.emit('token_delta', 4, {delta: 'partial'});
    e.emit('step_complete', 4, {content: 'authoritative'});
    e.emit('token_delta', 4, {delta: 'late'});
    e.flush();
    assert.equal(e.ctx.wfStepContents[4], 'authoritative');
    assert.ok(e.ctx.wfCompletedSteps.includes(4));
    assert.match(e.panel.innerHTML, /authoritative/);
});

test('仅内容区贴底跟随，上翻停留，滚回底部恢复，切换保留阅读位置', () => {
    const e = workflowContext();
    e.emit('step_start', 4);
    e.emit('token_delta', 4, {delta: 'long'.repeat(50)});
    e.flush();
    assert.equal(e.body().scrollTop, e.body().scrollHeight);
    assert.equal(e.panel.scrollTop, 0);
    e.body().scrollTop = 35;
    e.emit('token_delta', 4, {delta: '-more'});
    e.flush();
    assert.equal(e.body().scrollTop, 35);
    e.ctx.selectWfStep(5);
    e.emit('token_delta', 4, {delta: '-away'});
    e.flush();
    e.ctx.selectWfStep(4);
    assert.equal(e.body().scrollTop, 35);
    e.body().scrollTop = e.body().scrollHeight;
    e.emit('token_delta', 4, {delta: '-tail'});
    e.flush();
    assert.equal(e.body().scrollTop, e.body().scrollHeight);
});

test('switchTab 只切 class，不写 style.display，并同步 hash', async () => {
    const calls = [];
    const { ctx, elements, navLinks, history } = buildContext(async (url) => {
        calls.push(url);
        return { ok: true, status: 200, headers: { get: () => null }, text: () => Promise.resolve(JSON.stringify({ success: true, data: [] })) };
    });
    ctx.registerTabs();
    ctx.bindNavTabs();

    ctx.switchTab('reports', navLinks[2]);
    await wait();

    assert.equal(elements.get('reports').classList.contains('active'), true);
    assert.equal(elements.get('dashboard').classList.contains('active'), false);
    for (const id of ['reports', 'dashboard', 'performance']) {
        assert.equal(elements.get(id).style.display, undefined, id + ' 不应写 style.display');
    }
    assert.equal(navLinks[2].classList.contains('active'), true);
    assert.equal(history.pushed.at(-1), '#reports');
    assert.equal(ctx.location.hash, '#reports');
});

test('30 次模块往复切换不产生重复请求（TTL 内复用）', async () => {
    const calls = [];
    const { ctx, elements, navLinks } = buildContext(async (url) => {
        calls.push(url);
        const data = url.indexOf('reports') >= 0 ? [{ session_id: 's1', project_name: '样例', status: '完成' }]
            : url.indexOf('activity') >= 0 ? [{ action: 'workflow_start', description: '启动', time: '刚刚' }] : { completed_workflows: 1 };
        return { ok: true, status: 200, headers: { get: () => null }, text: () => Promise.resolve(JSON.stringify({ success: true, data })) };
    });
    ctx.registerTabs();
    ctx.bindNavTabs();

    ctx.switchTab('reports', navLinks[2]);
    await wait();
    const afterFirst = calls.length;

    for (let i = 0; i < 30; i++) {
        ctx.switchTab('dashboard', navLinks[0]);
        await wait(1);
        ctx.switchTab('reports', navLinks[2]);
        await wait(1);
    }

    assert.equal(afterFirst, 1, '首次进入只应请求一次报告列表');
    assert.equal(calls.length, afterFirst + 2, '后续切换只应新增 dashboard 与 activity 各一次');
    assert.equal(calls.filter(u => u.indexOf('/api/client/reports') >= 0).length, 1);
});

test('离开模块时 abort 在途请求，错误被识别为取消而非失败', async () => {
    let aborted = 0;
    const { ctx, navLinks } = buildContext((url, init) => new Promise((resolve, reject) => {
        if (init && init.signal) {
            init.signal.addEventListener('abort', () => { aborted++; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); });
        }
        setTimeout(() => resolve({ ok: true, status: 200, headers: { get: () => null }, text: () => Promise.resolve('{"success":true,"data":[]}') }), 50);
    }));
    ctx.registerTabs();
    ctx.bindNavTabs();
    ctx.switchTab('reports', navLinks[2]);
    await wait(1);
    ctx.switchTab('dashboard', navLinks[0]);
    await wait(30);
    assert.ok(aborted >= 1, '报告模块离开时应取消在途请求');
});

test('首屏按 hash 恢复模块，未知 hash 回落到 dashboard', async () => {
    const { ctx, location } = buildContext(async () => ({ ok: true, status: 200, headers: { get: () => null }, text: () => Promise.resolve('{"success":true,"data":[]}') }));
    ctx.registerTabs();
    location.hash = '#questionnaire';
    assert.equal(ctx.restoreTabFromHash(), 'questionnaire');
    location.hash = '#not-a-tab';
    assert.equal(ctx.restoreTabFromHash(), 'dashboard');
});
