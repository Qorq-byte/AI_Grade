/* 云海睿评 · 报告预览页脚本
 * 抽离后页面可 no-store，脚本走 /static 协商缓存。
 */
    let sessionId = '';
    let originalReport = '';
    let isModified = false;
    const API = '/api/agent';
    function authFetch(url, options = {}) {
        const headers = new Headers(options.headers);
        const token = sessionStorage.getItem('token') || localStorage.getItem('token');
        if (token) headers.set('Authorization', 'Bearer ' + token);
        return fetch(url, { ...options, headers });
    }

    document.addEventListener('DOMContentLoaded', () => {
        lucide.createIcons();

        const params = new URLSearchParams(window.location.search);
        sessionId = params.get('session') || '';

        if (sessionId) {
            loadReport();
        } else {
            document.getElementById('editor').value = '';
            document.getElementById('editor').placeholder = '未提供 session ID。请从工作流页面跳转至此，或在URL添加 ?session=xxx';
        }

        document.getElementById('editor').addEventListener('input', () => {
            isModified = document.getElementById('editor').value !== originalReport;
            document.getElementById('modified-badge').style.display = isModified ? 'inline-block' : 'none';
            debouncedPreview();
        });

        // Keyboard shortcut: Ctrl+S to save
        document.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                saveReport();
            }
        });
    });

    async function loadReport() {
        document.getElementById('editor').placeholder = '加载中...';

        try {
            const resp = await authFetch(`${API}/workflow/${sessionId}/status`);
            const data = await resp.json();

            if (!data.success || !data.data) {
                showToast('加载失败: 工作流不存在', 'error');
                return;
            }

            // Load step results
            const stepSummaries = data.data.step_summaries || [];
            loadStepSections(stepSummaries);

            // Try to get full report via completion state
            // For now, use the step content previews
            let reportContent = '';

            // Fetch detailed step contents
            // We'll use the step_results from a hypothetical complete state
            // In production, we need a get_full_report endpoint
            reportContent = await fetchFullReport();

            if (!reportContent || reportContent.trim().length < 50) {
                // Build from step summaries
                reportContent = buildReportFromSteps(data.data);
            }

            originalReport = reportContent;
            document.getElementById('editor').value = reportContent;
            document.getElementById('editor').placeholder = '在此编辑报告内容...';
            refreshPreview();
            updateWordCount();
            showToast('报告加载完成');
        } catch (e) {
            showToast('加载失败: ' + e.message, 'error');
            document.getElementById('editor').placeholder = '加载失败，请刷新重试';
        }
    }

    async function fetchFullReport() {
        try {
            const resp = await authFetch(`${API}/workflow/${sessionId}/report`);
            const data = await resp.json();
            if (data.data?.review_steps?.length) {
                showToast('上游已修改，待复核步骤：' + data.data.review_steps.join('、'), 'error');
            }
            if (data.success && data.data.content) {
                return data.data.content;
            }
        } catch (e) { /* ignore */ }
        return '';
    }

    function buildReportFromSteps(data) {
        const parts = [];
        parts.push(`# ${data.project_name || '绩效评价报告'}\n`);
        parts.push(`*生成时间: ${new Date().toLocaleString('zh-CN')}*\n`);

        const stepNames = {
            1:'一、资料清单', 2:'二、有效信息', 3:'三、指标体系',
            4:'四、指标分值', 5:'五、评分标准', 6:'六、评价指标体系表',
            7:'七、指标得分分析', 8:'八、经验做法', 9:'九、问题及原因分析',
            10:'十、改进建议', 11:'十一、综合评价结论', 12:'十二、基本情况',
            13:'十三、评价工作简介', 14:'十四、完整报告'
        };

        const summaries = data.step_summaries || [];
        summaries.forEach(s => {
            const name = stepNames[s.step_number] || `Step ${s.step_number}`;
            parts.push(`\n## ${name}\n`);
            parts.push(s.content_preview || '*(内容待补充)*');
        });

        return parts.join('\n');
    }

    function loadStepSections(summaries) {
        const container = document.getElementById('step-sections');
        if (!summaries || !summaries.length) {
            container.innerHTML = '<p style="font-size:0.8rem; color:var(--muted);">暂无步骤数据</p>';
            return;
        }

        container.innerHTML = `
            <h4 style="font-size:0.85rem; margin-bottom:8px; color:var(--muted);">已完成步骤</h4>
            ${summaries.map(s => `
                <div class="step-section">
                    <h3>Step ${s.step_number} — ${s.step_name}</h3>
                    <p style="font-size:0.8rem; color:var(--muted);">${(s.content_preview || '').substring(0,100)}...</p>
                </div>
            `).join('')}
        `;
    }

    // ─── Preview ────────────────────────────────
    let debounceTimer;
    function debouncedPreview() {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(refreshPreview, 300);
    }

    function refreshPreview() {
        const content = document.getElementById('editor').value;
        const preview = document.getElementById('preview');

        if (!content.trim()) {
            preview.innerHTML = '<em style="color:var(--muted);">等待编辑器中输入内容...</em>';
            updateWordCount();
            return;
        }

        // Simple markdown rendering
        preview.innerHTML = renderMarkdown(content);
        updateWordCount();
    }

    function renderMarkdown(md) {
        let html = md
            // Headers
            .replace(/^### (.+)$/gm, '<h3>$1</h3>')
            .replace(/^## (.+)$/gm, '<h2>$1</h2>')
            .replace(/^# (.+)$/gm, '<h1>$1</h1>')
            // Bold
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            // Italic
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            // Lists
            .replace(/^- (.+)$/gm, '<li>$1</li>')
            .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
            // Line breaks
            .replace(/\n\n/g, '</p><p>')
            .replace(/\n/g, '<br>');

        return `<p>${html}</p>`;
    }

    function updateWordCount() {
        const text = document.getElementById('editor').value;
        const count = text.replace(/\s/g, '').length;
        document.getElementById('word-count').textContent = `${count} 字`;
    }

    // ─── Actions ───────────────────────────────
    async function saveReport() {
        if (!sessionId) return;
        const content = document.getElementById('editor').value;
        // Note: We need a save endpoint. For now, show a message.
        try {
            const resp = await authFetch(`${API}/workflow/save-report`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, content }),
            });
            const data = await resp.json();
            if (data.success) {
                showToast('✅ 报告已保存到服务器');
                originalReport = content;
                isModified = false;
                document.getElementById('modified-badge').style.display = 'none';
            } else {
                showToast('保存失败: ' + (data.message || '未知错误'), 'error');
            }
        } catch(e) {
            showToast('保存失败: ' + e.message, 'error');
        }
    }

    function resetToOriginal() {
        if (confirm('确定恢复到原始报告吗？所有修改将丢失。')) {
            document.getElementById('editor').value = originalReport;
            isModified = false;
            document.getElementById('modified-badge').style.display = 'none';
            refreshPreview();
            showToast('已恢复');
        }
    }

    function insertTemplate() {
        const template = `\n## 模块标题\n\n### 1. 分项标题\n\n主要内容描述...\n\n* 要点一\n* 要点二\n* 要点三\n`;
        const editor = document.getElementById('editor');
        const start = editor.selectionStart;
        const end = editor.selectionEnd;
        const text = editor.value;
        editor.value = text.substring(0, start) + template + text.substring(end);
        editor.focus();
        editor.selectionStart = editor.selectionEnd = start + template.length;
        refreshPreview();
    }

    /* 导出闸门（批次 26）：报告有缺章/步骤没跑完时后端返回 409 + X-Export-Blocked。
       问一次「仍要导出」，确认后带 force 重导 —— 否则缺一章就彻底导不出去。 */
    async function exportWithForce(url, payload) {
        const post = (body) => authFetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        let resp = await post(payload);
        if (resp.status === 409 && resp.headers.get('X-Export-Blocked') === '1') {
            const blocked = await resp.json();
            const ok = confirm((blocked.detail || '报告还不能导出')
                + '\n\n仍要导出吗？缺失的章节会原样保留为一句占位说明。');
            if (!ok) return blocked;
            resp = await post(Object.assign({}, payload, { force: true }));
        }
        return resp.json();
    }

    async function exportEditedReport(format) {
        if (!sessionId) {
            showToast('未关联工作流', 'error');
            return;
        }
        showToast('导出中...');
        try {
            const data = await exportWithForce(`${API}/workflow/export`, { session_id: sessionId, format });
            if (data.success) {
                showToast(`导出成功: ${data.data.file_name}`);
            } else {
                showToast(data.detail || '导出失败', 'error');
            }
        } catch (e) {
            showToast('导出失败: ' + e.message, 'error');
        }
    }

    function showToast(msg, type = 'success') {
        const container = document.getElementById('toast-container');
        const t = document.createElement('div');
        t.className = `toast toast-${type}`;
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(() => t.remove(), 3000);
    }
    