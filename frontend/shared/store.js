/* 云海睿评 · 极简状态容器
 * 目的：模块切换时不再「每次都重新拉全量」。
 * 能力：内存缓存 + TTL 复用 + 失效 + 在途去重（与 api 层配合）+ 可选 sessionStorage 持久化。
 */
(function (global) {
    'use strict';

    var CFG = global.APP_CONFIG || {};
    var DEFAULT_TTL = (CFG.cache && CFG.cache.defaultTTL) || 30000;

    function now() { return Date.now(); }

    function Store(options) {
        this._memory = Object.create(null);   // key -> {value, expireAt, persisted}
        this._inflight = Object.create(null); // key -> Promise
        this._persist = !!(options && options.persist);
        this._prefix = (options && options.prefix) || 'wb_store:';
        if (this._persist) this._restore();
    }

    Store.prototype._restore = function () {
        try {
            for (var i = 0; i < sessionStorage.length; i++) {
                var full = sessionStorage.key(i);
                if (full.indexOf(this._prefix) !== 0) continue;
                var raw = sessionStorage.getItem(full);
                if (!raw) continue;
                var rec = JSON.parse(raw);
                if (rec && rec.expireAt > now()) {
                    this._memory[full.slice(this._prefix.length)] = {
                        value: rec.value, expireAt: rec.expireAt, persisted: true
                    };
                } else {
                    sessionStorage.removeItem(full);
                }
            }
        } catch (e) { /* sessionStorage 不可用时静默降级为纯内存 */ }
    };

    Store.prototype.get = function (key) {
        var rec = this._memory[key];
        if (!rec) return undefined;
        if (rec.expireAt <= now()) {
            this.invalidate(key);
            return undefined;
        }
        return rec.value;
    };

    Store.prototype.has = function (key) {
        return this.get(key) !== undefined;
    };

    Store.prototype.set = function (key, value, ttl) {
        var expireAt = now() + (typeof ttl === 'number' ? ttl : DEFAULT_TTL);
        var persisted = false;
        if (this._persist) {
            try {
                sessionStorage.setItem(this._prefix + key, JSON.stringify({ value: value, expireAt: expireAt }));
                persisted = true;
            } catch (e) { persisted = false; }
        }
        this._memory[key] = { value: value, expireAt: expireAt, persisted: persisted };
        return value;
    };

    Store.prototype.invalidate = function (key) {
        if (key === undefined) {
            this._memory = Object.create(null);
            this._clearPersisted();
            return;
        }
        delete this._memory[key];
        if (this._persist) {
            try { sessionStorage.removeItem(this._prefix + key); } catch (e) {}
        }
    };

    /* 按前缀批量失效，例如 invalidatePrefix('reports') 命中 reports、reports:page2 */
    Store.prototype.invalidatePrefix = function (prefix) {
        var self = this;
        Object.keys(this._memory).forEach(function (k) {
            if (k === prefix || k.indexOf(prefix + ':') === 0) self.invalidate(k);
        });
    };

    Store.prototype._clearPersisted = function () {
        if (!this._persist) return;
        try {
            var keys = [];
            for (var i = 0; i < sessionStorage.length; i++) {
                var k = sessionStorage.key(i);
                if (k && k.indexOf(this._prefix) === 0) keys.push(k);
            }
            keys.forEach(function (k) { sessionStorage.removeItem(k); });
        } catch (e) {}
    };

    /* 读取或加载：命中缓存直接返回；未命中时复用同一在途 Promise，避免并发重复请求。
     * loader 必须是返回 Promise 的函数。
     */
    Store.prototype.remember = function (key, loader, ttl) {
        var cached = this.get(key);
        if (cached !== undefined) return Promise.resolve(cached);
        if (this._inflight[key]) return this._inflight[key];
        var self = this;
        var p = Promise.resolve()
            .then(loader)
            .then(function (value) {
                self.set(key, value, ttl);
                delete self._inflight[key];
                return value;
            })
            .catch(function (err) {
                delete self._inflight[key];
                throw err;
            });
        this._inflight[key] = p;
        return p;
    };

    Store.prototype.abortInflight = function (key) {
        if (key === undefined) { this._inflight = Object.create(null); return; }
        delete this._inflight[key];
    };

    global.WBStore = new Store({ persist: false });
    global.WBStoreSession = new Store({ persist: true, prefix: 'wb_session:' });
    global.WBStoreClass = Store;
})(window);
