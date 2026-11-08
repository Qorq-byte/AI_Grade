// 步骤7 评分落地徽标：直接 eval index.html 里的真实函数，用真实形态的台账数据跑。
//
// 为什么不在页面里测：徽标是纯字符串拼装，最容易出的问题是「undefined 漏进 HTML」
// 和「标题与计数说的不是一回事」——两种都只能靠喂数据看出来。这里的夹具形态全部
// 取自真实运行的 score_disposition（四轮 e2e 备份），不是编的。

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const HTML = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'client', 'index.html'), 'utf8');

// esc 是页面里的公共转义函数，这里用等价实现，避免把整页脚本都拉进来
const escImpl = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/** 把 index.html 里的函数原样装进作用域，返回 (台账) => HTML。 */
function loadBadge() {
    const start = HTML.indexOf('function buildScoreDispositionBadge(){');
    assert.notStrictEqual(start, -1, 'index.html 里找不到 buildScoreDispositionBadge');
    const end = HTML.indexOf('/* 手改过分值或章节后', start);
    assert.notStrictEqual(end, -1, '找不到函数结束标记（注释「手改过分值或章节后」）');
    // 整段函数原样带进来，只用 var wfDispo 注入数据，免得在测试里复制一份实现
    const fnSrc = HTML.slice(start, end).trimEnd();
    const fn = new Function('esc', 'state',
        `var wfDispo = state;\n${fnSrc}\nreturn buildScoreDispositionBadge();`);
    return (state) => fn(escImpl, state);
}

const render = loadBadge();

const base = (counts, extra = {}) => ({
    step: 7,
    total: Object.values(counts).reduce((a, b) => a + b, 0),
    counts,
    accepted_score: 13.0,
    summary_line: '步骤 7 评分落地 x/y 项',
    items: {},
    ...extra,
});

test('全部落地 → ok 且不提缺失', () => {
    const html = render(base({ accepted: 3 }));
    assert.match(html, /wb-dispo ok/);
    assert.match(html, /全部 3 项指标已落地评分/);
    assert.doesNotMatch(html, /未落地/);
});

test('模型待核实为主 → warn 且说明这是合法拒绝', () => {
    const html = render(base({ accepted: 1, model_pending: 2 }));
    assert.match(html, /wb-dispo warn/);
    assert.match(html, /其余为模型判定资料不足/);
    assert.match(html, /合法拒绝，不是系统丢数据/);
    assert.doesNotMatch(html, /模型未提及/);
});

test('模型未提及为主 → warn，且标题不能说成「资料不足」', () => {
    // 真实形态：一轮 e2e 里模型只给了 1 条分，其余 21 个指标压根没提
    const html = render(base({ accepted: 1, not_mentioned: 21 }));
    assert.match(html, /wb-dispo warn/);
    assert.match(html, /其余 21 项模型未提及（模型输出不完整）/);
    assert.doesNotMatch(html, /其余为模型判定资料不足/, '未提及与资料不足是两回事');
    assert.match(html, /建议重跑步骤 7 或人工补评分/);
});

test('系统侧原因 → err，且排在最高优先级', () => {
    const html = render(base({ accepted: 2, anchor_unresolved: 1, not_mentioned: 5 }));
    assert.match(html, /wb-dispo err/);
    assert.match(html, /1 项评分因系统侧原因未落地/);
});

test('两类缺失并存 → 不硬猜原因，写「分项见下」', () => {
    const html = render(base({ accepted: 1, model_pending: 2, not_mentioned: 3 }));
    assert.match(html, /其余未落地（分项见下）/);
});

test('体系外指标 ID 单独计数并进明细', () => {
    const html = render(base({ accepted: 3 }, {
        unmatched: [{ indicator_id: 'L3_01_01_03', actual_score: 3, evidence: 'P1-1-1' }],
    }));
    assert.match(html, /模型写了体系外指标 ID 1 条/);
    assert.match(html, /L3_01_01_03/);
    assert.match(html, /这些分没有被采用/);
});

