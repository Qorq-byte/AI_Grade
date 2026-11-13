const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const { join } = require('node:path');

const source = fs.readFileSync(join(__dirname, '../frontend/shared/workflow-steps.js'), 'utf8');
const sandbox = { Set, Map, Array, Object, Number, String, Math, console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const steps = sandbox.WorkflowSteps;

const TOTAL = 14;

/* ── 回归：轮询不能把「正在运行」抹成「未开始」 ──────────────────
 * 用户报的现象：运行中点别的步骤、再点回来，运行中的那一行变成 ⏳，
 * 后面一串也都显示未运行，且看不到实时内容。
 * 根因是 renderStepList() 只认 completed/pending，而 pollStatus 每 5s 全量重画。
 */
test('polling keeps the running step running', () => {
    const next = steps.statesFromStatus(TOTAL, [1, 2, 3], 4, { status: 'running' });

    assert.equal(next[4], 'running');
    assert.equal(next[3], 'completed');
    assert.equal(next[5], 'pending');
});

test('a completed step never falls back to running or pending', () => {
    const next = steps.statesFromStatus(TOTAL, [1, 2], 3, { status: 'running' });

    assert.equal(next[1], 'completed');
    assert.equal(next[2], 'completed');
    assert.equal(next[3], 'running');
});

test('a step the server no longer lists as completed goes back to pending', () => {
    /* 步骤被回退/重跑时，服务端的 completed_steps 会少一项 —— 本地不能还挂着 completed */
    const next = steps.statesFromStatus(TOTAL, [1, 2], 3, { status: 'running' });

    assert.equal(next[9], 'pending');
    assert.equal(next[2], 'completed');
});

test('awaiting approval survives polling', () => {
    const next = steps.statesFromStatus(TOTAL, [1, 2, 3, 4, 5], 6, { status: 'awaiting_approval' });

    assert.equal(next[6], 'awaiting');
});

test('a failed step stays failed instead of turning back into running', () => {
    const next = steps.statesFromStatus(TOTAL, [1, 2, 3, 4, 5, 6], 7, { status: 'failed' });

    assert.equal(next[7], 'error');
});

test('a retry turns the failed step back into running', () => {
    /* error 不是终态：重试时同一个步骤要能重新亮起来，否则一直挂着 ❌ */
    const next = steps.statesFromStatus(TOTAL, [1, 2, 3, 4, 5, 6], 7, { status: 'running' });

    assert.equal(next[7], 'running');
});

test('unknown or missing previous state defaults to pending', () => {
    const next = steps.statesFromStatus(TOTAL, [], 1, { status: 'running' });

    assert.equal(next[1], 'running');
    assert.equal(next[2], 'pending');
    assert.equal(Object.keys(next).length, TOTAL);
});

test('mergeStatus only protects the completed terminal state', () => {
    assert.equal(steps.mergeStatus('completed', 'running'), 'completed');
    assert.equal(steps.mergeStatus('completed', 'pending'), 'completed');
    assert.equal(steps.mergeStatus('running', 'completed'), 'completed');
    assert.equal(steps.mergeStatus('error', 'running'), 'running');
    assert.equal(steps.mergeStatus('awaiting', 'running'), 'running');
    assert.equal(steps.mergeStatus(undefined, 'running'), 'running');
});

test('status icons and labels are one table, not two copies', () => {
    assert.equal(steps.stepIcon('completed'), '✅');
    assert.equal(steps.stepIcon('running'), '🔄');
    assert.equal(steps.stepIcon('awaiting'), '⏸️');
    assert.equal(steps.stepIcon('error'), '❌');
    assert.equal(steps.stepIcon('pending'), '⏳');
    assert.equal(steps.stepIcon('未定义的状态'), '⏳');
    assert.equal(steps.stepLabel('running'), '生成中');
});

/* ── 自动跟随：只在用户没往上翻的时候跟 ──────────────────────── */
function box(scrollTop, scrollHeight, clientHeight) {
    return { scrollTop, scrollHeight, clientHeight };
}

test('auto-follow when the live output is already at the bottom', () => {
    assert.equal(steps.shouldAutoFollow(box(400, 1000, 600)), true);
});

test('do not yank the view when the user scrolled up to read', () => {
    assert.equal(steps.shouldAutoFollow(box(0, 1000, 600)), false);
});

test('a short output that fits has nothing to follow', () => {
    assert.equal(steps.shouldAutoFollow(box(0, 300, 600)), true);
    assert.equal(steps.shouldAutoFollow(null), true);
});

test('the follow threshold tolerates sub-pixel and padding gaps', () => {
    assert.equal(steps.shouldAutoFollow(box(380, 1000, 600), 24), true);
    assert.equal(steps.shouldAutoFollow(box(300, 1000, 600), 24), false);
});
