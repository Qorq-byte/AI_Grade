/* 共享前端层回归测试（node --test）
 * 覆盖：store 的 TTL/失效/在途去重，api 的重试/不重试/401 刷新重放/并发去重/超时，sse 的续传语义。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

const root = path.join(__dirname, '..');

function memoryStorage() {
    const map = new Map();
    return {
        getItem: k => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => map.set(k, String(v)),
        removeItem: k => map.delete(k),
        key: i => Array.from(map.keys())[i] ?? null,
        get length() { return map.size; },
        _map: map,
    };
}

function loadStore(persist) {
    const storage = memoryStorage();
    const ctx = { console, Promise, Date, JSON, Object, sessionStorage: storage };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'frontend/shared/store.js'), 'utf8'), ctx);
    return { ctx, storage, Store: ctx.WBStoreClass };
}

test('store 在 TTL 内复用缓存，失效后重新加载', async () => {
    const { Store } = loadStore();
    const store = new Store();
    let calls = 0;
    const loader = () => Promise.resolve(++calls);
    assert.equal(await store.remember('k', loader, 1000), 1);
    assert.equal(await store.remember('k', loader, 1000), 1);
    assert.equal(calls, 1);
    store.invalidate('k');
    assert.equal(await store.remember('k', loader, 1000), 2);
    assert.equal(calls, 2);
});

test('store 合并并发加载，避免同一 key 重复请求', async () => {
    const { Store } = loadStore();
    const store = new Store();
    let calls = 0;
    const loader = () => new Promise(resolve => setTimeout(() => resolve(++calls), 20));
    const results = await Promise.all([store.remember('k', loader), store.remember('k', loader)]);
    assert.deepEqual(results, [1, 1]);
    assert.equal(calls, 1);
});

test('store 过期后不再返回旧值，且 sessionStorage 持久化可恢复', async () => {
    const { Store, storage } = loadStore();
    const store = new Store({ persist: true, prefix: 'p:' });
    store.set('a', { v: 1 }, -1);
    assert.equal(store.get('a'), undefined);
    store.set('b', { v: 2 }, 5000);
    assert.equal(storage.getItem('p:b') !== null, true);
    const restored = new Store({ persist: true, prefix: 'p:' });
    assert.deepEqual(restored.get('b'), { v: 2 });
});

function loadApi(fetchImpl, opts = {}) {
    const storage = memoryStorage();
    storage.setItem('token', 'old-token');
    storage.setItem('refresh_token', 'refresh-1');
    const location = { pathname: '/client', hash: '', href: '' };
    const ctx = {
        console, Promise, Date, JSON, Object, Math, setTimeout, clearTimeout,
        fetch: fetchImpl, AbortController, TextDecoder, FormData, sessionStorage: storage, localStorage: memoryStorage(),
        location, URL, encodeURIComponent, confirm: opts.confirm,
        APP_CONFIG: Object.assign({
            api: { timeout: 200, longTimeout: 400, maxRetries: 2, retryBaseDelay: 5, retryMaxDelay: 20, refreshPath: '/api/auth/refresh' },
            routes: { login: '/login' },
        }, opts.config || {}),
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'frontend/shared/api.js'), 'utf8'), ctx);
    return { ctx, storage, location, Api: ctx.WBApiClass };
}

function jsonResponse(body, status = 200, headers = {}) {
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: { get: name => headers[name] || null },
        text: () => Promise.resolve(JSON.stringify(body)),
        json: () => Promise.resolve(body),
    };
}

test('GET 在 5xx 时按指数退避重试，最终成功', async () => {
    let calls = 0;
    const { Api } = loadApi(async () => {
        calls++;
        if (calls < 3) return jsonResponse({ detail: 'busy' }, 503);
        return jsonResponse({ success: true, data: 42 });
    });
    const res = await new Api().get('/api/client/dashboard');
    assert.equal(res.data, 42);
    assert.equal(calls, 3);
});

test('POST 不自动重试，错误标记为可重试且带状态码', async () => {
    let calls = 0;
    const { Api } = loadApi(async () => { calls++; return jsonResponse({ detail: '服务繁忙' }, 503); });
    await assert.rejects(new Api().post('/api/agent/workflow/start', { a: 1 }), err => {
        assert.equal(err.message, '服务繁忙');
        assert.equal(err.status, 503);
        assert.equal(err.retryable, true);
        assert.ok(err.requestId.startsWith('req_'));
        return true;
    });
    assert.equal(calls, 1);
});

test('401 触发一次 refresh 并重放原请求，新 token 生效', async () => {
    const seen = [];
    const { Api, storage } = loadApi(async (url, init) => {
        seen.push({ url, auth: init.headers.Authorization });
        if (url.indexOf('/api/auth/refresh') === 0) {
            return jsonResponse({ success: true, data: { access_token: 'new-token' } });
        }
        if (init.headers.Authorization === 'Bearer old-token') return jsonResponse({ detail: '过期' }, 401);
        return jsonResponse({ success: true, data: 'ok' });
    });
    const res = await new Api().get('/api/client/me');
    assert.equal(res.data, 'ok');
    assert.equal(seen.length, 3);
    assert.equal(seen[1].auth, undefined);              // refresh 请求不带旧 token
    assert.equal(seen[2].auth, 'Bearer new-token');     // 重放使用新 token
    assert.equal(storage.getItem('token'), 'new-token');
});

test('refresh 失败则判定认证失败并跳转登录', async () => {
    const { Api, location } = loadApi(async (url) => {
        if (url.indexOf('/api/auth/refresh') === 0) return jsonResponse({ detail: '无效' }, 401);
        return jsonResponse({ detail: '过期' }, 401);
    });
    const api = new Api();
    await assert.rejects(api.get('/api/client/me'), err => err.code === 'auth');
    assert.ok(location.href.indexOf('/login?redirect=') === 0);
});

/* 批次 24：首登未改密返回 403 + X-Password-Change-Required。
 * 它和「登录失效」都是 403，若不加区分就会清 token 把人踢回登录页，
 * 而用户还要用这个 token 去调改密接口 —— 于是永远进不去，形成死循环。 */
