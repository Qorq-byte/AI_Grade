/* 云海睿评 · 前端共享配置
 * 零构建：通过 <script> 引入，挂载到 window.APP_CONFIG。
 * 约定：所有前端常量（轮询间隔、状态映射、步骤名、缓存 TTL、超时）都放这里，
 *       页面内不得再硬编码这些数字。
 */
(function (global) {
    'use strict';

    var APP_CONFIG = {
        version: 'r3.0.0',

        /* 缓存：模块数据在 TTL 内复用，避免切换模块重复拉取 */
        cache: {
            defaultTTL: 30000,
            shortTTL: 10000
        },

        /* 轮询：由统一调度器分配，document.hidden 时暂停 */
        poll: {
            schedulerTick: 10000,
            notifications: 30000,
            token: 30000,
            chatStatus: 30000,
            workflowFallback: 5000,
            statusFallback: 5000
        },

        /* 请求层 */
        api: {
            timeout: 30000,
            longTimeout: 120000,
            maxRetries: 2,
            retryBaseDelay: 400,
            retryMaxDelay: 4000,
            refreshPath: '/api/auth/refresh',
            loginPath: '/login'
        },

        /* SSE 渲染 */
        sse: {
            renderInterval: 50,
            heartbeatTimeout: 45000,
            replayWindow: 200
        },

        /* 工作流步骤（与后端 14 步对齐；names[0] 占位为「初始化」） */
        steps: {
            total: 14,
            names: [
                '初始化',
                '整理所需资料清单',
                '提取核心有效信息',
                '构建绩效评价指标体系',
                '生成各指标分值',
                '生成各指标评分标准',
                '生成评价指标体系表',
                '生成指标得分及分析',
                '总结主要经验做法',
                '分析存在问题及原因',
                '提出改进建议',
                '综合评价分析及结论',
                '生成基本情况',
                '生成评价工作简介',
                '组装最终报告'
            ],
            shortNames: {
                1: '整理资料清单', 2: '核心有效信息提取', 3: '构建指标体系',
                4: '生成指标分值', 5: '生成评分标准', 6: '评价指标体系表',
                7: '指标得分分析', 8: '总结经验做法', 9: '分析问题及原因',
                10: '提出改进建议', 11: '综合评价分析', 12: '基本情况',
                13: '评价工作简介', 14: '组装最终报告'
            },
            approvalSteps: [3, 6, 11]
        },

        /* 工作流状态 → 展示文案 */
        workflowStatus: {
            labels: {
                running: '执行中',
                retrying: '重试中',
                awaiting_approval: '等待人工确认',
                paused: '等待人工确认',
                completed: '已完成',
                failed: '失败',
                not_found: '未找到'
            },
            /* 这些状态仍属于「进行中」，需要继续监听 */
            active: ['running', 'retrying', 'awaiting_approval', 'paused']
        },

        /* 报告等级阈值（统一评分口径） */
        grade: {
            excellent: 90,
            good: 80,
            fair: 70,
            pass: 60
        },

        /* 接口路径 */
        routes: {
            refresh: '/api/auth/refresh',
            login: '/login',
            workflowStatus: '/api/agent/workflow/',
            client: '/client'
        }
    };

    APP_CONFIG.steps.approvalSteps = APP_CONFIG.steps.approvalSteps || [];
    global.APP_CONFIG = APP_CONFIG;

    /* 兼容旧代码里的 C.STEP_NAMES / C.APPROVAL_STEPS */
    global.C = global.C || {
        STEP_NAMES: APP_CONFIG.steps.names,
        APPROVAL_STEPS: APP_CONFIG.steps.approvalSteps
    };
})(window);
