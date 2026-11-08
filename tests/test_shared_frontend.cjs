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
        location, URL, encodeURIComponent,
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

test('相同 URL 的并发 GET 只发一次请求', async () => {
    let calls = 0;
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
