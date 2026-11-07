const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const { join } = require('node:path');

const source = fs.readFileSync(join(__dirname, '../frontend/workflow/sse.js'), 'utf8');
const readWorkflowEvents = vm.runInNewContext(source + '\nreadWorkflowEvents', { TextDecoder });

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