test('403 带强制改密标记 → 跳改密面板且保留 token', async () => {
    let authFailed = 0, pwdRequired = 0;
    const { Api, storage, location } = loadApi(
        async () => jsonResponse({ success: false, detail: '首次登录请先修改初始密码' }, 403,
            { 'X-Password-Change-Required': '1' }),
        { config: { routes: { login: '/login', client: '/client' } } });
    const api = new Api({
        onAuthFailure: () => { authFailed++; },
        onPasswordChangeRequired: () => { pwdRequired++; },
    });

    await assert.rejects(api.get('/api/client/reports'),
        err => err.code === 'password_change_required' && err.passwordChangeRequired === true);

    assert.equal(authFailed, 0, '不能当成登录失效（会清 token）');
    assert.equal(pwdRequired, 1, '要触发改密跳转');
    assert.equal(storage.getItem('token'), 'old-token', 'token 必须留着——还要拿它调改密接口');
    assert.equal(location.href, '', '不该跳登录页');
});

test('403 带强制改密标记但用默认回调 → 落到设置页且不清 token', async () => {
    const { Api, storage, location } = loadApi(
        async () => jsonResponse({ detail: '首次登录请先修改初始密码' }, 403,
            { 'X-Password-Change-Required': '1' }),
        { config: { routes: { login: '/login', client: '/client' } } });

    await assert.rejects(new Api().get('/api/client/reports'),
        err => err.code === 'password_change_required');

    assert.ok(location.hash === '#settings' || location.href.indexOf('settings') >= 0,
        `应落到改密面板，实际 hash=${location.hash} href=${location.href}`);
    assert.equal(storage.getItem('token'), 'old-token', '默认回调不能清 token');
    assert.equal(location.href.indexOf('/login'), -1, '不能被送去登录页');
});

