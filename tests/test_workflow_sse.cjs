const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const { join } = require('node:path');

const source = fs.readFileSync(join(__dirname, '../frontend/shared/workflow-controller.js'), 'utf8');
const sandbox = {
    TextDecoder, TextEncoder, Set, Map, Array, Object, JSON, Math, Promise, String, Number, Date,
    setTimeout, clearTimeout, console, fetch: () => Promise.reject(new Error('no network')),
    crypto: { getRandomValues: arr => arr.fill(1) },
    URL, encodeURIComponent,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const readWorkflowEvents = sandbox.readWorkflowEvents;
const controller = sandbox.WorkflowController;

function response(data) {
    const bytes = new TextEncoder().encode(data);
    const body = new ReadableStream({
        start(controller) {
            for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
            controller.close();
        },
    });
    return new Response(body);
}

test('decodes split UTF-8 and dispatches the terminal event once', async () => {
    const events = [];
    await readWorkflowEvents(response('data: {"event":"step_complete","content":"指标"}\n\ndata: {"event":"complete"}\n\n'),
        event => events.push(event));
    assert.deepEqual(events.map(event => event.event), ['step_complete', 'complete']);
    assert.equal(events[0].content, '指标');
});

test('EOF without a terminal event is a disconnect', async () => {
    await assert.rejects(readWorkflowEvents(response('data: {"event":"step_start"}\n\n'), () => {}), /断开/);
});

test('preserves HTTP errors and accepts approval and failure as terminal', async () => {
    const denied = Response.json({ detail: '无权限' }, { status: 403 });
    await assert.rejects(readWorkflowEvents(denied, () => {}), /无权限/);
    for (const type of ['waiting_approval', 'error']) {
        const seen = [];
        await readWorkflowEvents(response('data: ' + JSON.stringify({ event: type }) + '\n\n'),
            event => seen.push(event.event));
        assert.deepEqual(seen, [type]);
    }
});

test('ignores heartbeats and repeated step events across chunks', async () => {
    const events = [];
    const step = 'data: {"event":"step_start","step_number":2}\r\n\r\n';
    await readWorkflowEvents(response(': connected\r\n\r\n' + step + ': heartbeat\n\n' + step
        + 'data: {"event":"step_complete","step_number":2}\n\ndata: {"event":"complete"}\n\n'),
    event => events.push(event.event));
    assert.deepEqual(events, ['step_start', 'step_complete', 'complete']);
});

test('CSV cells escape quotes and prevent formula interpretation', () => {
    const html = fs.readFileSync(join(__dirname, '../frontend/questionnaire/index.html'), 'utf8');
    const inline = html.match(/<script>([\s\S]+?)<\/script>/)[1];
    const start = inline.indexOf('    function csvCell');
    const end = inline.indexOf('    function exportAsCSV', start);
    const cell = vm.runInNewContext(inline.slice(start, end) + '\ncsvCell');
    assert.equal(cell('含"引号",\n和换行'), '"含""引号"",\n和换行"');
    assert.equal(cell('=1+1'), `"'=1+1"`);
    assert.equal(cell(0), '"0"');
});

test('按 id 去重乱序补发帧，并原样透传 token_delta', async () => {
    const events = [];
    const body = [
        'id: 1\ndata: {"event":"step_start","step_number":1}\n\n',
        'id: 2\ndata: {"event":"token_delta","step_number":1,"delta":"指标"}\n\n',
        'id: 2\ndata: {"event":"token_delta","step_number":1,"delta":"重复"}\n\n',
        'id: 3\ndata: {"event":"complete"}\n\n',
    ].join('');
    await readWorkflowEvents(response(body), event => events.push(event));
    assert.deepEqual(events.map(e => e.event), ['step_start', 'token_delta', 'complete']);
    assert.equal(events[1].delta, '指标');
});

test('token_delta 按步骤合并，flush 时才触达渲染', async () => {
    const flushes = [];
    const renderer = controller.createDeltaRenderer((step, text) => flushes.push([step, text]), 1000);
    renderer.push(3, '构建');
    renderer.push(3, '指标');
    renderer.push(5, '体系');
    assert.equal(flushes.length, 0, '未到 flush 间隔不应渲染');
    renderer.flushNow();
    assert.deepEqual(flushes.sort((a, b) => a[0] - b[0]), [[3, '构建指标'], [5, '体系']]);
});

test('断线后用 Last-Event-ID 续传缺失事件', async () => {
    const calls = [];
    const originalFetch = sandbox.fetch;
    sandbox.fetch = async (url, init) => {
        calls.push({ url, headers: init.headers || {} });
        if (calls.length === 1) {
            return response('id: 1\ndata: {"event":"step_start","step_number":1}\n\n');
        }
        return response('id: 9\ndata: {"event":"complete"}\n\n');
    };
    const seen = [];
    await controller.streamWorkflow({
        url: '/api/agent/workflow/start-stream',
        body: { session_id: 'abc' },
        sessionId: 'abc',
        onEvent: e => seen.push(e.event),
        signal: { aborted: false },
        reconnect: true,
    });
    sandbox.fetch = originalFetch;
    assert.deepEqual(seen, ['step_start', 'complete']);
    assert.equal(calls.length, 2);
    assert.ok(calls[1].url.indexOf('last_event_id=1') > 0, '续传请求应带上最后收到的 id');
    assert.equal(calls[1].headers['Last-Event-ID'], '1');
});
