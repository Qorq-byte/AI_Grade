async function readWorkflowEvents(response, onEvent) {
    if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw new Error(error?.detail || 'HTTP ' + response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let terminal = false;
    const seen = new Set();
    try {
        while (true) {
            const { done, value } = await reader.read();
            buffer += decoder.decode(value, { stream: !done });
            const lines = buffer.split('\n');
            buffer = done ? '' : lines.pop();
            for (const line of lines) {
                if (!line.startsWith('data:')) continue;
                const event = JSON.parse(line.slice(5));
                if (['step_start', 'step_complete', 'token_update'].includes(event.event)) {
                    const key = [event.event, event.step_number, event.version ?? 1].join(':');
                    if (seen.has(key)) continue;
                    seen.add(key);
                }
                onEvent(event);
                if (['complete', 'error', 'waiting_approval'].includes(event.event)) terminal = true;
            }
            if (done) break;
        }
    } finally {
        reader.releaseLock();
    }
    if (!terminal) throw new Error('工作流连接已断开，请在「我的报告」查看执行状态');
}

const workflowStatusLabels = {
    running: '执行中', retrying: '重试中', awaiting_approval: '等待人工确认',
    paused: '等待人工确认', completed: '已完成', failed: '失败',
};
const workflowSessionKey = 'aiGradeWorkflowSession';

function newWorkflowSession() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
}

async function watchWorkflowStatus(sessionId, request, onStatus, onDisconnect, signal) {
    let afterStep = 0;
    while (!signal.aborted) {
        try {
            const response = await request('/api/agent/workflow/' + encodeURIComponent(sessionId)
                + '/status?after_step=' + afterStep, { signal });
            if (signal.aborted) return;
            if ([401, 403, 404].includes(response.status)) {
                onStatus({ status: 'not_found' });
                return;
            }
            if (!response.ok) throw new Error('状态请求失败');
            const { data } = await response.json();
            if (signal.aborted) return;
            onStatus(data);
            afterStep = Math.max(afterStep, ...data.completed_steps);
            if (!['running', 'retrying'].includes(data.status)) return;
        } catch (error) {
            if (signal.aborted) return;
            onDisconnect();
        }
        await new Promise(resolve => setTimeout(resolve, 1500));
    }
}
