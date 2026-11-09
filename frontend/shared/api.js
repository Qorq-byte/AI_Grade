/* 云海睿评 · 统一请求层
 * 提供：超时 / 指数退避重试（仅幂等 GET）/ 同 URL 并发去重 / 401 自动 refresh 并重放 /
 *      统一错误对象 {code, message, retryable, status, requestId} / 请求 ID 透传。
 */
(function (global) {
    'use strict';

    var CFG = global.APP_CONFIG || {};
    var API_CFG = CFG.api || {};
    var DEFAULT_TIMEOUT = API_CFG.timeout || 30000;
    var LONG_TIMEOUT = API_CFG.longTimeout || 120000;
    var MAX_RETRIES = API_CFG.maxRetries === undefined ? 2 : API_CFG.maxRetries;
    var BASE_DELAY = API_CFG.retryBaseDelay || 400;
    var MAX_DELAY = API_CFG.retryMaxDelay || 4000;

    function newRequestId() {
        var rand = Math.random().toString(36).slice(2, 10);
        return 'req_' + Date.now().toString(36) + '_' + rand;
    }

    var RETRYABLE_STATUS = { 408: 1, 425: 1, 429: 1, 500: 1, 502: 1, 503: 1, 504: 1 };

    function ApiError(message, meta) {
        var err = new Error(message);
        err.name = 'ApiError';
        err.code = (meta && meta.code) || 'unknown';
        err.status = (meta && meta.status) || 0;
        err.retryable = !!(meta && meta.retryable);
        err.requestId = (meta && meta.requestId) || '';
        err.payload = meta && meta.payload;
        return err;
    }

    function readToken() {
        try {
            return sessionStorage.getItem('token') || localStorage.getItem('token') || '';
        } catch (e) { return ''; }
    }

    function readRefreshToken() {
        try {
            return sessionStorage.getItem('refresh_token') || localStorage.getItem('refresh_token') || '';
        } catch (e) { return ''; }
    }

    function writeAccessToken(token) {
        try {
            if (sessionStorage.getItem('token') !== null) sessionStorage.setItem('token', token);
            else if (localStorage.getItem('token') !== null) localStorage.setItem('token', token);
            else sessionStorage.setItem('token', token);
        } catch (e) {}
    }

    function clearAuth() {
        try {
            ['token', 'refresh_token', 'user', 'auth_token'].forEach(function (k) {
                sessionStorage.removeItem(k);
                localStorage.removeItem(k);
            });
        } catch (e) {}
    }

    function redirectToLogin() {
        if (location.pathname === CFG.routes?.login || location.pathname === '/login') return;
        var target = (CFG.routes && CFG.routes.login) || '/login';
        location.href = target + '?redirect=' + encodeURIComponent(location.pathname + location.hash);
    }

    /* 首登未改密：后端用 403 + 该响应头表示。不能走 401 的「清 token 跳登录」，
       否则用户改不了密、也进不了系统，被死循环踢出。 */
    var PWD_CHANGE_HEADER = 'X-Password-Change-Required';

    function isPasswordChangeRequired(response) {
        try { return response.headers.get(PWD_CHANGE_HEADER) === '1'; } catch (e) { return false; }
    }

    function redirectToChangePassword() {
        var target = (CFG.routes && CFG.routes.client) || '/client/';
        if (location.pathname.indexOf('/client') === 0) {
            location.hash = '#settings';
            return;
        }
        location.href = target + '#settings';
    }

    function delay(ms) {
        return new Promise(function (r) { setTimeout(r, ms); });
    }

    function backoff(attempt) {
        var base = Math.min(BASE_DELAY * Math.pow(2, attempt), MAX_DELAY);
        return base + Math.floor(Math.random() * 120);
    }

    function ApiClient(options) {
        options = options || {};
        this.getToken = options.getToken || readToken;
        this.getRefreshToken = options.getRefreshToken || readRefreshToken;
        this.onAuthFailure = options.onAuthFailure || function () { clearAuth(); redirectToLogin(); };
        /* 被强制改密时只跳转、不清 token —— 用户还要用这个 token 去调改密接口 */
        this.onPasswordChangeRequired = options.onPasswordChangeRequired || redirectToChangePassword;
        this._refreshing = null;
        this._inflight = Object.create(null); // key -> Promise（并发去重）
        this.metrics = { total: 0, failed: 0, retried: 0, refreshed: 0, deduped: 0 };
    }

    /* 核心请求。opts: {timeout, long, retries, dedupe, signal, headers, raw} */
    ApiClient.prototype.request = function (method, url, body, opts) {
        var self = this;
        opts = opts || {};
        var upper = String(method || 'GET').toUpperCase();
        var isGet = upper === 'GET' || upper === 'HEAD';
        var timeout = opts.timeout || (opts.long ? LONG_TIMEOUT : DEFAULT_TIMEOUT);
        var maxRetries = isGet ? (opts.retries === undefined ? MAX_RETRIES : opts.retries) : 0;
        var dedupe = opts.dedupe === undefined ? isGet : !!opts.dedupe;
        var dedupeKey = upper + ' ' + url + ' ' + (body ? JSON.stringify(body) : '');

        if (dedupe && this._inflight[dedupeKey]) {
            this.metrics.deduped++;
            return this._inflight[dedupeKey];
        }

        var run = (function attempt(attemptNo, authRetried) {
            return self._send(upper, url, body, timeout, opts).catch(function (err) {
                if (err && err.name === 'AbortError') {
                    throw ApiError('请求已取消', { code: 'abort', retryable: false, requestId: err.requestId });
                }
                if (err && err.code === 'auth' && !authRetried) {
                    return self._refresh().then(function (ok) {
                        if (!ok) { self.onAuthFailure(); throw err; }
                        return attempt(attemptNo, true);
                    });
                }
                var canRetry = err && err.retryable && attemptNo < maxRetries;
                if (!canRetry) throw err;
                self.metrics.retried++;
                return delay(backoff(attemptNo)).then(function () { return attempt(attemptNo + 1, authRetried); });
            });
        })(0, false);

        this.metrics.total++;
        if (dedupe) {
            this._inflight[dedupeKey] = run;
            var cleanup = function () { delete self._inflight[dedupeKey]; };
            run.then(cleanup, cleanup);
        }
        run.catch(function () { self.metrics.failed++; });
        return run;
    };

    ApiClient.prototype._send = function (method, url, body, timeout, opts) {
        var self = this;
        var requestId = newRequestId();
        var headers = Object.assign({}, opts.headers || {});
        var token = this.getToken();
        if (token) headers['Authorization'] = 'Bearer ' + token;
        if (body !== undefined && body !== null && !(body instanceof FormData)) {
            headers['Content-Type'] = headers['Content-Type'] || 'application/json';
        }
        headers['X-Request-ID'] = requestId;

        var controller = new AbortController();
        var timedOut = false;
        var timer = null;
        var timeoutReject = null;
        /* 超时用独立的 deferred 判定，不依赖 fetch 是否响应 abort */
        var timeoutPromise = new Promise(function (_, reject) {
            timeoutReject = reject;
            timer = setTimeout(function () {
                timedOut = true;
                controller.abort();
                reject(ApiError('请求超时（' + Math.round(timeout / 1000) + ' 秒），请稍后重试', {
                    code: 'timeout', retryable: true, requestId: requestId
                }));
            }, timeout);
        });
        if (opts.signal) {
            if (opts.signal.aborted) controller.abort();
            else opts.signal.addEventListener('abort', function () { controller.abort(); }, { once: true });
        }

        var init = { method: method, headers: headers, signal: controller.signal };
        if (body !== undefined && body !== null) {
            init.body = body instanceof FormData ? body : JSON.stringify(body);
        }

        var fetchPromise = fetch(url, init).then(function (response) {
            if (response.status === 401 || response.status === 403) {
                /* 先判「首登未改密」：它是 403，但语义不是登录失效 */
                if (response.status === 403 && isPasswordChangeRequired(response)) {
                    return response.json().catch(function () { return {}; }).then(function (payload) {
                        var err = ApiError((payload && payload.detail) || '首次登录请先修改初始密码', {
                            code: 'password_change_required', status: 403, retryable: false,
                            requestId: requestId, payload: payload
                        });
                        err.passwordChangeRequired = true;
                        if (self.onPasswordChangeRequired) self.onPasswordChangeRequired(err);
                        throw err;
                    });
                }
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    throw ApiError((payload && payload.detail) || '登录状态已失效', {
                        code: 'auth', status: response.status, retryable: false, requestId: requestId, payload: payload
                    });
                });
            }
            if (opts.raw) {
                if (!response.ok) {
                    throw ApiError('请求失败（HTTP ' + response.status + '）', {
                        code: 'http_' + response.status, status: response.status,
                        retryable: !!RETRYABLE_STATUS[response.status], requestId: requestId
                    });
                }
                return response;
            }
            return response.text().then(function (text) {
                var data = null;
                if (text) {
                    try { data = JSON.parse(text); } catch (e) { data = { detail: text }; }
                }
                if (!response.ok) {
                    var message = (data && (data.detail || data.message)) || ('请求失败：' + response.status);
                    throw ApiError(typeof message === 'string' ? message : JSON.stringify(message), {
                        code: 'http_' + response.status,
                        status: response.status,
                        retryable: !!RETRYABLE_STATUS[response.status],
                        requestId: response.headers.get('X-Request-ID') || requestId,
                        payload: data
                    });
                }
                var body2 = data || {};
                if (body2 && typeof body2 === 'object' && body2.request_id === undefined) {
                    body2.request_id = response.headers.get('X-Request-ID') || requestId;
                }
                return body2;
            });
        });
        /* 超时后 fetch 仍可能自行拒绝，这里吸收掉，避免未处理拒绝 */
        fetchPromise.catch(function () {});

        return Promise.race([fetchPromise, timeoutPromise]).then(function (value) {
            clearTimeout(timer);
            return value;
        }, function (err) {
            clearTimeout(timer);
            if (err && err.name === 'ApiError') throw err;
            if (timedOut) {
                throw ApiError('请求超时（' + Math.round(timeout / 1000) + ' 秒），请稍后重试', {
                    code: 'timeout', retryable: true, requestId: requestId
                });
            }
            if (err && err.name === 'AbortError') {
                throw ApiError('请求已取消', { code: 'abort', retryable: false, requestId: requestId });
            }
            throw ApiError('网络连接失败，请检查网络', {
                code: 'network', retryable: true, requestId: requestId
            });
        });
    };

    /* 401 后刷新令牌：多个并发 401 只触发一次刷新 */
    ApiClient.prototype._refresh = function () {
        var self = this;
        if (this._refreshing) return this._refreshing;
        var refreshToken = this.getRefreshToken();
        if (!refreshToken) return Promise.resolve(false);
        this._refreshing = fetch(API_CFG.refreshPath + '?refresh_token_str=' + encodeURIComponent(refreshToken), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }).then(function (res) {
            if (!res.ok) return false;
            return res.json().then(function (data) {
                var token = data && data.data && data.data.access_token;
                if (!token) return false;
                writeAccessToken(token);
                self.metrics.refreshed++;
                return true;
            });
        }).catch(function () { return false; }).then(function (ok) {
            self._refreshing = null;
            return ok;
        });
        return this._refreshing;
    };

    ApiClient.prototype.get = function (url, opts) { return this.request('GET', url, null, opts); };
    ApiClient.prototype.post = function (url, body, opts) { return this.request('POST', url, body, opts); };
    ApiClient.prototype.put = function (url, body, opts) { return this.request('PUT', url, body, opts); };
    ApiClient.prototype.del = function (url, opts) { return this.request('DELETE', url, null, opts); };
    ApiClient.prototype.upload = function (url, formData, opts) {
        return this.request('POST', url, formData, Object.assign({ long: true }, opts || {}));
    };
    ApiClient.prototype.blob = function (url, opts) {
        return this.request('GET', url, null, Object.assign({ raw: true, long: true }, opts || {}));
    };

    global.WBApiClass = ApiClient;
    global.WBApi = new ApiClient();
})(window);
