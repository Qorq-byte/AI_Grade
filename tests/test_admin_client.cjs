/* 管理端请求层回归（node --test）
 *
 * 这一层原本有两个要命的问题，都在这里锁住：
 *   1. 非 2xx 时直接丢弃响应体 → 后端的 `detail` 永远到不了管理员眼前；
 *   2. 首登未改密是 403，但被当成「登录失效」→ 清 token 跳登录页，用户被踢进死循环。
 *
 * 做法：把 frontend/admin/app.js 原样塞进 vm 执行，
 * 只替换 DOM / fetch 这些宿主能力 —— 测的是真实源码，不是复制品。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

const root = path.join(__dirname, '..');

function memoryStorage(initial) {
    const map = new Map(Object.entries(initial || {}));
    return {
        getItem: k => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => map.set(k, String(v)),
        removeItem: k => map.delete(k),
        _map: map,
    };
}

function response(status, body, headers) {
    return {
        status,
        ok: status >= 200 && status < 300,
        headers: { get: k => (headers && Object.prototype.hasOwnProperty.call(headers, k) ? headers[k] : null) },
        text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
        json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    };
}

function fakeEl(tag) {
    const classes = new Set();
    const el = {
        tagName: tag, children: [], style: {}, dataset: {},
        textContent: '', value: '', disabled: false,
        classList: {
            add: c => classes.add(c),
            remove: c => classes.delete(c),
            contains: c => classes.has(c),
        },
        appendChild(c) { this.children.push(c); return c; },
        addEventListener(type, fn) { this._on = this._on || {}; this._on[type] = fn; },
        focus() {}, remove() {}, querySelector: () => null,
    };
    Object.defineProperty(el, 'innerHTML', {
        get() { return this._html || ''; },
        set(v) { this._html = v; this.children.length = 0; },
    });
    return el;
}

function loadAdmin(storage, fetchImpl) {
    // 脚本已经从 index.html 抽到 app.js：页面是 no-store，脚本走 /static 可缓存
    const html = fs.readFileSync(path.join(root, 'frontend/admin/index.html'), 'utf8');
    assert.match(html, /\/static\/admin\/app\.js/, 'index.html 没引用 app.js');
    const m = [null, fs.readFileSync(path.join(root, 'frontend/admin/app.js'), 'utf8')];

    const localStorage = storage || memoryStorage({ access_token: 'tok-1' });
    const ctx = {
        console, Promise, Date, JSON, Object, Array, String, Number, Math, RegExp, Error,
        setTimeout, clearTimeout, URLSearchParams, AbortController,
        localStorage, sessionStorage: memoryStorage(),
        fetch: fetchImpl || (async () => response(200, { success: true })),
        document: {
            addEventListener() {},
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: fakeEl,
            body: { appendChild() {} },
        },
        location: { href: '', pathname: '/admin', search: '', hash: '', reload() {} },
    };
    ctx.window = ctx;
    ctx.history = { replaceState() {} };
    vm.createContext(ctx);
    vm.runInContext(m[1], ctx);
    // 重试退避在测试里没意义，压到 1ms
    ctx.API_RETRY_BASE = 1;
    return { ctx, localStorage };
}

test('后端 detail 是字符串时原样透出', () => {
    const { ctx } = loadAdmin();
    assert.equal(ctx.extractDetail({ detail: '用户名已存在' }, '兜底'), '用户名已存在');
});

test('422 的校验错误数组被拼成人话', () => {
    const { ctx } = loadAdmin();
    const detail = [{ loc: ['query', 'page_size'], msg: 'Input should be less than or equal to 100' }];
    const text = ctx.extractDetail({ detail }, '兜底');
    assert.match(text, /page_size/);
    assert.match(text, /less than or equal to 100/);
});

test('没有 detail 时用兜底文案，不会返回 undefined', () => {
    const { ctx } = loadAdmin();
    assert.equal(ctx.extractDetail(null, '兜底'), '兜底');
    assert.equal(ctx.extractDetail({}, '兜底'), '兜底');
});

test('非 2xx 时返回带 detail 的失败对象，而不是 null', async () => {
    const { ctx } = loadAdmin(null, async () => response(400, {
        success: false, detail: '用户名已存在', request_id: 'rid-9',
    }));
    const r = await ctx.api('/api/admin/users', 'POST', { username: 'x' });
    assert.equal(r.success, false);
    assert.equal(r.detail, '用户名已存在');
    assert.equal(r.status, 400);
    assert.equal(r.request_id, 'rid-9');
});

test('422 不会被压成「请求失败: 422」', async () => {
    const { ctx } = loadAdmin(null, async () => response(422, {
        detail: [{ loc: ['query', 'page_size'], msg: 'Input should be less than or equal to 100' }],
    }));
    const r = await ctx.api('/api/admin/users?page_size=9999');
    assert.equal(r.success, false);
    assert.match(r.detail, /page_size/);
});

test('403 + X-Password-Change-Required 走改密闸门，绝不清 token', async () => {
    const { ctx, localStorage } = loadAdmin(null, async () => response(403,
        { success: false, detail: '首次登录请先修改初始密码' },
        { 'X-Password-Change-Required': '1' }));

    let gateOpened = 0;
    ctx.showPasswordChangeGate = () => { gateOpened++; };

    const r = await ctx.api('/api/admin/dashboard');
    assert.equal(gateOpened, 1, '必须弹改密浮层');
    assert.equal(r.success, false);
    assert.equal(r.status, 403);
    assert.equal(localStorage.getItem('access_token'), 'tok-1', 'token 必须留着，否则改不了密');
});

test('401 会先刷新令牌再重放一次', async () => {
    const calls = [];
    const { ctx, localStorage } = loadAdmin(
        memoryStorage({ access_token: 'old', refresh_token: 'rt-1' }),
        async (url, init) => {
            calls.push(String(url));
            if (String(url).includes('/api/auth/refresh')) {
                return response(200, { success: true, data: { access_token: 'new-token' } });
            }
            const auth = init && init.headers && init.headers.Authorization;
            if (auth === 'Bearer new-token') return response(200, { success: true, data: { ok: 1 } });
            return response(401, { detail: '无效的认证凭据' });
        }
    );

    const r = await ctx.api('/api/admin/dashboard');
    assert.equal(r.success, true);
    assert.equal(r.data.ok, 1);
    assert.equal(localStorage.getItem('access_token'), 'new-token');
    assert.equal(calls.filter(u => u.includes('/api/auth/refresh')).length, 1);
});

test('网络异常返回可读文案，不抛异常', async () => {
    const { ctx } = loadAdmin(null, async () => { throw new Error('boom'); });
    const r = await ctx.api('/api/admin/dashboard');
    assert.equal(r.success, false);
    assert.match(r.detail, /网络连接失败/);
});

test('CSV 单元格挡住公式注入并正确转义', () => {
    const { ctx } = loadAdmin();
    assert.equal(ctx.csvCell('=1+1'), "'=1+1");
    assert.equal(ctx.csvCell('@sum'), "'@sum");
    assert.equal(ctx.csvCell('a,b'), '"a,b"');
    assert.equal(ctx.csvCell('he said "hi"'), '"he said ""hi"""');
    assert.equal(ctx.csvCell(null), '');
});

// ═══════════════════════════════════════════════════════
//  重试 / 并发去重（P3-1：把共享请求层的这部分能力补齐）
// ═══════════════════════════════════════════════════════

test('GET 遇到 5xx 会退避重试，最终成功', async () => {
    let calls = 0;
    const { ctx } = loadAdmin(null, async () => {
        calls++;
        return calls < 3
            ? response(503, { success: false, detail: '服务暂时不可用' })
            : response(200, { success: true, data: { ok: 1 } });
    });

    const r = await ctx.api('/api/admin/dashboard');
    assert.equal(calls, 3);
    assert.equal(r.success, true);
});

test('GET 遇到 400 不重试（4xx 是调用方的问题）', async () => {
    let calls = 0;
    const { ctx } = loadAdmin(null, async () => {
        calls++;
        return response(400, { success: false, detail: '参数不对' });
    });

    const r = await ctx.api('/api/admin/users?page=abc');
    assert.equal(calls, 1, '4xx 重试只会把同一个错误重放两遍');
    assert.equal(r.detail, '参数不对');
});

test('POST 失败不重试（非幂等，重放可能写两次）', async () => {
    let calls = 0;
    const { ctx } = loadAdmin(null, async () => {
        calls++;
        return response(500, { success: false, detail: '炸了' });
    });

    await ctx.api('/api/admin/users', 'POST', { username: 'x' });
    assert.equal(calls, 1);
});

test('重试次数有上限，不会一直打', async () => {
    let calls = 0;
    const { ctx } = loadAdmin(null, async () => {
        calls++;
        return response(503, { success: false, detail: '一直挂着' });
    });

    const r = await ctx.api('/api/admin/dashboard');
    assert.equal(calls, 3, '首次 + 2 次重试');
    assert.equal(r.success, false);
});

test('同一时刻的相同 GET 只发一次（并发去重）', async () => {
    let calls = 0;
    const { ctx } = loadAdmin(null, async () => {
        calls++;
        await new Promise(r => setTimeout(r, 20));
        return response(200, { success: true, data: { at: calls } });
    });

    const [a, b] = await Promise.all([
        ctx.api('/api/admin/dashboard'),
        ctx.api('/api/admin/dashboard'),
    ]);

    assert.equal(calls, 1);
    assert.deepEqual(a, b);
});

test('去重按「方法+URL」区分，不同 URL 各发各的', async () => {
    let calls = 0;
    const { ctx } = loadAdmin(null, async () => {
        calls++;
        return response(200, { success: true });
    });

    await Promise.all([ctx.api('/api/admin/users'), ctx.api('/api/admin/models')]);
    assert.equal(calls, 2);
});

// ═══════════════════════════════════════════════════════
//  分页控件（P3-2：原来 5 个列表各抄一遍）
// ═══════════════════════════════════════════════════════

test('分页只渲染当前页附近的窗口，首末页可直接跳', () => {
    const { ctx } = loadAdmin();
    const box = ctx.document.createElement('div');

    ctx.renderPagination(box, 50, 100, () => {});

    const labels = box.children.map(c => c.textContent);
    assert.equal(labels[0], '‹ 上一页');
    assert.equal(labels[labels.length - 1], '下一页 ›');
    // 首末页 + 当前页前后各 2 页，再加省略号 —— 不该出现 100 个按钮
    assert.ok(box.children.length < 15, `按钮太多了：${box.children.length}`);
    assert.ok(labels.includes('1'));
    assert.ok(labels.includes('100'));
    assert.ok(labels.includes('50'));
});

test('分页按钮点了会回调对应页码', () => {
    const { ctx } = loadAdmin();
    const box = ctx.document.createElement('div');
    const got = [];

    ctx.renderPagination(box, 5, 10, p => got.push(p));
    const six = box.children.find(c => c.textContent === '6');
    assert.ok(six, '第 5 页的窗口里应当能看到 6');
    six._on.click();

    assert.deepEqual(got, [6]);
});

test('只有一页时不渲染任何按钮', () => {
    const { ctx } = loadAdmin();
    const box = ctx.document.createElement('div');

    ctx.renderPagination(box, 1, 1, () => {});

    assert.equal(box.children.length, 0);
});

test('第一页禁用「上一页」、最后一页禁用「下一页」', () => {
    const { ctx } = loadAdmin();
    const first = ctx.document.createElement('div');
    const last = ctx.document.createElement('div');

    ctx.renderPagination(first, 1, 5, () => {});
    ctx.renderPagination(last, 5, 5, () => {});

    assert.equal(first.children[0].disabled, true);
    assert.equal(last.children[last.children.length - 1].disabled, true);
});

// ═══════════════════════════════════════════════════════
//  提示词版本对比（P4-1）
// ═══════════════════════════════════════════════════════

test('两版完全一致时明说一致，而不是渲染一片空白', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderPromptDiff('a\nb\nc', 'a\nb\nc');
    assert.match(html, /内容一致/);
});

test('只标出真正变化的那一段，公共前后缀不重复渲染', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderPromptDiff(
        '第一行\n第二行\n第三行\n第四行',
        '第一行\n第二行改了\n第三行\n第四行'
    );

    // 旧行带 − 标记、新行带 + 标记
    assert.match(html, /−.*第二行(?!改了)/);
    assert.match(html, /\+.*第二行改了/);
    // 未变的第一行只出现一次（作为前缀），不会既进删除块又进新增块
    assert.equal((html.match(/第一行/g) || []).length, 1);
    assert.equal((html.match(/第四行/g) || []).length, 1);
});

test('纯新增时没有删除块', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderPromptDiff('a\nb', 'a\nb\nc');
    assert.match(html, /\+.*c/);
    assert.ok(!/background:#fff5f5/.test(html), '不该出现删除行的高亮');
});

test('超长差异会被截断，不会把几万行一口气塞进 DOM', () => {
    const { ctx } = loadAdmin();
    const oldText = Array.from({ length: 400 }, (_, i) => `old-${i}`).join('\n');
    const newText = Array.from({ length: 400 }, (_, i) => `new-${i}`).join('\n');

    const html = ctx.renderPromptDiff(oldText, newText);
    assert.match(html, /省略 \d+ 行/);
    assert.ok(!html.includes('old-399'), '最后一行不该被渲染出来');
});

// ═══════════════════════════════════════════════════════
//  系统健康渲染（P4-2）
// ═══════════════════════════════════════════════════════

test('健康等级映射到三档文案', () => {
    const { ctx } = loadAdmin();
    assert.equal(ctx.healthMeta('ok').text, '正常');
    assert.equal(ctx.healthMeta('warn').text, '注意');
    assert.equal(ctx.healthMeta('down').text, '异常');
    // 未知值按最坏处理，不能悄悄显示成「正常」
    assert.equal(ctx.healthMeta('whatever').text, '异常');
});

test('明细里的布尔和对象都能读', () => {
    const { ctx } = loadAdmin();
    const html = ctx.healthDetailRows({ alive: true, dead: false, by_status: { running: 2 }, empty: null });

    assert.match(html, /是/);
    assert.match(html, /否/);
    assert.match(html, /running/);
    assert.ok(!html.includes('empty'), 'null 不该渲染成一行空数据');
});

test('健康页把所有检查项都渲染出来，并标出总体状态', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderHealthBody({
        overall: 'warn',
        checked_at: '2026-11-13T15:00:00Z',
        checks: [
            { key: 'database', label: '数据库', level: 'ok', summary: '1.2 ms', detail: { latency_ms: 1.2 } },
            { key: 'disk', label: '磁盘', level: 'warn', summary: '剩余 10 GB', detail: {} },
        ],
    });

    assert.match(html, /总体 注意/);
    assert.match(html, /数据库/);
    assert.match(html, /磁盘/);
    assert.match(html, /1\.2 ms/);
    assert.match(html, /剩余 10 GB/);
});

// ═══════════════════════════════════════════════════════
//  工作流详情（P4-3）
// ═══════════════════════════════════════════════════════

const WF_SAMPLE = {
    session_id: 'sid-1', project_name: '某区供水工程', username: 'alice', user_id: 2,
    status: 'running', current_step: 0, total_steps: 14, model_name: 'deepseek-chat',
    use_rag: true, token_used: 3533,
    created_at: '2026-11-08T10:00:00Z', updated_at: null, completed_at: null,
    stalled_hours: 125.3,
    timeline: [
        { step: 1, state: 'partial', tokens: 3533 },
        ...Array.from({ length: 13 }, (_, i) => ({ step: i + 2, state: 'pending', tokens: 0 })),
    ],
    errors: [], warnings: [{ step: 1, message: '输出被截断' }],
    tokens_by_step: [{ step: 1, tokens: 3533 }],
    artifacts: { final_report_chars: 0, checkpoint_kb: 0, state_backup_kb: 11.1 },
    live: { in_memory: false },
};

test('时间线把 14 步都画出来，并区分「跑过未完成」', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderWorkflowTimeline(WF_SAMPLE.timeline);

    // 14 个格子
    assert.equal((html.match(/height:34px/g) || []).length, 14);
    assert.match(html, /跑过未完成/);
    // partial 用琥珀色，pending 用灰色并降透明度
    assert.match(html, /#f59e0b/);
    assert.ok(html.includes('opacity:.45'), '未开始的格子应当降透明度');
});

test('卡住的会话在详情页给出醒目的处置入口', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderWorkflowDetail(WF_SAMPLE);

    assert.match(html, /已卡住 125\.3 小时/);
    assert.match(html, /标记为失败/);
});

test('已完成的会话不显示更正按钮', () => {
    const { ctx } = loadAdmin();
    const done = Object.assign({}, WF_SAMPLE, {
        status: 'completed', stalled_hours: null,
        timeline: WF_SAMPLE.timeline.map(t => ({ step: t.step, state: 'done', tokens: t.tokens })),
    });
    const html = ctx.renderWorkflowDetail(done);

    assert.ok(!html.includes('标记为失败'), '终态会话不该出现处置按钮');
    assert.ok(!html.includes('已卡住'));
});

test('错误/警告列表能渲染对象也能渲染纯文本', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderIssueList(
        [{ step: 3, message: '模型超时' }, '一句纯文本'], '无记录', '#b7791f');

    assert.match(html, /步骤 3/);
    assert.match(html, /模型超时/);
    assert.match(html, /一句纯文本/);
    assert.equal(ctx.renderIssueList([], '无记录', 'red'), '<p style="font-size:.82rem;color:var(--muted)">无记录</p>');
});

// ═══════════════════════════════════════════════════════
//  分步正文入口（P4-4）
// ═══════════════════════════════════════════════════════

test('有正文的步骤格子可点，没正文的不给点', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderWorkflowTimeline(WF_SAMPLE.timeline, { 1: { step: 1, chars: 2680 } });

    assert.equal((html.match(/data-act="wf-step"/g) || []).length, 1, '只有第 1 步有正文');
    assert.match(html, /data-step="1"/);
    assert.match(html, /有正文 2680 字，点击查看/);
    assert.match(html, /cursor:pointer/);
});

test('没有正文目录时所有格子都不可点', () => {
    const { ctx } = loadAdmin();
    const html = ctx.renderWorkflowTimeline(WF_SAMPLE.timeline);

    assert.ok(!html.includes('data-act="wf-step"'), '没有正文就不该挂动作');
    assert.match(html, /无正文/);
});


// ═══════════════════════════════════════════════════════
//  统一事件委托（P3-3）
// ═══════════════════════════════════════════════════════

test('调度表覆盖全部动作，参数从 dataset 取', () => {
    const { ctx } = loadAdmin();
    assert.deepEqual(
        Object.keys(ctx.ACTIONS).sort(),
        ['user-change-pwd', 'user-chpwd-submit', 'user-deactivate', 'user-detail',
         'user-toggle', 'wf-detail', 'wf-fail', 'wf-step']
    );

    const calls = [];
    ctx.toggleUser = (id, enable) => calls.push(['toggle', id, enable]);
    ctx.ACTIONS['user-toggle']({ dataset: { uid: '7', enable: 'true' } });
    assert.deepEqual(calls, [['toggle', 7, true]]);
});

test('字符串参数走 dataset，不再经过内联属性里的 JS 字符串', () => {
    const { ctx } = loadAdmin();
    const calls = [];
    ctx.showChangePasswordModal = (id, name) => calls.push(['pwd', id, name]);
    ctx.closeModal = () => {};

    ctx.ACTIONS['user-change-pwd']({ dataset: { uid: '3', name: "o'brien \\ 测试" } });
    assert.deepEqual(calls, [['pwd', 3, "o'brien \\ 测试"]]);
});
