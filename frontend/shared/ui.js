/* 云海睿评 · 共享 UI 原子能力
 * 目标：替换各页面散落的 innerHTML 拼接、原生 confirm()、无 aria 提示。
 * 全部零依赖，样式内联注入一次，页面无需引入额外 CSS。
 */
(function (global) {
    'use strict';

    var STYLE_ID = 'wb-ui-style';
    var TOAST_HOST_ID = 'wb-toast-host';
    var LIVE_ID = 'wb-live-region';

    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var css = ''
            + '#wb-toast-host{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:360px}'
            + '.wb-toast{padding:12px 16px;border-radius:8px;color:#fff;font-size:14px;line-height:1.5;'
            + 'box-shadow:0 6px 18px rgba(16,24,40,.18);background:#4a5568;animation:wbToastIn .22s ease;word-break:break-word}'
            + '.wb-toast.success{background:#38a169}.wb-toast.error{background:#e53e3e}'
            + '.wb-toast.warn{background:#dd6b20}.wb-toast.info{background:#3182ce}'
            + '@keyframes wbToastIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}'
            + '.wb-sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;'
            + 'clip:rect(0,0,0,0);white-space:nowrap;border:0}'
            + '.wb-skeleton{display:flex;flex-direction:column;gap:10px;padding:8px 0}'
            + '.wb-skeleton-row{height:14px;border-radius:6px;background:linear-gradient(90deg,#edf2f7 25%,#e2e8f0 37%,#edf2f7 63%);'
            + 'background-size:400% 100%;animation:wbShimmer 1.4s ease infinite}'
            + '@keyframes wbShimmer{0%{background-position:100% 50%}100%{background-position:0 50%}}'
            + '.wb-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;'
            + 'padding:40px 16px;text-align:center;color:#718096}'
            + '.wb-empty-title{font-size:15px;font-weight:600;color:#2d3748}'
            + '.wb-empty-desc{font-size:13px;color:#a0aec0;max-width:420px;line-height:1.6}'
            + '.wb-modal-mask{position:fixed;inset:0;background:rgba(26,32,44,.45);display:flex;align-items:center;'
            + 'justify-content:center;z-index:10000;padding:16px}'
            + '.wb-modal{background:#fff;border-radius:12px;max-width:440px;width:100%;padding:20px 22px;'
            + 'box-shadow:0 20px 50px rgba(16,24,40,.25);color:#1a202c}'
            + '.wb-modal-title{font-size:16px;font-weight:600;margin:0 0 8px}'
            + '.wb-modal-body{font-size:14px;line-height:1.7;color:#4a5568;margin-bottom:18px;white-space:pre-wrap}'
            + '.wb-modal-actions{display:flex;justify-content:flex-end;gap:10px}'
            + '.wb-btn{padding:8px 16px;border-radius:8px;font-size:14px;cursor:pointer;border:1px solid #e2e8f0;'
            + 'background:#fff;color:#4a5568;font-family:inherit}'
            + '.wb-btn:focus-visible{outline:2px solid #667eea;outline-offset:2px}'
            + '.wb-btn-primary{background:#667eea;border-color:#667eea;color:#fff}'
            + '.wb-btn-danger{background:#e53e3e;border-color:#e53e3e;color:#fff}'
            + '@media (prefers-reduced-motion: reduce){.wb-toast,.wb-skeleton-row{animation:none}}';
        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = css;
        (document.head || document.documentElement).appendChild(style);
    }

    function ensureToastHost() {
        ensureStyle();
        var host = document.getElementById(TOAST_HOST_ID);
        if (!host) {
            host = document.createElement('div');
            host.id = TOAST_HOST_ID;
            host.setAttribute('role', 'status');
            host.setAttribute('aria-live', 'polite');
            document.body.appendChild(host);
        }
        return host;
    }

    function esc(text) {
        var d = document.createElement('div');
        d.textContent = text === undefined || text === null ? '' : String(text);
        return d.innerHTML;
    }

    function showToast(msg, type, options) {
        if (msg === undefined || msg === null || msg === '') return;
        var host = ensureToastHost();
        var el = document.createElement('div');
        var kind = type === 'success' || type === 'error' || type === 'warn' || type === 'info' ? type : 'info';
        el.className = 'wb-toast ' + kind;
        el.textContent = String(msg);
        host.appendChild(el);
        var timeout = (options && options.timeout) || (kind === 'error' ? 5000 : 3000);
        setTimeout(function () {
            el.style.transition = 'opacity .2s ease';
            el.style.opacity = '0';
            setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
        }, timeout);
        return el;
    }

    /* 独立 aria-live 区域：用于播报步骤完成等不打断用户的进展 */
    function announce(message) {
        ensureStyle();
        var live = document.getElementById(LIVE_ID);
        if (!live) {
            live = document.createElement('div');
            live.id = LIVE_ID;
            live.className = 'wb-sr-only';
            live.setAttribute('role', 'status');
            live.setAttribute('aria-live', 'polite');
            document.body.appendChild(live);
        }
        live.textContent = '';
        setTimeout(function () { live.textContent = String(message || ''); }, 30);
    }

    /* 骨架屏：只清空容器一次，之后由真实数据替换 */
    function renderSkeleton(target, options) {
        var el = typeof target === 'string' ? document.getElementById(target) : target;
        if (!el) return;
        var rows = (options && options.rows) || 4;
        var html = '<div class="wb-skeleton" aria-busy="true" aria-label="加载中">';
        for (var i = 0; i < rows; i++) {
            var w = 100 - (i % 3) * 12;
            html += '<div class="wb-skeleton-row" style="width:' + w + '%"></div>';
        }
        el.innerHTML = html + '</div>';
    }

    function emptyState(title, desc, actionHtml) {
        return '<div class="wb-empty">'
            + '<div class="wb-empty-title">' + esc(title || '暂无数据') + '</div>'
            + (desc ? '<div class="wb-empty-desc">' + esc(desc) + '</div>' : '')
            + (actionHtml || '')
            + '</div>';
    }

    /* Promise 版确认框，替代原生 confirm()：可键盘操作、可配置危险样式 */
    function confirmDialog(options) {
        var opts = typeof options === 'string' ? { message: options } : (options || {});
        return new Promise(function (resolve) {
            ensureStyle();
            var mask = document.createElement('div');
            mask.className = 'wb-modal-mask';
            var danger = opts.danger !== false;
            mask.innerHTML = ''
                + '<div class="wb-modal" role="dialog" aria-modal="true" aria-labelledby="wbModalTitle">'
                + '<h3 class="wb-modal-title" id="wbModalTitle">' + esc(opts.title || '请确认') + '</h3>'
                + '<div class="wb-modal-body">' + esc(opts.message || '') + '</div>'
                + '<div class="wb-modal-actions">'
                + '<button type="button" class="wb-btn" data-act="cancel">' + esc(opts.cancelText || '取消') + '</button>'
                + '<button type="button" class="wb-btn ' + (danger ? 'wb-btn-danger' : 'wb-btn-primary') + '" data-act="ok">'
                + esc(opts.okText || '确定') + '</button>'
                + '</div></div>';
            document.body.appendChild(mask);
            var okBtn = mask.querySelector('[data-act="ok"]');
            var cancelBtn = mask.querySelector('[data-act="cancel"]');
            var lastFocus = document.activeElement;

            function close(result) {
                document.removeEventListener('keydown', onKey, true);
                if (mask.parentNode) mask.parentNode.removeChild(mask);
                if (lastFocus && lastFocus.focus) lastFocus.focus();
                resolve(result);
            }
            function onKey(e) {
                if (e.key === 'Escape') { e.stopPropagation(); close(false); }
                else if (e.key === 'Tab') {
                    var focusables = [cancelBtn, okBtn];
                    var idx = focusables.indexOf(document.activeElement);
                    e.preventDefault();
                    var next = e.shiftKey ? (idx <= 0 ? focusables.length - 1 : idx - 1)
                        : (idx === focusables.length - 1 ? 0 : idx + 1);
                    focusables[next].focus();
                }
            }
            okBtn.addEventListener('click', function () { close(true); });
            cancelBtn.addEventListener('click', function () { close(false); });
            mask.addEventListener('click', function (e) { if (e.target === mask) close(false); });
            document.addEventListener('keydown', onKey, true);
            okBtn.focus();
        });
    }

    function openModal(el) {
        var node = typeof el === 'string' ? document.getElementById(el) : el;
        if (!node) return;
        node.hidden = false;
        node.setAttribute('aria-hidden', 'false');
        var focusable = node.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (focusable) focusable.focus();
    }

    function closeModal(el) {
        var node = typeof el === 'string' ? document.getElementById(el) : el;
        if (!node) return;
        node.hidden = true;
        node.setAttribute('aria-hidden', 'true');
    }

    /* ── 错误分流 ─────────────────────────────────────
     * 不是所有错误都该长成同一个 Toast：
     *   可重试（超时/断网/5xx/429）→ 黄色告诫 + 「稍后重试」，附请求号便于排查；
     *   终态（400/404/409/422）→ 红色，直接说清用户要改什么；
     *   已取消（abort）→ 静默，切页签/翻页不该弹错误；
     *   登录失效 → 请求层已跳转登录，这里不重复提示。
     */
    var ERROR_HINTS = {
        timeout: '请求超时，可稍后重试',
        network: '网络连接异常，请检查网络后重试',
        busy: '服务繁忙，请稍后重试',
        server: '服务暂时不可用',
        transient: '操作未完成，可稍后重试',
        conflict: '数据已被修改，请刷新后重试',
        notfound: '目标不存在或已被删除',
        invalid: '请检查填写内容后重试',
        auth: '登录状态已失效，请重新登录',
        abort: '',
        unknown: '操作失败'
    };

    var RETRYABLE_KINDS = { timeout: 1, network: 1, busy: 1, server: 1, transient: 1 };
    /* 这些类型的后端 detail 往往偏技术，补一句人话建议 */
    var ADVICE_KINDS = { busy: 1, server: 1, transient: 1, conflict: 1, notfound: 1, invalid: 1, unknown: 1 };

    function classifyApiError(err) {
        var e = err || {};
        var status = e.status || 0;
        var code = e.code || (e.name === 'AbortError' ? 'abort' : '');
        var kind;
        if (code === 'abort') kind = 'abort';
        else if (code === 'auth' || status === 401 || status === 403) kind = 'auth';
        else if (code === 'timeout') kind = 'timeout';
        else if (code === 'network') kind = 'network';
        else if (status === 409) kind = 'conflict';
        else if (status === 404) kind = 'notfound';
        else if (status === 400 || status === 422) kind = 'invalid';
        else if (status === 429) kind = 'busy';
        else if (status >= 500) kind = 'server';
        else if (e.retryable) kind = 'transient';
        else kind = 'unknown';

        var message = (e.message && String(e.message).trim()) || ERROR_HINTS[kind] || '操作失败';
        var retryable = !!RETRYABLE_KINDS[kind];
        var requestId = e.requestId || '';
        return {
            kind: kind,
            message: message,
            hint: ADVICE_KINDS[kind] ? (ERROR_HINTS[kind] || '') : '',
            retryable: retryable,
            status: status,
            requestId: requestId,
            /* 只有「服务端出错」或「说不清原因」时才把请求号摊给用户，避免噪音 */
            showRequestId: !!requestId && (kind === 'server' || kind === 'unknown'
                || kind === 'transient' || kind === 'timeout')
        };
    }

    function showApiError(err, options) {
        var opts = options || {};
        var info = classifyApiError(err);
        if (info.kind === 'abort') return null;
        if (info.kind === 'auth' && opts.skipAuth !== false) return null;

        var text = opts.action ? opts.action + '失败：' + info.message : info.message;
        if (info.hint && info.hint !== info.message) text += '（' + info.hint + '）';
        if (info.showRequestId) text += ' [请求号 ' + info.requestId + ']';

        var el = showToast(text, info.retryable ? 'warn' : 'error', {
            timeout: info.retryable ? 4500 : 6000
        });
        if (el) {
            el.setAttribute('data-error-kind', info.kind);
            el.setAttribute('data-request-id', info.requestId || '');
        }
        return el;
    }

    function debounce(fn, wait) {        var timer = null;
        return function () {
            var args = arguments, self = this;
            if (timer) clearTimeout(timer);
            timer = setTimeout(function () { timer = null; fn.apply(self, args); }, wait);
        };
    }

    function throttle(fn, wait) {
        var last = 0, timer = null, pendingArgs = null;
        return function () {
            var now = Date.now(), self = this;
            pendingArgs = arguments;
            if (now - last >= wait) {
                last = now;
                fn.apply(self, pendingArgs);
            } else if (!timer) {
                timer = setTimeout(function () {
                    timer = null;
                    last = Date.now();
                    fn.apply(self, pendingArgs);
                }, wait - (now - last));
            }
        };
    }

    global.WBUI = {
        esc: esc, showToast: showToast, announce: announce,
        renderSkeleton: renderSkeleton, emptyState: emptyState,
        confirmDialog: confirmDialog, openModal: openModal, closeModal: closeModal,
        debounce: debounce, throttle: throttle,
        classifyApiError: classifyApiError, showApiError: showApiError
    };

    /* 兼容旧调用：页面脚本若未再定义，则直接可用 */
    global.esc = esc;
})(window);
