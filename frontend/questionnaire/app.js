/* 云海睿评 · 问卷填写页脚本
 * 抽离后页面可 no-store，脚本走 /static 协商缓存。
 */
    let questionnaireData = [];
    let questionnaireVersion = null;
    let answers = {};  // question.id -> selected value
    let sessionId = '';

    document.addEventListener('DOMContentLoaded', () => {
        lucide.createIcons();
        // Check query param
        const params = new URLSearchParams(window.location.search);
        const sid = params.get('session');
        if (sid) {
            document.getElementById('session-input').value = sid;
            loadQuestionnaire();
        }
    });

    async function loadQuestionnaire() {
        sessionId = document.getElementById('session-input').value.trim();
        if (!sessionId) {
            showToast('请输入 session ID', 'error');
            return;
        }

        try {
            const resp = await fetch('/api/agent/workflow/' + encodeURIComponent(sessionId) + '/questionnaire');
            const data = await resp.json();

            if (!data.success) {
                showToast(data.detail || data.message || '加载失败', 'error');
                return;
            }

            questionnaireVersion = data.data.questionnaire_version;
            questionnaireData = (data.data.questionnaire || []).map(q => ({
                ...q,
                id: q.question_id ?? q.id,
                question: q.question_text ?? q.question,
            }));
            answers = {};
            renderAll();
            document.getElementById('controls-card').classList.remove('hidden');
            document.getElementById('stats-card').classList.remove('hidden');
            showToast(`已加载 ${questionnaireData.length} 道题目`);
        } catch (e) {
            showToast('加载失败: ' + e.message, 'error');
        }
    }

    async function reloadQuestionnaire() {
        answers = {};
        await loadQuestionnaire();
    }

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        })[char]);
    }

    function renderAll() {
        const container = document.getElementById('questionnaire-container');

        if (!questionnaireData.length) {
            container.innerHTML = '<div class="card" style="text-align:center;color:var(--muted);">尚无题目，请联系问卷发布者</div>';
            return;
        }

        container.innerHTML = questionnaireData.map((q, i) => {
            const isLikert = q.question_type === 'likert';
            const levels = q.scale_levels || [];
            const selected = answers[q.id];

            return `
                <div class="question-item" id="q-${escapeHtml(q.id)}">
                    <div style="display:flex; justify-content:space-between; align-items:start; margin-bottom:8px;">
                        <strong style="font-size:0.95rem;">${escapeHtml(q.index)}. ${escapeHtml(q.question)}</strong>
                        <span class="tag tag-${isLikert ? 'likert' : 'open'}">${isLikert ? '量表题' : '开放题'}</span>
                    </div>
                    ${q.related_indicator_name ? 
                        `<p style="font-size:0.75rem; color:var(--muted); margin-bottom:6px;">关联指标: ${escapeHtml(q.related_indicator_name)}</p>`
                        : ''}
                    ${isLikert ? renderLikertScale(q, selected) : renderOpenEnded(q, selected)}
                </div>
            `;
        }).join('');

        // Bind likert clicks
        questionnaireData.forEach(q => {
            if (q.question_type === 'likert') {
                (q.scale_levels || []).forEach((level, i) => {
                    const el = document.getElementById(`likert-${q.id}-${i}`);
                    if (el) {
                        el.addEventListener('click', () => selectLikert(q.id, i));
                    }
                });
            } else {
                const ta = document.getElementById(`open-${q.id}`);
                if (ta) {
                    ta.addEventListener('input', () => {
                        answers[q.id] = ta.value;
                        updateStats();
                    });
                }
            }
        });

        updateStats();
    }

    function renderLikertScale(q, selected) {
        const levels = q.scale_levels || [];
        const radios = levels.map((lv, i) => {
            const sel = selected === i ? ' selected' : '';
            return `<div class="likert-option${sel}" id="likert-${escapeHtml(q.id)}-${i}" title="${escapeHtml(lv)}">${i + 1}</div>`;
        }).join('');
        const labels = levels.map(lv => `<span class="likert-label">${escapeHtml(lv)}</span>`).join('');
        return `
            <div class="likert-scale">
                ${radios}
            </div>
            <div style="display:flex; gap:4px; margin-top:4px;">
                ${labels}
            </div>
        `;
    }

    function renderOpenEnded(q, selected) {
        return `<textarea id="open-${escapeHtml(q.id)}" rows="3" style="width:100%; padding:8px; background:#0f172a; border:1px solid var(--border); border-radius:8px; color:var(--text); font-size:0.85rem; margin-top:6px;" placeholder="请填写您的意见...">${escapeHtml(selected)}</textarea>`;
    }

    function selectLikert(qId, levelIndex) {
        answers[qId] = levelIndex;
        // Re-render just this item
        const q = questionnaireData.find(x => x.id === qId);
        if (!q) return;
        const item = document.getElementById('q-' + qId);
        if (!item) return;
        const levels = q.scale_levels || [];
        levels.forEach((_, i) => {
            const el = document.getElementById(`likert-${qId}-${i}`);
            if (el) el.classList.toggle('selected', i === levelIndex);
        });
        updateStats();
    }

    function updateStats() {
        const total = questionnaireData.length;
        const likert = questionnaireData.filter(q => q.question_type === 'likert').length;
        const open = questionnaireData.filter(q => q.question_type === 'open_ended').length;
        const answered = Object.values(answers).filter(v => v !== undefined && v !== '').length;

        document.getElementById('stat-total').textContent = total;
        document.getElementById('stat-likert').textContent = likert;
        document.getElementById('stat-open').textContent = open;
        document.getElementById('stat-answered').textContent = answered;
    }

    function csvCell(value) {
        const text = String(value ?? '');
        const safe = /^\s*[=+@-]|^[\t\r]/.test(text) ? "'" + text : text;
        return '"' + safe.replaceAll('"', '""') + '"';
    }

    function exportAsCSV() {
        if (!questionnaireData.length) return;
        const rows = [['序号', '题型', '问题', '关联指标', '答案']];
        questionnaireData.forEach(q => {
            const answer = answers[q.id];
            const text = q.question_type === 'likert' && answer !== undefined
                ? q.scale_levels?.[answer] ?? String(answer + 1) : answer;
            rows.push([q.index, q.question_type === 'likert' ? '量表题' : '开放题',
                q.question, q.related_indicator_name, text]);
        });
        const csv = '\uFEFF' + rows.map(row => row.map(csvCell).join(',')).join('\r\n');

        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `问卷_${new Date().toISOString().slice(0,10)}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        showToast('CSV 已下载');
    }

    async function submitAnswers() {
        if (!sessionId) { showToast('请先生成问卷', 'error'); return; }
        if (!questionnaireData.length) { showToast('问卷为空', 'error'); return; }

        const payload = {
            session_id: sessionId,
            questionnaire_version: questionnaireVersion,
            answers: questionnaireData.map(q => ({
                question_id: q.id,
                question_text: q.question,
                question_type: q.question_type,
                related_indicator_id: q.related_indicator_id || '',
                related_indicator_name: q.related_indicator_name || '',
                selected_level_index: q.question_type === 'likert' ? (answers[q.id] ?? null) : null,
                text_answer: q.question_type === 'open_ended' ? (answers[q.id] || '') : null,
            })),
        };

        try {
            const resp = await fetch('/api/agent/workflow/questionnaire/submit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (data.success) {
                showToast(`✅ 答卷已提交，共保存 ${data.data.saved} 条`);
            } else {
                showToast('提交失败: ' + (data.detail || data.message || '未知错误'), 'error');
            }
        } catch(e) {
            showToast('提交失败: ' + e.message, 'error');
        }
    }

    function printQuestionnaire() {
        window.print();
    }

    function showToast(msg, type = 'success') {
        const container = document.getElementById('toast-container');
        const t = document.createElement('div');
        t.className = `toast toast-${type}`;
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(() => t.remove(), 3000);
    }
    