test('普通 403（无标记头）仍按认证失败处理', async () => {
    let authFailed = 0;
    const { Api } = loadApi(async () => jsonResponse({ detail: '需要管理员权限' }, 403));
    const api = new Api({ onAuthFailure: () => { authFailed++; } });
    await assert.rejects(api.get('/api/admin/users'), err => err.code === 'auth');
    assert.equal(authFailed, 1);
});

test('相同 URL 的并发 GET 只发一次请求', async () => {    let calls = 0;
    const { Api } = loadApi(() => new Promise(resolve => {
        calls++;
        setTimeout(() => resolve(jsonResponse({ success: true, data: calls })), 20);
    }));
    const api = new Api();
    const [a, b] = await Promise.all([api.get('/api/client/reports'), api.get('/api/client/reports')]);
    assert.equal(calls, 1);
    assert.deepEqual(a, b);
});

test('超时产生可重试错误，且不会无限等待', async () => {
    const { Api } = loadApi(() => new Promise(resolve => setTimeout(() => resolve(jsonResponse({})), 500)));
    await assert.rejects(new Api().get('/api/client/reports', { retries: 0 }), err => {
        assert.equal(err.code, 'timeout');
        assert.equal(err.retryable, true);
        return true;
    });
});

test('4xx 终端错误不重试', async () => {
    let calls = 0;
    const { Api } = loadApi(async () => { calls++; return jsonResponse({ detail: '没找到' }, 404); });
    await assert.rejects(new Api().get('/api/nope'), err => {
        assert.equal(err.code, 'http_404');
        assert.equal(err.retryable, false);
        return true;
    });
    assert.equal(calls, 1);
});

/* 导出闸门（批次 26）：后端用 409 + X-Export-Blocked 表示「报告还不能导出」。
   前端必须问一次再带 force 重放，否则缺一章的会话彻底导不出去。 */

test('导出被拦截时先询问，确认后带 force 重放一次', async () => {
    const bodies = [];
    const { Api } = loadApi(async (url, init) => {
        bodies.push(JSON.parse(init.body));
        if (bodies.length === 1) {
            return jsonResponse({ detail: '报告还不能导出：REPORT_CHAPTER_MISSING#1 报告有 1 章还是占位符' },
                409, { 'X-Export-Blocked': '1' });
        }
        return jsonResponse({ success: true, data: { file_name: 'a.docx' } });
    }, { confirm: () => true });

    const res = await new Api().post('/api/agent/workflow/export', { session_id: 's1', format: 'word' });

    assert.equal(res.data.file_name, 'a.docx');
    assert.equal(bodies.length, 2);
    assert.equal(bodies[0].force, undefined);
    assert.equal(bodies[1].force, true);
    assert.equal(bodies[1].session_id, 's1');
});

test('用户取消时如实抛出被拦原因，且不重放', async () => {
    let calls = 0;
    const { Api } = loadApi(async () => {
        calls++;
        return jsonResponse({ detail: '报告还不能导出：缺 1 章' }, 409, { 'X-Export-Blocked': '1' });
    }, { confirm: () => false });

    await assert.rejects(new Api().post('/api/agent/workflow/export', { session_id: 's1' }), err => {
        assert.equal(err.status, 409);
        assert.match(err.message, /缺 1 章/);
        return true;
    });
    assert.equal(calls, 1);
});

test('普通 409（无导出标记）不会被当成导出闸门反复询问', async () => {
    let calls = 0;
    let asked = 0;
    const { Api } = loadApi(async () => {
        calls++;
        return jsonResponse({ detail: '工作流正在执行' }, 409);
    }, { confirm: () => { asked++; return true; } });

    await assert.rejects(new Api().post('/api/agent/workflow/export', { session_id: 's1' }),
        err => err.status === 409);
    assert.equal(calls, 1);
    assert.equal(asked, 0);
});
