/* 云海睿评 · 工作流前端控制器
 * client 与 workflow 两个页面共用这一份 SSE 消费与状态观测代码。
 *
 * 能力：
 *  - 解析带 `id: {seq}` 的 SSE 帧，支持 `Last-Event-ID` 续传；
 *  - 注释帧（`: ping {ts}`）作为心跳，用于判断链路存活；
 *  - token_delta 按 50ms 合并后交给渲染回调，避免逐字触发布局；
 *  - 未知事件类型静默忽略，保证老页面不被新事件打断；
 *  - 状态观测优先 SSE，断线后以 5s 轮询兜底。
 */
(function (global) {
    'use strict';

    var CFG = global.APP_CONFIG || {};
    var SSE_CFG = CFG.sse || {};
    var RENDER_INTERVAL = SSE_CFG.renderInterval || 50;
    var HEARTBEAT_TIMEOUT = SSE_CFG.heartbeatTimeout || 45000;

    var TERMINAL = ['complete', 'error', 'waiting_approval'];
    var DEDUPE_TYPES = ['step_start', 'step_complete', 'token_update', 'token_delta'];

    var workflowStatusLabels = (CFG.workflowStatus && CFG.workflowStatus.labels) || {
        running: '执行中', retrying: '重试中', awaiting_approval: '等待人工确认',
        paused: '等待人工确认', completed: '已完成', failed: '失败'
    };
    var workflowSessionKey = 'aiGradeWorkflowSession';

    function newWorkflowSession() {
        var bytes = new Uint8Array(16);
        if (global.crypto && global.crypto.getRandomValues) global.crypto.getRandomValues(bytes);
        else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
        return Array.from(bytes, function (n) { return n.toString(16).padStart(2, '0'); }).join('');
    }

    /* token_delta 合并器：按步骤缓冲，每 interval 毫秒 flush 一次 */
    function createDeltaRenderer(onFlush, interval) {
        var buffers = Object.create(null);
        var timer = null;
        var wait = interval || RENDER_INTERVAL;

        function flush() {
            timer = null;
            var snapshot = buffers;
            buffers = Object.create(null);
            Object.keys(snapshot).forEach(function (step) {
                var text = snapshot[step];
                if (text) onFlush(Number(step), text);
            });
        }

        return {
            push: function (step, delta) {
                if (!delta) return;
                buffers[step] = (buffers[step] || '') + delta;
                if (!timer) timer = setTimeout(flush, wait);
            },
            flushNow: function () {
                if (timer) { clearTimeout(timer); timer = null; }
                flush();
            },
            reset: function (step) {
                if (step === undefined) { buffers = Object.create(null); return; }
                delete buffers[step];
            }
        };
    }

    /* 逐行解析 SSE，返回是否遇到终止事件 */
    async function readWorkflowEvents(response, onEvent, options) {
        options = options || {};
        if (!response.ok) {
            var error = await response.json().catch(function () { return null; });
            throw new Error((error && error.detail) || 'HTTP ' + response.status);
        }

        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var seen = options.seen || new Set();
        var state = { terminal: false, lastEventId: options.lastEventId || 0, heartbeatAt: Date.now() };

        function handleFrame(frame) {
            var eventId = 0;
            var dataLines = [];
            var lines = frame.split('\n');
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i];
                if (!line || line.charAt(0) === ':') continue;
                if (line.indexOf('id:') === 0) eventId = parseInt(line.slice(3).trim(), 10) || 0;
                else if (line.indexOf('data:') === 0) dataLines.push(line.slice(5).replace(/^ /, ''));
            }
            state.heartbeatAt = Date.now();
            if (!dataLines.length) return;
            var event;
            try { event = JSON.parse(dataLines.join('\n')); } catch (e) { return; }
            if (eventId) {
                if (eventId <= state.lastEventId) return; /* 补发时的重复帧 */
                state.lastEventId = eventId;
            }
            if (DEDUPE_TYPES.indexOf(event.event) >= 0 && event.event !== 'token_delta') {
                var key = [event.event, event.step_number, event.version == null ? 1 : event.version].join(':');
                if (seen.has(key)) return;
                seen.add(key);
            }
            onEvent(event);
            if (TERMINAL.indexOf(event.event) >= 0) state.terminal = true;
        }

        try {
            while (true) {
                var chunk = await reader.read();
                buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
                /* 统一换行，兼容 \r\n\r\n 分帧 */
                buffer = buffer.replace(/\r\n/g, '\n');
                var frames = buffer.split('\n\n');
                buffer = chunk.done ? '' : frames.pop();
                for (var f = 0; f < frames.length; f++) {
                    if (frames[f].trim()) handleFrame(frames[f].replace(/\r/g, ''));
                }
                if (chunk.done) break;
            }
            if (buffer.trim()) handleFrame(buffer.replace(/\r/g, ''));
        } finally {
            try { reader.releaseLock(); } catch (e) {}
        }
        return state;
    }

    /* 启动工作流并消费事件；断线后自动用 Last-Event-ID 续传一次 */
    async function streamWorkflow(options) {
        var url = options.url;
        var body = options.body;
        var onEvent = options.onEvent;
        var signal = options.signal;
        var reconnect = options.reconnect !== false;
        var seen = new Set();
        var lastEventId = 0;
        var emittedTerminal = false;

        async function consume(response) {
            var state = await readWorkflowEvents(response, function (event) {
                if (TERMINAL.indexOf(event.event) >= 0) emittedTerminal = true;
                onEvent(event);
            }, { seen: seen, lastEventId: lastEventId });
            lastEventId = state.lastEventId || lastEventId;
            return state;
        }

        var response = await fetch(url, {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, options.headers || {}),
            body: JSON.stringify(body),
            signal: signal
        });
        var state = await consume(response);
        if (state.terminal || emittedTerminal || !reconnect) return;

        /* 断线：用 Last-Event-ID 续传最近窗口 */
        var sessionId = options.sessionId;
        if (!sessionId) return;
        for (var attempt = 0; attempt < 2; attempt++) {
            await new Promise(function (r) { setTimeout(r, 800 * (attempt + 1)); });
            if (signal && signal.aborted) return;
            var headers = Object.assign({ Accept: 'text/event-stream' }, options.headers || {});
            if (lastEventId) headers['Last-Event-ID'] = String(lastEventId);
            var resumed = await fetch('/api/agent/workflow/' + encodeURIComponent(sessionId)
                + '/stream?last_event_id=' + lastEventId, { headers: headers, signal: signal });
            if (!resumed.ok) return;
            var s2 = await consume(resumed);
            lastEventId = s2.lastEventId || lastEventId;
            if (s2.terminal || emittedTerminal) return;
        }
    }

    /* 状态观测：SSE 优先，断线后 5s 轮询兜底 */
    async function watchWorkflowStatus(sessionId, request, onStatus, onDisconnect, signal, options) {
        options = options || {};
        var interval = options.interval || (CFG.poll && CFG.poll.statusFallback) || 5000;
        var afterStep = 0;
        while (!signal.aborted) {
            try {
                var response = await request('/api/agent/workflow/' + encodeURIComponent(sessionId)
                    + '/status?after_step=' + afterStep, { signal: signal });
                if (signal.aborted) return;
                if ([401, 403, 404].includes(response.status)) {
                    onStatus({ status: 'not_found' });
                    return;
                }
                if (!response.ok) throw new Error('状态请求失败');
                var payload = await response.json();
                if (signal.aborted) return;
                onStatus(payload.data);
                afterStep = Math.max(afterStep, Math.max.apply(null, payload.data.completed_steps.concat([0])));
                if (!['running', 'retrying'].includes(payload.data.status)) return;
            } catch (error) {
                if (signal.aborted) return;
                onDisconnect();
            }
            await new Promise(function (resolve) { setTimeout(resolve, interval); });
        }
    }

    global.WorkflowController = {
        readWorkflowEvents: readWorkflowEvents,
        streamWorkflow: streamWorkflow,
        watchWorkflowStatus: watchWorkflowStatus,
        createDeltaRenderer: createDeltaRenderer,
        newWorkflowSession: newWorkflowSession,
        workflowStatusLabels: workflowStatusLabels,
        workflowSessionKey: workflowSessionKey,
        HEARTBEAT_TIMEOUT: HEARTBEAT_TIMEOUT
    };

    /* 兼容旧页面直接引用的全局函数 */
    global.readWorkflowEvents = function (response, onEvent, options) {
        return readWorkflowEvents(response, onEvent, options).then(function (state) {
            if (!state.terminal) throw new Error('工作流连接已断开，请在「我的报告」查看执行状态');
            return state;
        });
    };
    global.watchWorkflowStatus = watchWorkflowStatus;
    global.newWorkflowSession = newWorkflowSession;
    global.workflowStatusLabels = workflowStatusLabels;
    global.workflowSessionKey = workflowSessionKey;
})(window);
