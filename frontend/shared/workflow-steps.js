/* 云海睿评 · 工作流步骤状态（批次 55）
 *
 * 为什么单独一个模块：步骤状态原来只有 `completedSteps: Set`，`renderStepList()`
 * 只画 completed / pending 两种。于是「正在运行」这个态只活在两处 DOM 补丁里，
 * 任何人调一次全量重画（pollStatus 每 5 秒调一次）就把它抹成 ⏳ ——
 * 用户看到的就是「运行中点别的步骤再点回来，运行中的那行变成未运行」。
 *
 * 这里只放**纯函数**：不碰 DOM、不读全局，node 里能直接跑（tests/test_workflow_steps.cjs）。
 * 页面负责把它们接到渲染上。
 */
(function (global) {
    'use strict';

    var ICONS = {
        completed: '✅',
        running: '🔄',
        awaiting: '⏸️',
        error: '❌',
        pending: '⏳'
    };

    var LABELS = {
        completed: '已完成',
        running: '生成中',
        awaiting: '待确认',
        error: '失败',
        pending: '未开始'
    };

    function stepIcon(status) {
        return ICONS[status] || ICONS.pending;
    }

    function stepLabel(status) {
        return LABELS[status] || LABELS.pending;
    }

    /*: 合并单个步骤的新旧状态。completed 是终态，任何后续事件都不能把它降级；
     *  其余（awaiting / error）都可以被一次新的 running 覆盖 —— 那是重试，不是回退。 */
    function mergeStatus(current, incoming) {
        if (!incoming) return current || 'pending';
        if (current === 'completed') return 'completed';
        return incoming;
    }

    /*: 由 `/workflow/{id}/status` 的 data 推出全量状态。
     *
     * 服务端**没有** "running" 这个字段：它只给 `completed_steps`、`current_step`
     * 与会话 `status`。所以「当前步在跑 / 在等确认 / 已失败」要从这三者推，
     * 这正是不该被"只认 completed/pending"的全量重画冲掉的原因。
     *
     * 规则（顺序有意义）：
     *   1. 在 completed_steps 里 → completed（最高优先，永不回退）
     *   2. 是 current_step → 按会话状态给 running / awaiting / error
     *   3. 其它步骤 → pending
     *
     * 第 3 条以服务端为准：它没把这个步骤算进 completed_steps、也不是当前步，
     * 那就是没开始（或步骤被回退/重跑了）。本地残留的 running 不能当真 ——
     * 否则续跑之后会有一行永远转圈。
     *
     * 之所以敢完全信服务端：`/status` 的 `current_step` 优先取内存里的
     * `progress_bus`（`_tracked_node` 在步骤开始时就写了），不会落后于 SSE。
     */
    function statesFromStatus(total, completedSteps, currentStep, options) {
        var sessionStatus = (options && options.status) || 'running';
        var done = Object.create(null);
        (completedSteps || []).forEach(function (n) { done[Number(n)] = true; });
        var current = Number(currentStep) || 0;
        var out = {};
        for (var n = 1; n <= total; n++) {
            if (done[n]) {
                out[n] = 'completed';
            } else if (n === current) {
                if (sessionStatus === 'failed') out[n] = 'error';
                else if (sessionStatus === 'awaiting_approval' || sessionStatus === 'paused') out[n] = 'awaiting';
                else out[n] = 'running';
            } else {
                out[n] = 'pending';
            }
        }
        return out;
    }

    /*: 滚动容器是否「贴底」。用户往上翻在读旧内容时不要把他拽回去。
     *  `gap` 留 24px 容差：行高/内边距会让 scrollTop 差几个像素。 */
    function shouldAutoFollow(el, threshold) {
        if (!el) return true;
        var limit = threshold == null ? 24 : threshold;
        var gap = el.scrollHeight - el.scrollTop - el.clientHeight;
        return gap <= limit;
    }

    global.WorkflowSteps = {
        stepIcon: stepIcon,
        stepLabel: stepLabel,
        mergeStatus: mergeStatus,
        statesFromStatus: statesFromStatus,
        shouldAutoFollow: shouldAutoFollow
    };
})(window);