test('未落地明细表带出模型原始引用（便于排查）', () => {
    const html = render({
        step: 7, total: 2, counts: { accepted: 1, anchor_unresolved: 1 }, accepted_score: 4,
        summary_line: 'x',
        items: {
            L3_01_01_01: { status: 'accepted', actual_score: 4, evidence: 'P1-10-1' },
            L3_02_02_02: { status: 'anchor_unresolved', evidence: 'P1-1-1', cited_ids: ['P1-1-1'] },
        },
    });
    assert.match(html, /L3_02_02_02/);
    assert.match(html, /P1-1-1/);
    assert.match(html, /引用无法定位/);
});

test('台账为空时不渲染任何东西', () => {
    assert.strictEqual(render(null), '');
    assert.strictEqual(render({}), '');
    assert.strictEqual(render({ total: 0 }), '');
});

test('任何输入都不许把 undefined / NaN 漏进 HTML', () => {
    const fixtures = [
        base({ accepted: 1, model_pending: 2 }),
        base({ accepted: 1, not_mentioned: 21 }),
        base({ accepted: 2, anchor_unresolved: 1 }),
        base({ accepted: 0, score_invalid: 18, reason_missing: 1 }),
        base({ accepted: 1 }, { unmatched: [{ indicator_id: 'X', actual_score: null, evidence: null }] }),
        { step: 7, total: 1, counts: {}, summary_line: '', items: { A: {} } },
    ];
    for (const fixture of fixtures) {
        const html = render(fixture);
        assert.doesNotMatch(html, /undefined/, `台账 ${JSON.stringify(fixture.counts)} 渲染出 undefined`);
        assert.doesNotMatch(html, /NaN/, `台账 ${JSON.stringify(fixture.counts)} 渲染出 NaN`);
    }
});

test('该被转义的字符不会原样进 HTML', () => {
    const html = render({
        step: 7, total: 1, counts: { accepted: 1 }, accepted_score: 4, summary_line: 'x',
        items: {}, unmatched: [{ indicator_id: '<img onerror=x>', actual_score: 1, evidence: '"><b>' }],
    });
    assert.doesNotMatch(html, /<img onerror/);
    assert.match(html, /&lt;img onerror/);
});

test('弱定位（anchor_weak）→ warn 而不是 err，且点名「原文行」', () => {
    // 实测第 7 轮：fe_wf_sample.txt 第14行 —— 引用是真的、行号是真的，
    // 只是那一行没进检索证据。这不是系统故障，不该显示成红色错误。
    const html = render(base({ anchor_weak: 2 }, {
        items: {
            L3_02_01_02: { status: 'anchor_weak', actual_score: 2.0,
                evidence: 'fe_wf_sample.txt 第14行',
                evidence_anchor: { kind: 'material_locator', matched: ['fe_wf_sample.txt:第14行'] } },
        },
    }));
    assert.match(html, /wb-dispo warn/);
    assert.doesNotMatch(html, /wb-dispo err/);
    assert.match(html, /原文行号/);
    assert.match(html, /不计入总分/);
});

test('弱定位不算系统侧原因（不能和后两类混成一句「系统侧未落地」）', () => {
    const html = render(base({ anchor_weak: 1 }));
    assert.doesNotMatch(html, /系统侧原因未落地/);
});

test('弱定位的 chip 不使用 bad 样式', () => {
    const html = render(base({ anchor_weak: 1 }));
    assert.match(html, /引用可定位到原文行、但该行未进入检索证据 1 项/);
    assert.doesNotMatch(html, /wb-dispo-chip bad[^>]*>引用可定位到原文行/);
});

test('弱定位与「模型未提及」同时存在时，标题不把两者混为一谈', () => {
    const html = render(base({ anchor_weak: 2, not_mentioned: 15 }));
    assert.match(html, /分项见下/);
    assert.match(html, /引用可定位到原文行、但该行未进入检索证据 2 项/);
    assert.match(html, /模型未提及 15 项/);
});
