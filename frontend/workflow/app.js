/* 云海睿评 · 工作流页脚本
 * 从 index.html 抽出来单独托管：页面是 no-store，脚本走 /static 可协商缓存。
 */
    // ══════════════════════════════════════
    //  State
    // ══════════════════════════════════════
    const API = '/api/agent';
    /* 统一走共享请求层（WBApi.raw）：
       - 自动带令牌；401 时先刷新令牌再重放一次，不会莫名其妙掉登录
       - 超时/网络错误归一成带 detail 的 ApiError，页面提示能说清原因
       - 仍然返回原始 Response，调用方继续自己 res.json() / 读 SSE 流
       noTimeout：工作流步进动辄几分钟，不能被默认的 30 秒掐掉。*/
    function authFetch(url, options = {}) {
        return WBApi.raw(options.method || 'GET', url, options.body, {
            headers: options.headers,
            signal: options.signal,
            noTimeout: true,
        });
    }
    const STEPS = {
        1:'整理资料清单',2:'核心有效信息提取',3:'构建指标体系',4:'生成指标分值',
        5:'生成评分标准',6:'评价指标体系表',7:'指标得分分析',8:'总结经验做法',
        9:'分析问题及原因',10:'提出改进建议',11:'综合评价分析',12:'基本情况',
        13:'评价工作简介',14:'组装最终报告'
    };
    let sessionId = null;
    let workflowStreamController = null;
    let eventSource = null;
    let isRunning = false;
    let completedSteps = new Set();
    // 每个步骤的状态（pending/running/awaiting/completed/error）。
    // 以前只有 completedSteps 一个 Set，于是「正在运行」只存在于两处 DOM 补丁里，
    // pollStatus 每 5 秒全量重画一次就把它抹成未运行（用户报的就是这个）。
    let stepStates = {};
    let currentApprovalStep = null;
    let workflowStepResults = {};
    // 问卷提示不是 14 个步骤之一，但它也要活过 renderStepList()；
    // 存成 HTML 片段，由 renderStepList 补在末尾（原来直接 appendChild，一次轮询就没了）
    let questionnairePromptHtml = null;
    let questionnairePromptDone = false;
    let uploadedFiles = [];

    // ── 登录校验 + 自动携带认证Token ──
    const AUTH_TOKEN = sessionStorage.getItem('token') || localStorage.getItem('token') || '';
    if (!AUTH_TOKEN) {
        window.location.href = '/login?redirect=/workflow';
    }
    document.addEventListener('DOMContentLoaded', () => {
        if (!AUTH_TOKEN) return;
        if (window.lucide) lucide.createIcons();
        setupUpload();
        document.getElementById('start-btn').addEventListener('click', startWorkflow);
        document.getElementById('stop-btn').addEventListener('click', stopWorkflow);
        sessionId = new URLSearchParams(location.search).get('session') || sessionStorage.getItem(workflowSessionKey);
        if (sessionId) pollStatus();
    });

    // ══════════════════════════════════════
    //  File Upload
    // ══════════════════════════════════════
    function setupUpload() {
        const dropzone = document.getElementById('dropzone');
        const input = document.getElementById('file-input');

        dropzone.addEventListener('click', () => input.click());
        dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag-over'));
        dropzone.addEventListener('drop', e => {
            e.preventDefault();
            dropzone.classList.remove('drag-over');
            handleFiles(e.dataTransfer.files);
        });
        input.addEventListener('change', () => handleFiles(input.files));
    }

    // 分类上传标签切换
    let currentUploadTab = 'all';
    const UPLOAD_TYPE_MAP = {
        'all':   { accept: '.pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png,.bmp,.txt,.csv', hint: '拖拽文件或点击上传', types: 'PDF/Word/Excel/图片/TXT' },
        'word':  { accept: '.doc,.docx', hint: '上传Word文档', types: '.doc / .docx' },
        'excel': { accept: '.xls,.xlsx', hint: '上传Excel表格', types: '.xls / .xlsx' },
        'pdf':   { accept: '.pdf', hint: '上传PDF文件', types: '.pdf' },
        'image': { accept: '.jpg,.jpeg,.png,.bmp', hint: '上传图片文件', types: 'JPG/PNG/BMP' },
    };

    function switchUploadTab(type, btn) {
        currentUploadTab = type;
        // 更新按钮样式
        document.querySelectorAll('[id^="upload-tab-"]').forEach(b => {
            b.style.background = '';
            b.style.color = '';
        });
        btn.style.background = 'var(--accent)';
        btn.style.color = '#000';
        // 更新文件输入accept
        const input = document.getElementById('file-input');
        const cfg = UPLOAD_TYPE_MAP[type] || UPLOAD_TYPE_MAP['all'];
        input.accept = cfg.accept;
        document.getElementById('upload-hint').textContent = cfg.hint;
        document.getElementById('upload-types').textContent = cfg.types;
    }

    async function handleFiles(files) {
        const formData = new FormData();
        for (const f of files) formData.append('files', f);

        try {
            const resp = await authFetch('/api/agent/upload/batch', { method: 'POST', body: formData });
            const data = await resp.json();
            if (data.success) {
                data.data.uploaded.forEach(f => {
                    uploadedFiles.push(f);
                    addFileTag(f);
                });
                data.data.errors.forEach(e => showToast(e.error || '上传失败', 'error'));
            }
        } catch(e) {
            showToast('上传失败: ' + e.message, 'error');
        }
    }

    function addFileTag(file) {
        const list = document.getElementById('file-list');
        const tag = document.createElement('span');
        tag.className = 'file-tag';
        tag.innerHTML = `${file.original_name} <button onclick="removeFile('${file.file_path}', this.parentElement)">✕</button>`;
        list.appendChild(tag);
    }

    function removeFile(path, el) {
        uploadedFiles = uploadedFiles.filter(f => f.file_path !== path);
        el.remove();
    }

    // ══════════════════════════════════════
    //  SSE Workflow
    // ══════════════════════════════════════
    function startWorkflow() {
        const projectName = document.getElementById('project-name').value.trim() || '未命名项目';
        const modelName = document.getElementById('model-select').value;
        const tempProfile = document.getElementById('temp-profile').value;
        const compMode = document.getElementById('comparison-mode').value;
        const useRag = document.getElementById('use-rag').checked;

        if (isRunning) return;

        // Reset state
        completedSteps = new Set();
        stepStates = {};
        workflowStepResults = {};
        questionnairePromptHtml = null;
        questionnairePromptDone = false;
        resetLivePanel(null);
        document.getElementById("workflow-step-detail").hidden = true;
        currentApprovalStep = null;
        isRunning = true;

        document.getElementById('start-btn').disabled = true;
        document.getElementById('stop-btn').classList.remove('hidden');
        document.getElementById('quick-actions').classList.add('hidden');
        document.getElementById('report-area').classList.add('hidden');
        document.getElementById('approval-panel').classList.add('hidden');
        document.getElementById('alerts-area').innerHTML = '';
        document.getElementById('progress-bar').style.width = '0%';
        document.getElementById('progress-text').textContent = '0 / 14';
        document.getElementById('status-title').textContent = '🚀 执行中...';

        renderStepList();
        updateProgressBar();

        // Show feature toolbar
        document.getElementById('feature-toolbar').classList.remove('hidden');

        sessionId = newWorkflowSession();
        sessionStorage.setItem(workflowSessionKey, sessionId);
        const body = {
            session_id: sessionId,
            require_approval: document.getElementById('require-approval').checked,
            project_name: projectName,
            file_paths: uploadedFiles.map(f => f.file_path),
            model_name: modelName,
            temperature_profile: tempProfile,
            comparison_mode: compMode,
            use_rag: useRag,
            language_style: document.getElementById('language-style').value,
        };

        workflowStreamController = new AbortController();
        authFetch('/api/agent/workflow/start-stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: workflowStreamController.signal,
        }).then(response => readWorkflowEvents(response, handleSSEEvent)).catch(err => {
            if (err.name === 'AbortError') return;
            showToast(err.message, 'error');
            pollStatus();
        });
    }

    // ══════════════════════════════════════
    //  Live output panel
    // ══════════════════════════════════════
    // 三份状态：liveStep（正在追的步骤）、liveText（它的全文）、livePre（DOM 缓存）。
    // 为什么要存 liveText：只看 DOM 的话，用户点开别的步骤或把面板关掉，
    // 已经生成的内容就再也拿不回来了 —— 而"点回正在运行的步骤"正是最常见的操作。
    let liveStep = null;
    let liveText = '';
    let livePre = null;

    function livePanelEls() {
        return {
            panel: document.getElementById('step-detail-panel'),
            content: document.getElementById('step-detail-content'),
            title: document.getElementById('step-detail-title'),
        };
    }

    /* 重建面板骨架（标题 + 提示 + <pre>），返回可追加的 <pre> */
    function buildLivePanel(step) {
        const { content, title } = livePanelEls();
        if (!content || !title) return null;
        const status = stepStates[step] || 'running';
        title.textContent = `Step ${step} · ${STEPS[step] || ''} · ${WorkflowSteps.stepLabel(status)}`;
        content.innerHTML =
            '<p class="live-hint" style="color:var(--muted);font-size:0.85rem;margin:0 0 8px;">等待模型输出…</p>'
            + '<pre class="live-output" style="white-space:pre-wrap;margin:0;font-family:inherit"></pre>';
        livePre = content.querySelector('.live-output');
        return livePre;
    }

    /* 切到某步骤的实时面板。换步骤必须清空，否则新步骤的字会接在上一步的尾巴后面。 */
    function ensureLivePanel(step) {
        const { panel } = livePanelEls();
        if (!panel) return null;
        const wasHidden = panel.classList.contains('hidden');
        if (liveStep !== step || !livePre) {
            liveStep = step;
            liveText = '';
            buildLivePanel(step);
        }
        if (wasHidden) {
            panel.classList.remove('hidden');
            // 只在首次出现时把页面带过来；之后的跟随交给面板内部滚动
            panel.scrollIntoView({ block: 'nearest' });
        }
        return livePre;
    }

    function resetLivePanel(step) {
        liveStep = step;
        liveText = '';
        livePre = null;
        const { panel, content } = livePanelEls();
        if (!step) {
            if (content) content.innerHTML = '';
            if (panel) panel.classList.add('hidden');
            return;
        }
        if (panel && !panel.classList.contains('hidden')) buildLivePanel(step);
    }

    /* 点正在运行的步骤：把实时面板显示出来并滚过去（内容还没到就显示提示） */
    function focusLivePanel(step) {
        ensureLivePanel(step);
        const { panel } = livePanelEls();
        if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    /* 步骤完成：用事件里的完整正文替掉流式累积（两者可能有细微差异） */
    function finishLivePanel(event) {
        if (liveStep !== event.step_number) return;
        const pre = buildLivePanel(event.step_number);
        if (!pre) return;
        liveText = event.content || liveText;
        pre.textContent = liveText;
        pre.scrollTop = pre.scrollHeight;
    }

    // token 增量：50ms 合并后追加到实时面板，面板内部跟随到底部
    const deltaRenderer = WorkflowController.createDeltaRenderer((step, text) => {
        const pre = ensureLivePanel(step);
        if (!pre) return;
        // 用户往上翻在读旧内容时不要把他拽回底部
        const follow = WorkflowSteps.shouldAutoFollow(pre);
        liveText += text;
        pre.textContent = liveText;
        const hint = pre.parentElement && pre.parentElement.querySelector('.live-hint');
        if (hint) hint.remove();
        if (follow) pre.scrollTop = pre.scrollHeight;
    });

    function handleSSEEvent(event) {
        const type = event.event;

        if (type === 'token_delta') {
            deltaRenderer.push(event.step_number, event.delta);
            return;
        }
        if (type === 'step_start') {
            deltaRenderer.reset();
            resetLivePanel(event.step_number);   // 上一步的残留必须清掉
            updateStepStatus(event.step_number, 'running', event.step_name);
            document.getElementById('status-title').textContent = '执行中 · ' + event.step_name;
            updateProgressBar();
        } else if (type === 'step_complete') {
            deltaRenderer.flushNow();
            updateStepStatus(event.step_number, 'completed', event.step_name);
            completedSteps.add(event.step_number);
            workflowStepResults[event.step_number] = event;
            finishLivePanel(event);
            updateProgressBar();
            if (event.warnings?.length) showAlert('warning', event.warnings);
        } else if (type === 'approval_required') {
            updateStepStatus(event.step_number, 'awaiting', event.step_name);
            showApprovalPanel(event);
        } else if (type === 'waiting_approval') {
            const step = event.approval_step;
            updateStepStatus(step, 'awaiting', STEPS[step]);
            showApprovalPanel({ ...event, step_number: step, step_name: STEPS[step] });
            document.getElementById('status-title').textContent = '等待人工确认 · ' + STEPS[step];
        } else if (type === 'warning') {
            showAlert('warning', [event.message]);
        } else if (type === 'error') {
            updateStepStatus(event.step_number, 'error', event.step_name);
            showAlert('error', [event.message]);
            pollStatus();
            return;
        } else if (type === 'complete') {
            handleComplete(event);
        } else if (type === 'questionnaire_ready') {
            showQuestionnairePrompt(event);
        } else if (type === 'token_update') {
            // 更新token消耗显示
            const tokenEl = document.getElementById('token-used');
            if (tokenEl) tokenEl.textContent = event.tokens_used || 0;
        }

        if (event.session_id && !sessionId) {
            sessionId = event.session_id;
        }
    }

    function showQuestionnairePrompt(event) {
        // 在步骤列表中插入问卷提示
        if (questionnairePromptHtml) return; // 只显示一次
        questionnairePromptHtml = `
            <span class="step-dot awaiting"></span>
            <span style="flex:1;">📋 <strong>生成调查问卷</strong></span>
            <button class="btn btn-accent btn-sm" onclick="event.stopPropagation();generateQuestionnaire()">立即生成</button>
        `;
        renderStepList();   // 由 renderStepList 负责把它补在 14 行之后，重画不会丢
    }

    async function generateQuestionnaire() {
        if (!sessionId) { showToast('请先启动工作流', 'error'); return; }
        try {
            const resp = await authFetch('/api/agent/workflow/questionnaire', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, max_questions: 30 }),
            });
            const data = await resp.json();
            if (data.success) {
                showToast(`已生成 ${data.total_questions} 道问卷题目`, 'success');
                questionnairePromptDone = true;
                questionnairePromptHtml = `
                    <span class="step-dot completed"></span>
                    <span>📋 调查问卷已生成 (${data.total_questions} 题)</span>
                    <button class="btn btn-sm" onclick="event.stopPropagation();window.open('/questionnaire?session=${sessionId}','_blank')">打开问卷</button>
                `;
                renderStepList();
            } else {
                showToast('问卷生成失败: ' + (data.message || '未知错误'), 'error');
            }
        } catch(e) {
            showToast('问卷生成失败: ' + e.message, 'error');
        }
    }

    function showApprovalPanel(event) {
        currentApprovalStep = event.step_number;
        const panel = document.getElementById('approval-panel');
        panel.classList.remove('hidden');
        document.getElementById('approval-step-name').textContent =
            `Step ${event.step_number} — ${event.step_name}`;
        document.getElementById('approval-preview').textContent =
            event.content_preview || '(暂无预览)';
    }

    async function doApprove(approved) {
        const feedback = document.getElementById('approval-feedback').value;
        await workflowAction('approve', { approved, feedback, background: true, approval_step: currentApprovalStep });
    }

    async function workflowAction(action, fields = {}) {
        try {
            const response = await authFetch('/api/agent/workflow/' + action, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, ...fields }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail);
        } catch (error) {
            showToast(error.message, 'error');
        }
        pollStatus();
    }

    function retryWorkflow() {
        return workflowAction('retry');
    }

    let statusController;
    function pollStatus() {
        if (!sessionId) return;
        statusController?.abort();
        statusController = new AbortController();
        watchWorkflowStatus(sessionId, authFetch, data => {
            const status = data.status;
            isRunning = ['running', 'retrying', 'awaiting_approval', 'paused'].includes(status);
            document.getElementById('start-btn').disabled = isRunning;
            document.getElementById('retry-workflow').hidden = !data.retryable;
            document.getElementById('status-title').textContent = (workflowStatusLabels[status] || '会话不可用')
                + (data.current_step_name ? ' · ' + data.current_step_name : '');
            if (status === 'not_found') {
                sessionStorage.removeItem(workflowSessionKey);
                return;
            }
            completedSteps = new Set(data.completed_steps);
            // 由服务端的 completed + current_step 推出全量状态；running 不再被"只认 completed"抹掉
            stepStates = WorkflowSteps.statesFromStatus(14, data.completed_steps, data.current_step,
                { status });
            data.step_summaries.forEach(step => {
                if ('content' in step) workflowStepResults[step.step_number] = step;
            });
            renderStepList();
            updateProgressBar();
            if (data.current_step && status !== 'completed') {
                const style = status === 'failed' ? 'error' : data.requires_approval ? 'awaiting' : 'running';
                updateStepStatus(data.current_step, style, data.current_step_name);
            }
            document.getElementById('approval-panel').classList.toggle('hidden', !data.requires_approval);
            if (data.requires_approval) {
                showApprovalPanel({
                    step_number: data.approval_step, step_name: data.approval_step_name,
                    content_preview: data.step_summaries.find(step => step.step_number === data.approval_step)?.content_preview,
                });
            } else if (status === 'completed') {
                handleComplete({ session_id: sessionId,
                    final_report_preview: data.step_summaries.find(step => step.step_number === 14)?.content_preview });
            }
            if (data.review_steps.length) showAlert('warning', ['待复核步骤：' + data.review_steps.join('、')]);
        }, () => {
            document.getElementById('status-title').textContent = '浏览器连接中断，正在恢复进度…';
        }, statusController.signal);
    }

    function handleComplete(event) {
        isRunning = false;
        document.getElementById('start-btn').disabled = false;
        document.getElementById('stop-btn').classList.add('hidden');
        document.getElementById('status-title').textContent = '✅ 报告生成完成';
        document.getElementById('progress-bar').style.width = '100%';
        document.getElementById('progress-text').textContent = '14 / 14';
        document.getElementById('quick-actions').classList.remove('hidden');

        // 列表也要一起走到"全绿"：以前只改了进度条，左侧仍挂着上一轮的状态
        completedSteps = new Set(Object.keys(STEPS).map(Number));
        stepStates = WorkflowSteps.statesFromStatus(14, [...completedSteps], 14, { status: 'completed' });
        renderStepList();

        sessionId = event.session_id || sessionId;

        if (event.final_report_preview) {
            const area = document.getElementById('report-area');
            area.classList.remove('hidden');
            document.getElementById('report-content').textContent = event.final_report_preview;
        }

        showToast('报告生成完成！');
    }

    function stopWorkflow() {
        workflowStreamController?.abort();
        pollStatus();
    }

    function resetUI() {
        isRunning = false;
        sessionId = null;
        currentApprovalStep = null;
        completedSteps = new Set();
        stepStates = {};
        workflowStepResults = {};
        questionnairePromptHtml = null;
        questionnairePromptDone = false;
        resetLivePanel(null);
        document.getElementById('start-btn').disabled = false;
        document.getElementById('stop-btn').classList.add('hidden');
        document.getElementById('approval-panel').classList.add('hidden');
        document.getElementById('status-title').textContent = '📋 进度';
        document.getElementById('progress-bar').style.width = '0%';
        document.getElementById('progress-text').textContent = '0 / 14';
        renderStepList();
    }

    // ══════════════════════════════════════
    //  Step List Rendering
    // ══════════════════════════════════════
    function showWorkflowStep(step) {
        const result = workflowStepResults[step];
        if (!result) {
            const status = stepStates[step] || 'pending';
            // 正在跑/等确认：没有"结果"可看，但实时面板里有正在生成的正文
            if (status === 'running' || status === 'awaiting') { focusLivePanel(step); return; }
            // 其余情况明确说出来 —— 以前这里静默 return，用户以为"点了没反应"
            showStepPlaceholder(step, status);
            return;
        }
        const details = document.getElementById('workflow-step-detail');
        details.hidden = false;
        details.open = true;
        document.getElementById('workflow-step-name').textContent = result.step_name;
        const sources = (result.evidence || []).map(hit => '[' + hit.id + '] ' + hit.source + ' · ' + hit.locator);
        document.getElementById('workflow-step-content').textContent = [result.content, ...sources].join('\n');
    }

    function showStepPlaceholder(step, status) {
        const details = document.getElementById('workflow-step-detail');
        details.hidden = false;
        details.open = true;
        document.getElementById('workflow-step-name').textContent =
            `Step ${step} · ${STEPS[step] || ''} · ${WorkflowSteps.stepLabel(status)}`;
        document.getElementById('workflow-step-content').textContent =
            status === 'error'
                ? '该步骤执行失败。可点左侧「重试失败步骤」重新生成，或查看上方告警。'
                : '该步骤尚未开始，暂无内容。';
    }

    function renderStepList() {
        const container = document.getElementById('step-list');
        container.innerHTML = Object.entries(STEPS).map(([num, name]) => {
            const n = parseInt(num);
            // 从 stepStates 渲染（不再只看 completedSteps）—— running 才不会一重画就丢
            const status = stepStates[n] || 'pending';
            return `<div class="step-row ${status}" id="step-${n}" onclick="showWorkflowStep(${n})">
                <div class="step-dot ${status}"></div>
                <span style="font-weight:600;">Step ${num}.</span>
                <span>${name}</span>
                <span class="step-icon" style="margin-left:auto;">${WorkflowSteps.stepIcon(status)}</span>
            </div>`;
        }).join('');
        if (questionnairePromptHtml) {
            const row = document.createElement('div');
            row.id = 'questionnaire-prompt-row';
            row.className = 'step-row ' + (questionnairePromptDone ? 'completed' : 'awaiting');
            row.style.cssText = 'cursor:pointer;';
            row.innerHTML = questionnairePromptHtml;
            row.addEventListener('click', () => generateQuestionnaire());
            container.appendChild(row);
        }
    }

    /* 只改一行，不重画整表（重画会丢焦点、丢滚动位置） */
    function paintStepRow(stepNo) {
        const el = document.getElementById('step-' + stepNo);
        if (!el) return;
        const status = stepStates[stepNo] || 'pending';
        el.className = `step-row ${status}`;
        const dot = el.querySelector('.step-dot');
        if (dot) dot.className = `step-dot ${status}`;
        const icon = el.querySelector('.step-icon');
        if (icon) icon.textContent = WorkflowSteps.stepIcon(status);
    }

    function updateStepStatus(stepNo, status, name) {
        const next = WorkflowSteps.mergeStatus(stepStates[stepNo], status);
        stepStates[stepNo] = next;
        if (document.getElementById('step-' + stepNo)) paintStepRow(stepNo);
        else renderStepList();
    }

    function updateProgressBar() {
        const count = completedSteps.size;
        const pct = Math.round((count / 14) * 100);
        document.getElementById('progress-bar').style.width = pct + '%';
        document.getElementById('progress-text').textContent = `${count} / 14`;
    }

    // ══════════════════════════════════════
    //  Alerts
    // ══════════════════════════════════════
    function showAlert(type, messages) {
        const area = document.getElementById('alerts-area');
        messages.forEach(msg => {
            const div = document.createElement('div');
            div.className = `card ${type === 'error' ? 'btn-danger' : ''}`;
            div.style.cssText = `margin:4px 0; padding:8px 12px; font-size:0.85rem; color:${type === 'error' ? '#ef4444' : '#f59e0b'};`;
            div.textContent = (type === 'error' ? '❌ ' : '⚠️ ') + msg;
            area.appendChild(div);
            setTimeout(() => div.remove(), 8000);
        });
    }

    // ══════════════════════════════════════
    //  Export
    // ══════════════════════════════════════
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

    async function exportReport(format) {
        if (!sessionId) return;
        showToast('正在导出...');
        try {
            const data = await exportWithForce('/api/agent/workflow/export', { session_id: sessionId, format });
            if (data.success) {
                showToast(`导出成功: ${data.data.file_name}`);
            } else {
                showToast(data.detail || '导出失败', 'error');
            }
        } catch(e) {
            showToast('导出失败: ' + e.message, 'error');
        }
    }

    function showToast(msg, type = 'success') {
        const container = document.getElementById('toast-container');
        const t = document.createElement('div');
        t.className = `toast toast-${type}`;
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(() => t.remove(), 4000);
    }

    // Initial render
    renderStepList();

    // ══════════════════════════════════════
    //  New Features: Step-specific actions
    // ══════════════════════════════════════
    let manualProblems = [];
    let deductionPoints = [];

    // Set language style before running steps
    async function setLanguageStyle() {
        if (!sessionId) return;
        const style = document.getElementById('language-style').value;
        try {
            await authFetch('/api/agent/workflow/set-language-style', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, language_style: style }),
            });
        } catch(e) { console.warn('Set language style failed:', e); }
    }

    // Extract deduction points from step 7
    async function extractDeductions() {
        if (!sessionId) return;
        showToast('正在提取扣分点...');
        try {
            const resp = await authFetch(`/api/agent/workflow/${sessionId}/extract-deductions`);
            const data = await resp.json();
            if (data.success) {
                deductionPoints = data.data.deduction_points || [];
                renderDeductionList();
                document.getElementById('deduction-panel').classList.remove('hidden');
                document.getElementById('deduction-panel').scrollIntoView({ behavior: 'smooth' });
                showToast(`已提取 ${deductionPoints.length} 个扣分点`);
            } else {
                showToast('提取失败', 'error');
            }
        } catch(e) {
            showToast('提取扣分点失败: ' + e.message, 'error');
        }
    }

    function renderDeductionList() {
        const container = document.getElementById('deduction-list');
        container.innerHTML = deductionPoints.map((p, i) => `
            <div style="display:flex; align-items:center; gap:10px; padding:6px 0; border-bottom:1px solid var(--border);">
                <input type="checkbox" id="ded-${i}" ${p.selected !== false ? 'checked' : ''} onchange="deductionPoints[${i}].selected = this.checked">
                <span style="flex:1; font-size:0.85rem;">
                    <strong>${p.indicator_name || p.level2_name + ' > ' + (p.indicator_name || '')}</strong>
                    <span style="color:#ef4444;"> 扣${p.deduction}分</span>
                    <div style="color:var(--muted); font-size:0.75rem;">${p.score_reason || p.context || ''}</div>
                </span>
            </div>
        `).join('') || '<p style="color:var(--muted);">未发现扣分项</p>';
    }

    function addManualProblem() {
        const input = document.getElementById('manual-problem-input');
        const text = input.value.trim();
        if (!text) return;
        manualProblems.push({ problem: text, source: 'manual' });
        input.value = '';
        renderManualProblems();
    }

    function renderManualProblems() {
        const container = document.getElementById('manual-problems-list');
        container.innerHTML = manualProblems.map((p, i) => `
            <div class="file-tag" style="display:inline-flex; margin:3px;">
                ${p.problem}
                <button onclick="manualProblems.splice(${i}, 1); renderManualProblems();">✕</button>
            </div>
        `).join('');
    }

    async function confirmDeductions() {
        if (!sessionId) return;
        const selected = deductionPoints.filter(p => p.selected !== false);
        try {
            const resp = await authFetch('/api/agent/workflow/confirm-deductions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionId,
                    selected_points: selected,
                    manual_problems: manualProblems,
                }),
            });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('deduction-panel').classList.add('hidden');
                showToast(`已确认 ${selected.length} 个扣分点 + ${manualProblems.length} 个手动问题`);
            }
        } catch(e) {
            showToast('确认失败: ' + e.message, 'error');
        }
    }

    // Logic verification (after step 10)
    async function verifyLogic() {
        if (!sessionId) return;
        showToast('正在进行逻辑校对...');
        try {
            const resp = await authFetch(`/api/agent/workflow/${sessionId}/verify-logic`, { method: 'POST' });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('logic-verification-content').textContent = data.data.verification_result;
                document.getElementById('logic-verification-panel').classList.remove('hidden');
                document.getElementById('logic-verification-panel').scrollIntoView({ behavior: 'smooth' });
                showToast('逻辑校对完成');
            }
        } catch(e) {
            showToast('逻辑校对失败: ' + e.message, 'error');
        }
    }

    // Score summary (step 11)
    async function showScoreSummary() {
        if (!sessionId) return;
        showToast('正在计算评分汇总...');
        try {
            const resp = await authFetch(`/api/agent/workflow/${sessionId}/score-summary`);
            const data = await resp.json();
            if (data.success) {
                const d = data.data;
                document.getElementById('score-summary-content').innerHTML = `
                    <div style="font-size:1.2rem; margin-bottom:8px;">
                        综合得分: <strong style="color:var(--accent);">${d.score_100 == null ? '待核实' : d.score_100 + '分'}</strong>
                        &nbsp; 等级: <strong style="font-size:1.3rem; color:${d.grade === '优秀' ? '#22c55e' : d.grade === '良好' ? '#3b82f6' : d.grade === '合格' ? '#f59e0b' : '#ef4444'}">${d.grade}</strong>
                    </div>
                    <table style="width:100%; border-collapse:collapse; font-size:0.85rem;">
                        <tr style="border-bottom:1px solid var(--border);">
                            <th style="text-align:left; padding:6px;">一级指标</th>
                            <th style="text-align:right;">满分</th>
                            <th style="text-align:right;">得分</th>
                            <th style="text-align:right;">得分率</th>
                        </tr>
                        ${d.level1_scores.map(l1 => `
                            <tr style="border-bottom:1px solid var(--border);">
                                <td style="padding:6px;">${l1.name}</td>
                                <td style="text-align:right;">${l1.full_score}</td>
                                <td style="text-align:right; color:${l1.score_rate >= 80 ? '#22c55e' : l1.score_rate >= 60 ? '#f59e0b' : '#ef4444'};">${l1.actual_score}</td>
                                <td style="text-align:right;">${l1.score_rate}%</td>
                            </tr>
                        `).join('')}
                        <tr style="font-weight:bold; border-top:2px solid var(--accent);">
                            <td style="padding:6px;">合计</td>
                            <td style="text-align:right;">${d.total_full}</td>
                            <td style="text-align:right; color:var(--accent);">${d.total_score}</td>
                            <td style="text-align:right;">${d.score_100 == null ? "—" : d.score_100 + "%"}</td>
                        </tr>
                    </table>`;
                document.getElementById('score-summary-panel').classList.remove('hidden');
                document.getElementById('score-summary-panel').scrollIntoView({ behavior: 'smooth' });

                // Draw chart
                drawScoreChart(d.chart_data);
                showToast('评分汇总完成');
            }
        } catch(e) {
            showToast('获取评分汇总失败: ' + e.message, 'error');
        }
    }

    function drawScoreChart(chartData) {
        const canvas = document.getElementById('score-chart');
        if (!canvas || !chartData) return;
        const ctx = canvas.getContext('2d');
        const w = canvas.width, h = canvas.height;
        ctx.clearRect(0, 0, w, h);

        const { categories, full_scores, actual_scores, score_rates } = chartData;
        if (!categories || categories.length === 0) return;

        const barWidth = 30;
        const groupGap = 60;
        const padding = 60;
        const chartW = w - padding * 2;
        const chartH = h - 60;
        const maxScore = Math.max(...full_scores, 1);

        // Y axis
        ctx.strokeStyle = '#475569';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(padding, 20);
        ctx.lineTo(padding, h - 40);
        ctx.stroke();

        // X axis
        ctx.beginPath();
        ctx.moveTo(padding, h - 40);
        ctx.lineTo(w - 20, h - 40);
        ctx.stroke();

        categories.forEach((cat, i) => {
            const x = padding + 30 + i * groupGap;
            const fullH = (full_scores[i] / maxScore) * chartH;
            const actualH = (actual_scores[i] / maxScore) * chartH;

            // Full score bar (outline)
            ctx.fillStyle = 'rgba(100,116,139,0.3)';
            ctx.fillRect(x, h - 40 - fullH, barWidth, fullH);

            // Actual score bar
            const color = score_rates[i] >= 80 ? '#22c55e' : score_rates[i] >= 60 ? '#f59e0b' : '#ef4444';
            ctx.fillStyle = color;
            ctx.fillRect(x + 2, h - 40 - actualH, barWidth - 4, actualH);

            // Label
            ctx.fillStyle = '#94a3b8';
            ctx.font = '11px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(cat.substring(0, 6) + (cat.length > 6 ? '...' : ''), x + barWidth / 2, h - 10);

            // Rate
            ctx.fillStyle = '#e2e8f0';
            ctx.font = '10px sans-serif';
            ctx.fillText(score_rates[i] + '%', x + barWidth / 2, h - 40 - actualH - 8);
        });
    }

    // Step 7: Per-indicator scoring
    async function openIndicatorScoring() {
        if (!sessionId) return;
        showToast('正在加载指标体系...');
        try {
            const resp = await authFetch(`/api/agent/workflow/${sessionId}/indicators-for-scoring`);
            const data = await resp.json();
            if (data.success) {
                const indicators = data.data.indicators;
                const panel = document.getElementById('step-detail-panel');
                panel.classList.remove('hidden');
                document.getElementById('step-detail-title').textContent = '📝 逐指标评分 (Step 7)';
                document.getElementById('step-detail-content').innerHTML = `
                    <div style="max-height:500px; overflow-y:auto;">
                        ${indicators.map((ind, i) => `
                            <div style="padding:10px; margin:6px 0; background:#0f172a; border-radius:8px; border:1px solid var(--border);">
                                <div style="font-size:0.85rem; color:var(--muted);">${ind.level1_name} > ${ind.level2_name}</div>
                                <div style="font-weight:600; margin:4px 0;">${ind.level3_name} (满分: ${ind.full_score}分)</div>
                                <div style="font-size:0.8rem; color:var(--muted); margin-bottom:6px;">${ind.indicator_explanation || ''}</div>
                                <div style="display:flex; gap:8px; align-items:center;">
                                    <input type="number" id="score-${i}" placeholder="得分" min="0" max="${ind.full_score}" step="0.1" style="width:80px;">
                                    <input type="text" id="reason-${i}" placeholder="得分/扣分原因..." style="flex:1;">
                                </div>
                                ${ind.evaluation_points.length > 0 ? `
                                    <div style="margin-top:6px; font-size:0.75rem; color:var(--muted);">
                                        评价要点: ${ind.evaluation_points.join('; ')}
                                    </div>
                                ` : ''}
                            </div>
                        `).join('')}
                    </div>
                    <button class="btn btn-accent" onclick="saveIndicatorScores(${JSON.stringify(indicators).replace(/"/g, '&quot;')})" style="margin-top:12px;">💾 保存评分</button>
                    <button class="btn" onclick="generateSingleAnalysis(${JSON.stringify(indicators).replace(/"/g, '&quot;')})" style="margin-top:8px; margin-left:8px;">🤖 AI辅助分析</button>`;
                panel.scrollIntoView({ behavior: 'smooth' });
            }
        } catch(e) {
            showToast('加载指标体系失败: ' + e.message, 'error');
        }
    }

    async function generateSingleAnalysis(indicators) {
        if (!sessionId || indicators.length === 0) return;
        showToast('正在AI辅助分析...');
        // 取第一个有评分的指标进行分析
        let target = null;
        for (let i = 0; i < indicators.length; i++) {
            const scoreEl = document.getElementById('score-' + i);
            const reasonEl = document.getElementById('reason-' + i);
            const score = scoreEl ? parseFloat(scoreEl.value) || 0 : 0;
            const reason = reasonEl ? reasonEl.value.trim() : '';
            if (score > 0 || reason) { target = { ...indicators[i], score, reason, idx: i }; break; }
        }
        if (!target) { showToast('请先至少为一个指标填写评分或原因', 'error'); return; }
        try {
            const resp = await authFetch('/api/agent/workflow/generate-indicator-analysis', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionId,
                    indicator_id: target.indicator_id,
                    level1_name: target.level1_name,
                    level2_name: target.level2_name,
                    level3_name: target.level3_name,
                    indicator_explanation: target.indicator_explanation || '',
                    scoring_standard: target.scoring_standard || '',
                    score_inputs: [{ evaluation_point: target.level3_name, score: target.score, reason: target.reason }],
                }),
            });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('step-detail-content').innerHTML +=
                    `<div class="card" style="margin-top:8px; white-space:pre-wrap; font-size:0.85rem;"><strong>${target.level3_name} 分析结果：</strong><br>${data.data.analysis}</div>`;
                showToast('分析完成');
            }
        } catch(e) { showToast('分析失败: ' + e.message, 'error'); }
    }

    async function saveIndicatorScores(indicators) {
        if (!sessionId) return;
        const scores = [];
        indicators.forEach((ind, i) => {
            const scoreEl = document.getElementById('score-' + i);
            const reasonEl = document.getElementById('reason-' + i);
            const score = scoreEl ? parseFloat(scoreEl.value) || 0 : 0;
            const reason = reasonEl ? reasonEl.value.trim() : '';
            scores.push({
                indicator_id: ind.indicator_id,
                indicator_name: ind.level3_name,
                level1_name: ind.level1_name,
                level2_name: ind.level2_name,
                full_score: ind.full_score,
                actual_score: score,
                score_reason: reason,
            });
        });

        try {
            const resp = await authFetch('/api/agent/workflow/save-indicator-scores', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, scores }),
            });
            const data = await resp.json();
            if (data.success) {
                showToast('评分已保存');
                document.getElementById('step-detail-panel').classList.add('hidden');
                if (data.data.score_table) {
                    document.getElementById('report-content').textContent = data.data.score_table;
                    document.getElementById('report-area').classList.remove('hidden');
                }
            }
        } catch(e) {
            showToast('保存失败: ' + e.message, 'error');
        }
    }

    // ══════════════════════════════════════
    //  New features for Step 3 (Template Library), Step 6 (Reorder), Step 12 (Modules), Step 14 (Assembly), Export Style
    // ══════════════════════════════════════

    // --- Template Library (Step 3) ---
    async function openTemplateLibrary() {
        if (!sessionId) return;
        showToast('正在加载模板库...');
        try {
            const resp = await authFetch('/api/agent/workflow/templates');
            const data = await resp.json();
            if (data.success) {
                document.getElementById('template-list').innerHTML = data.data.map(t => `
                    <div style="display:flex; align-items:center; justify-content:space-between; padding:8px; margin:4px 0; background:#0f172a; border-radius:8px; border:1px solid var(--border);">
                        <div>
                            <strong>${t.name}</strong>
                            <span style="color:var(--muted); font-size:0.8rem; margin-left:8px;">${t.type} · ${t.indicator_count}个三级指标</span>
                            ${t.description ? `<div style="font-size:0.75rem; color:var(--muted);">${t.description}</div>` : ''}
                        </div>
                        <button class="btn btn-accent" onclick="importTemplate('${t.id}')">导入</button>
                    </div>
                `).join('') || '<p style="color:var(--muted);">暂无模板</p>';
                document.getElementById('template-panel').classList.remove('hidden');
                document.getElementById('template-panel').scrollIntoView({ behavior: 'smooth' });
            }
        } catch(e) { showToast('加载模板库失败: ' + e.message, 'error'); }
    }

    async function importTemplate(tid) {
        if (!sessionId) return;
        try {
            const resp = await authFetch('/api/agent/workflow/templates/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, template_id: tid }),
            });
            const data = await resp.json();
            if (data.success) {
                showToast('模板导入成功');
                document.getElementById('template-panel').classList.add('hidden');
            }
        } catch(e) { showToast('导入失败: ' + e.message, 'error'); }
    }

    async function saveCurrentAsTemplate() {
        if (!sessionId) return;
        const name = prompt('请输入模板名称：');
        if (!name) return;
        const type = prompt('请输入模板类型（如：项目支出/部门整体/自定义）：', '自定义') || '自定义';
        try {
            const resp = await authFetch('/api/agent/workflow/templates/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, template_name: name, template_type: type }),
            });
            const data = await resp.json();
            if (data.success) showToast('模板保存成功');
        } catch(e) { showToast('保存失败: ' + e.message, 'error'); }
    }

    // --- Indicator Reorder (Step 6) ---
    async function reorderIndicators(level, fromIdx, toIdx, parentL1, parentL2) {
        if (!sessionId) return;
        try {
            const body = { session_id: sessionId, level, from_index: fromIdx, to_index: toIdx };
            if (parentL1 !== undefined) body.parent_l1_idx = parentL1;
            if (parentL2 !== undefined) body.parent_l2_idx = parentL2;
            await authFetch('/api/agent/workflow/reorder-indicators', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch(e) { console.warn('Reorder failed:', e); }
    }

    async function moveIndicator(level, fromIdx, toIdx, parentL1, parentL2) {
        await reorderIndicators(level, fromIdx, toIdx, parentL1, parentL2);
        showToast('排序已更新');
    }

    // --- Report Assembly (Step 14) ---
    async function renderSectionOrder(sections, isCustom) {
        document.getElementById('report-assembly-panel').classList.remove('hidden');
        document.getElementById('report-assembly-panel').scrollIntoView({ behavior: 'smooth' });
        const container = document.getElementById('section-order-list');
        container.innerHTML = sections.map((s, i) => `
            <div style="display:flex; align-items:center; gap:10px; padding:8px; margin:4px 0; background:#0f172a; border-radius:8px; border:1px solid var(--border); cursor:move;" draggable="true"
                 ondragstart="dragSection(event, ${i})" ondragover="event.preventDefault()" ondrop="dropSection(event, ${i})">
                <span style="color:var(--muted);">☰</span>
                <label style="flex:1;">
                    <input type="checkbox" ${s.enabled !== false ? 'checked' : ''} onchange="window._sections[${i}].enabled = this.checked">
                    &nbsp; ${s.name}
                </label>
                ${s.has_content ? '<span style="color:#22c55e; font-size:0.75rem;">(已有内容)</span>' : '<span style="color:var(--muted); font-size:0.75rem;">(暂无内容)</span>'}
            </div>
        `).join('');
        window._sections = sections;
        window._isCustom = isCustom;
    }

    let _dragFrom = -1;
    function dragSection(e, idx) { _dragFrom = idx; }
    function dropSection(e, toIdx) {
        if (_dragFrom < 0 || _dragFrom === toIdx) return;
        const s = window._sections.splice(_dragFrom, 1)[0];
        window._sections.splice(toIdx, 0, s);
        _dragFrom = -1;
        renderSectionOrder(window._sections, true);
        // Save order
        authFetch('/api/agent/workflow/report-order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, sections: window._sections }),
        }).catch(() => {});
    }

    async function assembleReport() {
        if (!sessionId) return;
        showToast('正在组装报告...');
        const includeSections = (window._sections || []).filter(s => s.enabled !== false).map(s => s.key);
        try {
            const resp = await authFetch('/api/agent/workflow/assemble-report', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, show_title: true, include_sections: includeSections }),
            });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('report-content').textContent = data.data.report;
                document.getElementById('report-area').classList.remove('hidden');
                document.getElementById('report-area').scrollIntoView({ behavior: 'smooth' });
                showToast(`报告组装完成，共 ${data.data.section_count} 个章节`);
            }
        } catch(e) { showToast('组装失败: ' + e.message, 'error'); }
    }

    async function openReportAssembly() {
        if (!sessionId) return;
        const resp = await authFetch('/api/agent/workflow/' + sessionId + '/report-order');
        const data = await resp.json();
        if (data.success) renderSectionOrder(data.data, data.is_custom);
    }

    async function checkReportFormat() {
        if (!sessionId) return;
        showToast('正在格式校对...');
        try {
            const resp = await authFetch('/api/agent/workflow/' + sessionId + '/format-check', { method: 'POST' });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('logic-verification-content').textContent = data.data.check_result;
                document.getElementById('logic-verification-panel').classList.remove('hidden');
                document.getElementById('logic-verification-panel').scrollIntoView({ behavior: 'smooth' });
                showToast('格式校对完成');
            }
        } catch(e) { showToast('校对失败: ' + e.message, 'error'); }
    }

    // --- Export Style ---
    async function openExportStyle() {
        if (!sessionId) return;
        try {
            const resp = await authFetch('/api/agent/workflow/' + sessionId + '/export-style');
            const data = await resp.json();
            if (data.success) {
                document.getElementById('export-font').value = data.data.font_family || 'SimSun';
                document.getElementById('export-font-size').value = data.data.font_size || 12;
                document.getElementById('export-line-spacing').value = data.data.line_spacing || 1.5;
                document.getElementById('export-page-margin').value = data.data.page_margin || 2.5;
            }
        } catch(e) {}
        document.getElementById('export-style-panel').classList.remove('hidden');
        document.getElementById('export-style-panel').scrollIntoView({ behavior: 'smooth' });
    }

    async function saveExportStyle() {
        if (!sessionId) return;
        try {
            await authFetch('/api/agent/workflow/export-style', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionId,
                    font_family: document.getElementById('export-font').value,
                    font_size: parseInt(document.getElementById('export-font-size').value),
                    line_spacing: parseFloat(document.getElementById('export-line-spacing').value),
                    page_margin: parseFloat(document.getElementById('export-page-margin').value),
                }),
            });
            showToast('导出样式已保存');
        } catch(e) { showToast('保存失败: ' + e.message, 'error'); }
    }

    // --- Dynamic Module Management (Step 12) ---
    async function addCustomModule() {
        if (!sessionId) return;
        const moduleName = prompt('请输入新模块名称（如：项目合规性分析）：');
        if (!moduleName) return;
        const key = 'custom_' + Date.now();
        try {
            const resp = await authFetch('/api/agent/workflow/add-custom-module', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, module_key: key, module_name: moduleName, content: '' }),
            });
            const data = await resp.json();
            if (data.success) showToast('模块已添加');
        } catch(e) { showToast('添加失败: ' + e.message, 'error'); }
    }

    // --- Indicator Tree View (Step 6: Drag-and-drop reorder) ---
    async function openIndicatorTree() {
        if (!sessionId) return;
        showToast('正在加载指标体系...');
        try {
            const resp = await authFetch('/api/agent/workflow/' + sessionId + '/indicators-for-scoring');
            const data = await resp.json();
            if (!data.success) return;

            const indicators = data.data.indicators;
            if (!indicators || indicators.length === 0) { showToast('指标体系尚未生成'); return; }

            // Group by L1 → L2 → L3
            const tree = {};
            indicators.forEach(ind => {
                if (!tree[ind.level1_name]) tree[ind.level1_name] = {};
                if (!tree[ind.level1_name][ind.level2_name]) tree[ind.level1_name][ind.level2_name] = [];
                tree[ind.level1_name][ind.level2_name].push(ind);
            });

            let html = '<div style="max-height:550px; overflow-y:auto;">';
            let l1Idx = 0;
            for (const [l1Name, l2Map] of Object.entries(tree)) {
                html += `<div style="margin:8px 0; padding:8px; background:#1e293b; border-radius:8px; border:1px solid var(--accent);">
                    <div style="display:flex; align-items:center; gap:8px;">
                        <span style="cursor:pointer; color:var(--muted);" onclick="moveIndicator('level1',${l1Idx},${l1Idx-1})" title="上移">▲</span>
                        <span style="cursor:pointer; color:var(--muted);" onclick="moveIndicator('level1',${l1Idx},${l1Idx+1})" title="下移">▼</span>
                        <strong style="color:var(--accent);">${l1Name}</strong>
                    </div>`;
                let l2Idx = 0;
                for (const [l2Name, l3List] of Object.entries(l2Map)) {
                    const l2FullScore = l3List.reduce((s, i) => s + i.full_score, 0);
                    html += `<div style="margin:4px 0 4px 20px; padding:6px; background:#0f172a; border-radius:6px; border:1px solid var(--border);">
                        <div style="display:flex; align-items:center; gap:8px;">
                            <span style="cursor:pointer; color:var(--muted);" onclick="moveIndicator('level2',${l2Idx},${l2Idx-1},${l1Idx})" title="上移">▲</span>
                            <span style="cursor:pointer; color:var(--muted);" onclick="moveIndicator('level2',${l2Idx},${l2Idx+1},${l1Idx})" title="下移">▼</span>
                            <strong>${l2Name}</strong> <span style="color:var(--muted); font-size:0.75rem;">(${l2FullScore}分)</span>
                        </div>`;
                    l3List.forEach((l3, l3Idx) => {
                        html += `<div style="margin:2px 0 2px 20px; padding:4px; font-size:0.8rem; display:flex; align-items:center; gap:6px;">
                            <span style="cursor:pointer; color:var(--muted);" onclick="moveIndicator('level3',${l3Idx},${l3Idx-1},${l1Idx},${l2Idx})" title="上移">▲</span>
                            <span style="cursor:pointer; color:var(--muted);" onclick="moveIndicator('level3',${l3Idx},${l3Idx+1},${l1Idx},${l2Idx})" title="下移">▼</span>
                            <span style="flex:1;">${l3.level3_name}</span>
                            <span style="color:var(--muted);">${l3.full_score}分</span>
                        </div>`;
                    });
                    html += `</div>`;
                    l2Idx++;
                }
                html += `</div>`;
                l1Idx++;
            }
            html += '</div>';

            document.getElementById('step-detail-title').textContent = '🔀 指标体系排序 (Step 6)';
            document.getElementById('step-detail-content').innerHTML = html;
            document.getElementById('step-detail-panel').classList.remove('hidden');
            document.getElementById('step-detail-panel').scrollIntoView({ behavior: 'smooth' });
        } catch(e) { showToast('加载指标体系失败: ' + e.message, 'error'); }
    }

    