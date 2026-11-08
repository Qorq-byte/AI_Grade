# API 参考

> 除 `/health`、`/api/auth/login`、`/api/auth/register` 外，所有接口都需要
> `Authorization: Bearer <access_token>`。
>
> 响应统一为 `{"success": bool, "data": ..., "message": "..."}`；错误体额外带
> `request_id`，与响应头 `X-Request-ID`、日志第三段一致，可直接定位全链路。

- [认证 `/api/auth`](#认证-apiauth)
- [工作流 `/api/agent`](#工作流-apiagent)
- [问卷](#问卷)
- [评分与分析](#评分与分析)
- [报告组装与导出](#报告组装与导出)
- [客户端 `/api/client`](#客户端-apiclient)
- [管理端 `/api/admin`](#管理端-apiadmin)
- [SSE 流式协议](#sse-流式协议)

---

## 认证 `/api/auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册（username / password / phone） |
| POST | `/api/auth/login` | 登录，返回 access_token + refresh_token |
| POST | `/api/auth/refresh?refresh_token_str=xxx` | 刷新访问令牌 |
| GET | `/api/auth/me` | 当前用户信息 |
| POST | `/api/auth/admin/create-user` | 管理员创建用户 |
| GET | `/api/auth/users` | 用户列表 |
| POST | `/api/auth/users/{id}/toggle-status` | 切换用户状态 |
| GET | `/api/auth/init-admin` | 初始化默认管理员 |

登录示例：

```http
POST /api/auth/login
Content-Type: application/json

{"username": "admin", "password": "Admin@123456"}
```

```json
{
  "success": true,
  "message": "登录成功",
  "data": {
    "user_id": 1,
    "username": "admin",
    "role": "admin",
    "tokens_used": 198,
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
  }
}
```

---

## 工作流 `/api/agent`

### 启动与状态

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/workflow/start-stream` | **前端主用**，SSE 流式执行 14 步 |
| POST | `/api/agent/workflow/start` | 同步启动（兼容旧调用，耗时长） |
| GET | `/api/agent/workflow/{session_id}/status` | 实时状态：步骤摘要、审批、覆盖度、评分台账、一致性 |
| GET | `/api/agent/workflow/{session_id}/stream` | 断线续传，支持 `Last-Event-ID` |
| GET | `/api/agent/workflow/{session_id}/report` | 完整报告（重启后可恢复） |
| POST | `/api/agent/workflow/save-report` | 保存编辑后的报告 |
| POST | `/api/agent/workflow/approve` | 人工确认节点（默认自动放行） |
| POST | `/api/agent/workflow/refine` | AI 对话优化指定步骤 |
| POST | `/api/agent/workflow/{session_id}/consistency-check` | 按需重跑步骤间一致性检查 |

启动请求体：

```json
{
  "project_name": "测试项目",
  "file_paths": ["D:/AI_Grade/uploads/xxx.txt"],
  "model_name": "deepseek-chat",
  "comparison_mode": "single",
  "temperature_profile": "normal",
  "top_p": 0.9,
  "indicator_type": "项目支出指标体系"
}
```

### 文件与导出

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/upload` / `/api/agent/upload/batch` | 单文件 / 批量上传 |
| POST | `/api/agent/workflow/export` | 导出完整报告或单步骤（word / pdf） |
| GET | `/api/agent/download/{file_name}` | 下载导出文件 |

导出为幂等操作：同一用户 + 同一会话 + 同一内容 + 同一排版 + 同一格式，
10 分钟内只渲染一次，响应 `data.reused = true`。

---

## 问卷

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/workflow/questionnaire` | 基于指标体系生成里克特 5 级量表 |
| GET | `/api/agent/workflow/questionnaire/list` | 问卷列表 |
| POST | `/api/agent/workflow/questionnaire/submit` | 提交答卷 |
| GET | `/api/agent/workflow/{session_id}/questionnaire/answers` | 查看答卷 |
| DELETE | `/api/agent/workflow/questionnaire/{session_id}` | 删除问卷 |
| POST | `/api/agent/workflow/questionnaire/batch-delete` | 批量删除 |

---

## 评分与分析

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent/workflow/{session_id}/indicators-for-scoring` | 待评分指标 |
| POST | `/api/agent/workflow/generate-indicator-analysis` | AI 辅助生成指标分析 |
| POST | `/api/agent/workflow/save-indicator-scores` | 保存逐指标评分并刷新台账 |
| GET | `/api/agent/workflow/{session_id}/score-summary` | 评分汇总与等级判定 |
| GET | `/api/agent/workflow/{session_id}/extract-deductions` | 提取扣分点 |
| POST | `/api/agent/workflow/confirm-deductions` | 确认扣分点 |
| POST | `/api/agent/workflow/{session_id}/verify-logic` | 逻辑一致性校对 |
| POST | `/api/agent/workflow/set-language-style` | 语言风格（中立 / 尖锐 / 委婉） |
| POST | `/api/agent/workflow/{session_id}/perspective` | 多视角分析 |

`score-summary` 同时返回 `disposition`，即逐指标处置台账：

```json
{
  "total": 23,
  "counts": {
    "accepted": 15,
    "model_pending": 4,
    "anchor_weak": 3,
    "anchor_unresolved": 0,
    "score_invalid": 0,
    "reason_missing": 1,
    "not_mentioned": 0
  },
  "accepted_score": 30.0
}
```

`anchor_weak` 表示「引用能定位到原文某一行，但该行未进入本步检索证据」，
**不计入总分**，只作为人工确认线索。

---

## 报告组装与导出

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/agent/workflow/{session_id}/report-order` | 获取 / 保存章节顺序 |
| POST | `/api/agent/workflow/assemble-report` | 按自定义顺序组装报告 |
| POST | `/api/agent/workflow/{session_id}/format-check` | 格式校对 |
| GET/POST | `/api/agent/workflow/{session_id}/export-style` | 导出样式（字体 / 字号 / 行距 / 页边距） |
| GET | `/api/agent/workflow/templates` | 指标体系模板库 |
| POST | `/api/agent/workflow/templates/import` / `save` | 导入 / 保存模板 |
| POST | `/api/agent/workflow/reorder-indicators` | 指标拖拽排序 |
| GET | `/api/agent/workflow/{session_id}/modules` | 步骤 12 模块列表 |
| POST | `/api/agent/workflow/generate-module` | 生成模块 |
| POST | `/api/agent/workflow/add-custom-module` | 新增自定义模块 |
| POST | `/api/agent/workflow/update-module-content` | 更新模块内容 |
| POST | `/api/agent/workflow/delete-module` | 删除模块 |

---

## 客户端 `/api/client`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/client/me` | 当前用户信息（含统计） |
| PUT | `/api/client/profile` | 修改用户名 |
| PUT | `/api/client/password` | 修改密码 |
| POST | `/api/client/avatar` | 上传头像 |
| DELETE | `/api/client/account` | 注销账号（级联删除数据） |
| GET | `/api/client/dashboard` | 仪表盘 |
| GET | `/api/client/activity` | 最近活动 |
| GET | `/api/client/reports` | 报告列表（含内存 / 备份恢复） |
| DELETE | `/api/client/reports/{session_id}` | 删除报告 |
| POST | `/api/client/reports/batch-delete` | 批量删除 |
| GET | `/api/client/token-stats` | Token 统计 |
| GET | `/api/client/notifications` | 通知列表 |
| PUT | `/api/client/notifications/{id}/read` | 标记已读 |
| PUT | `/api/client/notifications/read-all` | 全部已读 |
| DELETE | `/api/client/notifications/{id}` | 删除单条 |
| DELETE | `/api/client/notifications` | 清空 |
| POST | `/api/client/ai/chat` | AI 聊天 |
| POST | `/api/client/ai/voice-chat` | 语音聊天（form 提交识别文本） |
| GET | `/api/client/ai/chat-status` | AI 聊天开关 |
| GET | `/api/client/ai/models` | 可用模型（按密钥配置过滤） |
| GET/POST | `/api/client/ai/conversations` | 对话列表 / 新建 |
| GET/PUT/DELETE | `/api/client/ai/conversations/{id}` | 详情 / 改名 / 删除 |

---

## 管理端 `/api/admin`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/dashboard` | 仪表盘（总览 + 最近操作） |
| GET | `/api/admin/token-stats` | Token 趋势 / 分布 / Top 用户 |
| GET | `/api/admin/ai-chat-stats` | AI 对话统计 |
| GET | `/api/admin/settings` | 系统设置 |
| POST | `/api/admin/settings/ai-chat-toggle` | AI 聊天开关 |
| POST | `/api/admin/settings/rating-thresholds` | 评分等级阈值 |
| GET/POST/PUT/DELETE | `/api/admin/users` | 用户管理 |
| GET/POST/PUT/PATCH/DELETE | `/api/admin/models` | 模型管理 |
| GET/POST/PUT/DELETE | `/api/admin/prompts` | 提示词管理（含 granular / defaults / reset） |
| GET/POST/PUT/DELETE | `/api/admin/knowledge` | 知识库管理 |
| GET | `/api/admin/workflows` | 工作流监控 |
| GET | `/api/admin/audit-logs` | 审计日志 |
| GET | `/api/admin/audit-logs/summary` | 审计摘要 |
| GET | `/api/admin/file-stats` | 文件统计 |
| GET | `/api/admin/step-config` | 步骤配置总览 |
| GET/POST/PUT/DELETE | `/api/admin/module-sequences` | 报告模块排序 |
| GET/POST | `/api/admin/notifications` | 通知发送 / 记录 |
| DELETE | `/api/admin/notifications/by-title` | 按标题撤回 |
| GET | `/api/admin/cleanup/preview` | 清理预览 |
| POST | `/api/admin/cleanup/execute` | 执行清理 |
| POST | `/api/admin/cleanup/fix-failed` | 清理卡住的工作流 |

---

## SSE 流式协议

`POST /api/agent/workflow/start-stream` 返回 `text/event-stream`：

```
id: 42
event: step_complete
data: {"step_number": 3, "step_name": "构建绩效评价指标体系", "content_preview": "..."}

: heartbeat
```

| 事件 | 说明 |
|------|------|
| `step_start` | 步骤开始 |
| `step_complete` | 步骤完成（含 `content` 与 `content_preview`） |
| `token_delta` | token 级增量（前端按 50ms 合并渲染） |
| `token_update` | 累计 token 用量 |
| `questionnaire_ready` | 步骤 6 完成，可生成问卷 |
| `complete` | 工作流完成（含 `final_report_preview`） |
| `error` | 错误信息（含 `request_id`） |

协议约定：

- 每帧带自增 `id`（seq），客户端按 id 去重，乱序直接丢弃；
- 断线后带 `Last-Event-ID` 请求 `GET /workflow/{id}/stream`，服务端补发缺失帧；
- 心跳为注释帧（`: heartbeat`），避免被代理当作空闲连接断开；
- 服务端设置 `X-Accel-Buffering: no`，Nginx 需配合 `proxy_buffering off;`。
