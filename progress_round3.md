# 第三轮优化 · 进度记录（Batch 1–15）

本轮按《优化提示词_第三轮_全面优化.md》执行，遵守「分批交付、基线优先、向后兼容」纪律。
已完成**阶段一（前端模块化）主体**与**阶段二（连接契约与传输层）的核心 SSE 部分**，
另外顺带完成阶段四第 1 项、阶段五的循环依赖与配置一致性小项。

> 结论先行：接口路径、SSE 既有事件名与语义、数据库字段均未变更；新增 `token_delta`、
> `id/seq`、续传端点与心跳格式，老客户端可静默忽略。

---

## 1. 完成度总览

| 阶段 | 子项 | 状态 |
|---|---|---|
| 一 | 1. 抽取 `frontend/shared/`（api/store/ui/config） | ✅ 完成 |
| 一 | 2. `switchTab` 生命周期 + hash + 骨架屏 | ✅ 完成 |
| 一 | 3. 删除调试角标与版本串 | ✅ 完成 |
| 一 | 4. 三个 30s 轮询合并为统一调度器 | ✅ 完成 |
| 一 | 5. 消除 client/workflow 重复的 SSE 消费代码 | ⚠️ 部分（SSE 已共用，页面 UI 逻辑未合并） |
| 一 | 6. 列表分页 / 增量渲染 | ⚠️ 部分（缓存 + 骨架屏已做，分页未做） |
| 一 | 7. 可访问性 | ⚠️ 部分（Toast 播报、确认框键盘化、导航 role/keydown；内联 onclick 未全量迁移） |
| 二 | 1. SSE：`token_delta` / `id:seq` / `Last-Event-ID` / 心跳 / 节流渲染 | ✅ 完成 |
| 二 | 2. 状态查询去轮询化 | ⚠️ 部分（兜底间隔 1.5s → 5s；未实现 304 版本号） |
| 二 | 3. API 契约统一 + OpenAPI 快照 + 导出幂等 | ⚠️ 部分（导出幂等 + request_id 贯通已完成；统一响应包与 OpenAPI 快照未做） |
| 二 | 4. 上传解析复用 | ❌ 未开始 |
| 二 | 5. 错误语义区分 retryable/terminal | ✅ 完成（请求层分类 + 页面提示按语义分流 + 错误体带 request_id） |
| 三 | 评估集与基线（`tests/rag_eval/`） | ✅ 完成（29 样本 / Recall@3=0.172 / 查询无关性=1.0） |
| 三 | 查询感知检索（BM25 排序） | ✅ 完成（Recall@3 0.172 → 0.414；瓶颈定位到「查询」） |
| 三 | 每步取证面软融合（query plan） | ⚠️ 小胜（0.414 → 0.448，收益约 1 条样本；槽位才是硬约束） |
| 三 | 向量混合 / 结构化切块 / 动态槽位 | ❌ 未开始（8.4 已给出成本收益口径） |
| 四 | 1. 温度档位真正生效 | ✅ 完成 |
| 四 | 2. 长文档覆盖度透明化 | ✅ 完成（分块定位 + 头尾预算 + 覆盖度声明 + 未引用部分摘要） |
| 四 | 3. 结构化输出 | ✅ 完成（形状校验 + 一次修复重试 + `manual_review` 保留原文） |
| 四 | 报告自检：步骤间一致性（本批新增，非任务书条目） | ✅ 完成（纯程序、零 token；17 类 issue；报告页徽标 + 按需重跑接口） |
| 四 | 内容索引真正生效（Batch 10 新增） | ✅ 完成（步骤 3/7/9 从「看不到资料」变为「按类取用」；注入加预算；+46% 输出 tokens 的代价已记录） |
| 四 | 步骤7评分落地可解释（Batch 11 新增） | ✅ 完成（引用锚点五类解析 + 6 类处置台账 + 体系外 id 单列；同一备份重放 2/23→7/23、2/25→6/25、3/23→15/23。**但跑间差异远大于改动收益，详见 B11.7**） |
| 四 | 步骤7弱定位独立成项（Batch 12 新增） | ✅ 完成（第 6 类锚点 `material_locator` + 第 7 类状态 `anchor_weak`，**不计入总分**；标题改从原文抽，不再把自己生成的表格标题当证据。同一备份重放：`b425eb90` 1/20→3/20 且总分仍等于备份真实值 11.0） |
| 四 | 3. 结构化修复不得缩水（Batch 13 新增） | ✅ 完成（回显上限步骤 7 单独 24,000；提示词不再教删条目；`repair_shrunk` 拦缩水并保留原文；首次正文留档 `raw_content`。历史备份：`ok` 组正文最短 5,751 字、`repaired` 组最长 3,218 字，**零重叠**；真实 14 步验证「格式修好、条目没少」的正常修复未被误伤） |
| 四 | 3. 缩水闸门不能自带盲区（Batch 14 新增） | ✅ 完成（基准改为原文 JSON 条目，认 dict/list/扁平三形态；加「可计分数值分不得减少」；判不了置 `unverifiable` 并写 warning。22 份备份新旧口径对比只变 1 份，且是**修正真实误伤**：`11b16a29` 的 15 条数组形态旧规则数成 0 → 误判缩水。真实 26 条/6 分值、12,608 字→2,745 字的修复未被误伤） |
| 二 | 备份文件读写竞态（Batch 15 新增，B14.5 挂账） | ✅ 完成（写/删加 2 秒退避重试，问卷列表读路径改走持锁入口；双进程对打 162/400→0/400 次 `WinError 5`；那条「偶发 500」可在 12 秒内稳定复现了） |
| 四 | 4/5/6/8/9/10 | ❌ 未开始 |
| 四 | 7. 模型调用韧性 | ⚠️ 部分（流式 usage 优先上游、无 usage 标注 estimated 不落假数据；降级链与分级超时未做） |
| 五 | 1. 解耦 agent → routes 循环依赖 | ✅ 完成（`progress_bus`） |
| 五 | 5. 配置一致性（DEFAULT_MODEL、Redis 等待） | ✅ 完成 |
| 五 | 2/3/4/6/7 | ❌ 未开始 |

---

## 2. 改动清单与理由

### 新增文件

| 文件 | 作用 |
|---|---|
| `frontend/shared/config.js` | 唯一常量源：轮询间隔、超时、重试、缓存 TTL、14 步名称/审批步、状态文案、等级阈值 |
| `frontend/shared/store.js` | 极简状态容器：TTL 复用、`remember()` 合并并发、`invalidate/clear`、可选 sessionStorage 持久化 |
| `frontend/shared/ui.js` | `esc / showToast / announce / renderSkeleton / emptyState / confirmDialog / openModal / throttle`，样式内联注入一次 |
| `frontend/shared/api.js` | 统一请求层：30s/120s 超时、仅 GET 指数退避重试（≤2 次）、同 URL 并发去重、401→refresh→重放、错误对象 `{code,message,retryable,status,requestId}`、`X-Request-ID` 透传 |
| `frontend/shared/workflow-controller.js` | client 与 workflow 共用的 SSE 消费：`id/seq` 解析与去重、`Last-Event-ID` 续传、心跳、`token_delta` 50ms 合并、状态观测 |
| `backend/services/progress_bus.py` | 进程内进度总线：token 增量队列、SSE 帧环形缓冲（400 帧）、消费者计数、`ProgressProxy`（抗模块 reload） |
| `tests/test_shared_frontend.cjs` | 10 项：store TTL/并发/持久化、api 重试/不重试/刷新重放/去重/超时/4xx |
| `tests/test_client_modules.cjs` | 4 项：class-only 切换、30 次往复不重复请求、离开即取消、hash 恢复 |
| `tests/test_progress_bus.py` | 6 项：增量门控、帧补发、序号单调、代理抗 reload |

### 修改文件

| 文件 | 改动 |
|---|---|
| `frontend/client/index.html` | 接入共享层；重写 `switchTab`（生命周期/hash/骨架/取消）；删除 `jsStatusTag` 与 `jsErrorBanner`；轮询合并；报告/问卷/仪表盘走缓存与骨架屏；`confirm()` → `confirmDialog`；接入 `token_delta` 打字机渲染；工作流结束时失效缓存 |
| `frontend/workflow/index.html` | 改载共享控制器；`token_delta` 实时追加到步骤详情面板 |
| `frontend/login/index.html` | 始终保存 `refresh_token`（会话级），使 401 自动刷新可用 |
| `frontend/workflow/sse.js` | **删除**，实现迁至 `shared/workflow-controller.js`（两页共用同一份代码） |
| `backend/routes/agent_routes.py` | SSE 帧带 `id/seq`；新增 `token_delta` 转发；心跳改 `: ping {ts}`（仅空闲时发）；新增 `GET /workflow/{session_id}/stream` 续传端点；`_WORKFLOW_PROGRESS` 改指向 `progress_bus.PROXY`；轮询间隔 0.25s→0.1s |
| `backend/agents/performance_agent.py` | 新增 `_invoke_llm`（有消费者时走 `llm.stream()` 推增量，失败回退非流式）、`_chunk_text`、`_record_usage`；温度档位映射到实例参数；解除对 routes 的反向 import |
| `backend/services/progress_bus.py` | 见上 |
| `backend/config.py` | `DEFAULT_MODEL` 统一为 `deepseek-chat`（与 `.env.example`、README 一致） |
| `docker-entrypoint.sh` | Redis 等待改为「配置了 `REDIS_URL` 才等待」，与「当前单进程总线」的事实对齐 |
| `tests/test_engineering_regressions.py` | 隔离夹具改用 `progress_bus.PROGRESS.clear()`；心跳断言随协议更新；新增温度档位用例 |
| `tests/test_workflow_sse.cjs` | 加载路径迁移到共享控制器；新增 `id` 去重、增量合并、续传 3 项 |

---

## 3. 量化对比

| 指标 | 改造前 | 改造后 | 依据 |
|---|---|---|---|
| 连续切换模块 30 次（reports↔dashboard） | 每次进入都重新拉全量 | 报告列表仅 **1 次**，dashboard/activity 各 1 次 | `tests/test_client_modules.cjs` 断言 |
| 可见性切换 | 三个 30s 定时器照常跑 | 统一调度器，`document.hidden` 暂停、恢复立即补跑 | 代码 + `PollScheduler` |
| SSE 首字延迟 | 仅步骤级（0.25s 轮询，整步完成后才有内容） | **token 级**（`token_delta` 实测逐字到达，前端 50ms 合并渲染） | 实机探测（见第 4 节） |
| 断线恢复 | 只能轮询状态接口，缺事件不可重放 | `Last-Event-ID` 补发缓冲帧 + 续接期间完成的步骤 | 实机探测：断开时 id=2，续传补回 3–7 与 `step_complete` |
| 请求超时/重试 | 无超时、无重试，401 直接跳登录 | 30s/120s 超时、GET 重试 ≤2、401→refresh→重放 | `tests/test_shared_frontend.cjs` |
| token 用量 | `len(content)//3` 估算后落库 | 流式优先取上游 `usage_metadata`；无 usage 时标注 `estimated` 且**不落假数据** | `_record_usage` |
| 温度档位 | 仅影响提示词文案 | 严谨 0.3/0.5、普通 0.7/0.9、发散 0.9/0.95 落到 LLM 实例 | `test_temperature_profile_reaches_llm_instance` |

测试基线（本轮改完后）：

```
pytest tests/           41 passed   （基线 34；新增 progress_bus 6 + 温度档位 1）
node --test tests/*.cjs 22 passed   （基线 5；SSE 5→8，新增共享层 10、模块切换 4）
test_all_apis.py        46/46        （与基线一致）
frontend_e2e_check.py   63/63        （与基线一致）
wf_e2e.py               35/35        （与基线一致，真实 14 步编排）
bash -n docker-entrypoint.sh        通过
git diff --check                    通过
```

---

## 4. 可重跑命令

```bash
# 单元 / 回归（无需服务器）
.venv/Scripts/python.exe -m pytest -q tests/
node --test tests/test_workflow_sse.cjs tests/test_shared_frontend.cjs tests/test_client_modules.cjs

# 需要先启动服务器（本机有代理时务必绕过）
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/Scripts/python.exe test_all_apis.py
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/Scripts/python.exe frontend_e2e_check.py
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/Scripts/python.exe wf_e2e.py
```

SSE 实机观察（本轮使用过的探针，需有效模型配置）：

```
': connected'
'id: 1'  data: {"event":"step_start","step_number":1,...}
'id: 2'  data: {"event":"token_delta","step_number":1,"delta":"⚠",...}
'id: 3'  data: {"event":"token_delta","step_number":1,"delta":"️",...}
...
# 断开后 12 秒再用 Last-Event-ID: 2 续传
补发帧 id: [3,4,5,6,7,1→续接序号] 事件: token_delta×5, step_complete
```

---

## 5. 回滚方式

- 前端：`git checkout -- frontend/client/index.html frontend/workflow/index.html frontend/login/index.html frontend/workflow/sse.js`
  并删除 `frontend/shared/`；页面恢复为内联实现（`/static/workflow/sse.js` 引用也随之一并恢复）。
- 后端：`git checkout -- backend/routes/agent_routes.py backend/agents/performance_agent.py backend/config.py docker-entrypoint.sh`
  并删除 `backend/services/progress_bus.py`。旧客户端不识别新事件，但恢复后不再发送，互不影响。
- 测试：新增的 4 个测试文件可直接删除；被修改的两处断言随代码一起回滚。

---

## 6. 兼容性说明

- **未改任何 API 路径、请求字段、数据库表结构**。
- SSE 新增 `token_delta` 与 `id:` 行；老前端只解析 `data:` 行，遇到未知 `event` 会走默认分支（不抛错）。
- 心跳注释由 `: heartbeat` 改为 `: ping {ts}`，二者都是 SSE 注释帧，浏览器不会当作事件分发给业务代码。
- 新增端点 `GET /api/agent/workflow/{session_id}/stream` 需要 Bearer 令牌与归属校验（匿名 401、他人 404）。
- `progress_bus.PROXY` 让 `agent_routes._WORKFLOW_PROGRESS` 在模块 reload 后依然指向当前实例；测试隔离改为 `progress_bus.PROGRESS.clear()`。

---

## 7. 剩余风险与未验证项（诚实列出）

1. **token_delta 来源与任务书不同**：任务书要求来自 LangGraph `astream_events` 的 `on_chat_model_stream`。
   本轮工作流仍以同步 `app.invoke()` 在 executor 线程执行，改为 `astream_events` 需要重写整条执行链。
   现实现是在 `_invoke_llm` 内用 `llm.stream()` 取增量并写入总线，**语义等价（真实逐 token）**，但
   与 LangGraph 事件层的解耦方式不同。若要严格对齐，建议单独一批：`astream_events` + 节点级事件映射。
2. **多 worker 不成立**：`progress_bus` 仍是进程内实现；多 worker 时续传与增量只在接住请求的进程内可见。
   启动日志与 README 已注明单进程语义，未实现 Redis 发布订阅。
3. **浏览器交互未自动化验证**：本机没有 Playwright/Selenium，`switchTab`、骨架屏、hash 前进后退、
   `confirmDialog` 键盘操作只有 Node + 最小 DOM 桩的断言，**真实浏览器渲染未验**。建议用
   `frontend_e2e_check.py` + 人工点检 6 个模块切换复核一次。
4. **续传是「补发 + 状态续接」而非严格重放**：缓冲窗口 400 帧（含 token 增量，约十几秒），
   超出窗口只能靠步骤完成的合成事件；续传连接不接收新的 `token_delta`。
5. **真实模型质量、限流降级、OCR、代理缓冲**本轮未涉及，仍需对应环境验证。
6. **前端仍是零构建 + 大量内联 `onclick`**：a11y 只覆盖了新增组件与导航，未做全量迁移。

---

## 8. 下一步建议（按优先级）

1. **阶段二.3 API 契约**：统一响应包 + OpenAPI 快照 + 导出幂等键（收益明确、风险低）。
2. **阶段四.3 结构化输出**：步骤 3/4/7 走 Pydantic + JSON mode + 一次修复重试，`_extract_json` 退为兜底。
3. **阶段四.2 长文覆盖度**：取消 `[:8000]/[:12000]` 静默截断，改 map-reduce 并在提示词声明覆盖范围。
4. **阶段三 RAG**：先建 `tests/rag_eval/` 基线与跑分脚本，再动检索，避免「改了但说不清好坏」。
5. **前端分页与 a11y 收尾**：报告/问卷列表分页（前后端配合），内联 `onclick` 迁移到事件委托。
6. **阶段五.2/5**：`MemorySaver` 换可插拔 checkpointer、`main.py` 迁移 `lifespan` 并持有后台任务引用。

---

# Batch 3 · 连接契约收尾（request_id 贯通 + 导出幂等 + 错误分流）

> 一句话：把「出错之后查不到、连点导出白干活、所有错误长一个样」这三件事一起收掉。
> 本批**未改动任何已有接口路径、请求字段、SSE 事件名与语义、数据库结构**；
> 只在响应里**新增** `request_id` / `data.reused` 两个字段。

## 9.1 三个问题与本批对策

| 问题（改造前） | 后果 | 本批做法 |
|---|---|---|
| 前端报错只有一句 `detail`，后端日志只有时间戳 | 用户报「导出失败」，无法定位是哪一次请求、哪一条日志 | 每个响应带 `X-Request-ID`；错误体回显 `request_id`；日志行第三段即请求号；前端提示里带请求号 |
| 导出按钮连点/重试，每次都完整渲染 Word/PDF | 65KB 文档重复渲染、并发写同一个文件可能读到半成品 | 幂等键 `用户+会话+目标+格式+排版指纹` + 内容指纹 + 文件存在性校验，10 分钟内复用；响应 `data.reused` |
| 所有失败都 `showToast(e.message,'error')`（client 页 20 处） | 超时（可重试）和 409（要改数据）看起来一样，用户不知道该等一下还是该改东西 | `classifyApiError` 分出 8 类语义，可重试给黄色告诫+重试建议，终态给红色+人话建议，取消/登录失效静默 |

## 9.2 改动清单

**新增**

| 文件 | 作用 |
|---|---|
| `backend/middleware/request_context.py` | 纯 ASGI 中间件：校验并透传入站 `X-Request-ID`（不合法则重新生成 16 位 uuid）、写响应头、写入 `scope["state"]` 与 contextvar；`install_log_context()` 用 loguru patcher 让所有日志自动带请求号（无上下文记 `-`） |
| `backend/services/export_cache.py` | 进程内幂等缓存：`cache_key/content_fingerprint/style_fingerprint/lookup/store/clear/stats`，600s TTL、200 条上限、命中需「未过期 + 内容指纹一致 + 文件仍存在」 |
| `tests/test_request_id.py` | 10 项：每请求新号、入站透传、非法值替换、错误体与响应头一致、404 体带号、contextvar 请求内可见、5 并发互不串号、日志带号 |
| `tests/test_error_routing.cjs` | 6 项：可重试→warn+请求号、5xx→warn、冲突/不存在→error+建议、取消与登录失效静默、12 种错误分类、文案不重复 |

**修改**

| 文件 | 改动 |
|---|---|
| `backend/main.py` | 日志格式加 `rid` 段；CORS `expose_headers=["X-Request-ID"]`；异常处理器扩为 `StarletteHTTPException` + `RequestValidationError` + `Exception` 三件套，**错误体统一带 `request_id`**（原 `detail` 保持原样） |
| `backend/routes/agent_routes.py` | 抽 `_export_with_cache()`；整份报告导出、带 `step_content` 的单步导出、按 state 的单步导出三处统一走它；响应新增 `data.reused`；`_launch_workflow` 改用 `contextvars.copy_context().run(...)` 提交线程任务，使 14 步里的日志也带请求号 |
| `frontend/shared/ui.js` | 新增 `classifyApiError` / `showApiError`：8 类语义（timeout/network/busy/server/transient/conflict/notfound/invalid/auth/abort），可重试类只在这些场景附请求号（5xx/未知/超时），避免噪音 |
| `frontend/client/index.html` | 20 处 `showToast(e.message,'error')` 改为 `showApiError(e,{action:'导出'})` 等；上传态保留 413/415 专门文案 |
| `tests/test_engineering_regressions.py` | 夹具增加 `export_cache.clear()`；新增 5 项：导出幂等 4 项（同报告 3 次只渲染 1 次、内容变更重渲染、文件丢失重渲染、word/pdf 独立命中、排版指纹生效、TTL/内容/文件三重失效）+ 工作流线程继承 `request_id` 1 项 |

## 9.3 量化对比

| 指标 | 改造前 | 改造后 | 依据 |
|---|---|---|---|
| 同一报告连续导出 3 次 | 渲染 3 次、写 3 次同一文件 | 渲染 **1 次**，后两次 `reused=true` | 实机：文件 mtime 未变；`test_same_report_exports_once` 计数断言 |
| 报告内容变更后导出 | 无感知（本来每次都重渲染） | 内容指纹不匹配 → 必然重渲染（不会返回旧文件） | 同上用例 |
| 错误可追溯 | 前后端只能靠时间戳猜 | 一次请求一个号，响应头/错误体/日志三处一致 | 实机：`fe-trace-log-1` 同时出现在响应头与 `WARNING | fe-trace-log-1 | ... Token解码失败` |
| 前端错误提示语义 | 1 种（红色 + 原始 message） | 4 类（可重试告诫 / 终态错误 / 静默取消 / 登录跳转） | `tests/test_error_routing.cjs` |
| 前端提示可见的排查信息 | 无 | 5xx/未知错误附「[请求号 xxx]」 | 同上 |

测试基线（本批改完后）：

```
pytest tests/           56 passed   （Batch2 后 41；新增 request_id 10 + 导出幂等 4 + 线程上下文 1）
node --test tests/*.cjs 28 passed   （Batch2 后 22；新增错误分流 6）
test_all_apis.py        46/46       （与基线一致）
frontend_e2e_check.py   63/63       （与基线一致）
wf_e2e.py               35/35       （与基线一致，真实 14 步编排 + Word/PDF 导出）
git diff --check        通过
```

## 9.4 实机验证记录（本批新增）

```
1) 两次 /health：x-request-id: d298ce3ec8c64829 / 3cd1f0fbe7984954（每请求新号）
2) 前端指定：-H "X-Request-ID: fe-trace-9f2c" → 响应头原样返回 fe-trace-9f2c
3) 401 错误体：{"success":false,"detail":"Not authenticated","request_id":"fe-trace-401"}
4) 404 未知路由：{"success":false,"detail":"Not Found","request_id":"f50291b70437450f"}
5) 跨域：access-control-expose-headers: X-Request-ID
6) 日志串联：21:29:39 | WARNING | fe-trace-log-1 | backend.middleware.auth_middleware:decode_token:67 - Token解码失败
7) SSE 未受影响：GET /api/agent/workflow/{sid}/stream 仍返回 200 + text/event-stream
   + transfer-encoding: chunked + x-accel-buffering: no，并带 x-request-id: fe-trace-sse
8) 真实渲染幂等（真实 ExportService，非桩）：word 37KB / pdf 3.4KB
   首次 reused=False → 二次 reused=True 且 mtime 未变 → 内容加一句后 reused=False
9) 接口级三次导出：reused=False → True → True，导出目录仍只有 1 个 .docx
10) 线程上下文：`test_workflow_thread_inherits_request_id` 断言 executor 线程里
    `get_request_id()` 等于发起请求的号（修复前该值为空，14 步日志显示 `-`）
11) 修复后重跑真实 14 步（SSE）：该次请求的日志共 33 行，其中 23 行带同一个请求号
    `86d296d4771e4b95`（`[Step 3]`、状态备份、token 统计等），其余 10 行为启动期无请求上下文日志
```

## 9.5 回滚方式

- 后端：`git checkout -- backend/main.py backend/routes/agent_routes.py`，
  删除 `backend/middleware/request_context.py`、`backend/services/export_cache.py`。
  回滚后响应不再带 `X-Request-ID` 与 `data.reused`，前端只是拿不到这两个可选字段，不会报错。
- 前端：`git checkout -- frontend/shared/ui.js frontend/client/index.html`；
  即便只回滚前端，后端新增字段也被忽略，**前后端可独立回滚**。
- 测试：删除 `tests/test_request_id.py`、`tests/test_error_routing.cjs`；
  `tests/test_engineering_regressions.py` 里 4 个导出用例与夹具的 `export_cache.clear()` 一并回滚。

## 9.6 剩余风险与未验证项（诚实列出）

1. **幂等缓存是进程内的**：多 worker / 多副本部署时每个进程各有一份，只能挡住单进程内的重复点击；
   进程重启后缓存清空（会重渲染一次，不影响正确性）。若要跨进程，需要 Redis 或落库。
2. **幂等键含内容指纹，不含「渲染时间」**：封面日期是渲染时刻，因此 10 分钟内复用会保留首次渲染的日期；
   跨天再导出会因 TTL 过期而重渲染，日期正常。若要求「同一天内复用但日期必须今天」，需把日期也纳入键。
3. **`X-Request-ID` 的 500 边界**：Starlette 的 `ServerErrorMiddleware` 位于用户中间件外层，
   理论上未被全局异常处理器捕获的异常（即发生在中间件层自身）返回的 500 不带该头；
   本项目所有业务异常都经 `Exception` 处理器，实测均带头。
4. **日志的线程边界（本批已修）**：工作流出图在 executor 线程里，`run_in_executor` 不复制 contextvars，
   修复前 14 步日志显示 `-`。现已改为 `contextvars.copy_context().run(...)` 提交，并有
   `test_workflow_thread_inherits_request_id` 断言线程内取到的就是发起请求的号。
   `run_in_threadpool`（anyio）路径本就复制上下文，无需改动。
5. **`frontend/workflow/index.html` 未接入错误分流**：该页仍是自带的 `showToast` + 原生 `fetch`，
   错误只有原始 message。统一它意味着给该页接入 `shared/api.js`，属于阶段一模块化的收尾工作。
6. **`RequestValidationError` 的响应体结构**：保持 FastAPI 默认的 `detail: [...]` 列表形态（只加 `request_id`），
   若后续要做「统一响应包」，会改动前端对 422 的解析，需单独一批并同步改测试。

---

# Batch 4 · 结构化输出：形状校验 + 一次修复重试

> 一句话：以前步骤 3/4/7 的 JSON 解析失败会被 `except ValueError` 吞掉，下游拿着上一版数据继续跑，
> 用户看到的是一份「看起来完整、其实这一步没生效」的报告。本批把这件事变成显式的：
> **要么通过，要么修一次，要么标待人工复核并保留原文**，不再有第四种「静默变空」的结局。

## 10.1 改造前的问题（代码级）

```python
try:
    isys = json.loads(self._extract_json(content) or "null")
except ValueError:
    isys = None            # ← 三层信息全丢：错在哪、原始输出是什么、要不要重试
```
- 步骤 3：解析失败 → `indicator_tree_error(None)` → 只说「指标体系必须是对象」；
- 步骤 4：解析失败 → `scores = {}` → 静默保留上一次分值；
- 步骤 7：解析失败 → `candidates = {}` → 一条评分都不落库；
- 三处都**没有**「让模型按结构重出一次」的机制，也**没有**在步骤内容里留下可核对的原味输出。

## 10.2 判定规则（这是本批的核心设计）

只判**形状**，不判**内容**——内容对不对是 eval 与人工复核的事，形状对不对是程序能保证的事。

| 级别 | 含义 | 处理 |
|---|---|---|
| 阻断性错误 | JSON 解析不出来／疑似被截断（有 `{` 无 `}`）／顶层不是对象／容器类型不对／一条可用条目都没有（步骤 3） | 触发**一次**修复重试 |
| 非阻断问题 | 个别条目缺 `id`/`name`、分值不是数字、缺 `evidence_id`/`reason` | 丢弃该条目 + 记 warning（下游本来也会丢，但现在**看得见**） |
| 合法空结果 | 步骤 7 返回 `{"scores": {}}`（提示词要求「没有依据的指标写待核实，不输出分值」） | 通过 + warning，**不重试** |

状态三态：`ok`（一次通过）／`repaired`（修一次后通过）／`manual_review`（两次都不合规）。
`manual_review` 时：该步骤 `structured_status="manual_review"`、`structured_errors` 记录原因、
`warnings` 追加「待人工复核：…」、**原始输出仍在 `step_results[step].content` 里**。

## 10.3 改动清单

**新增**

| 文件 | 作用 |
|---|---|
| `backend/agents/tools/structured_output.py` | 纯函数层：`extract_json`（唯一实现，agent 的 `_extract_json` 改为委托）、`parse(step, text)`、三个步骤的校验器、`_coerce_number`（`20` / `"20分"` / `"1,200"` / `"20%"` 均可）、`repair_messages`、`log_outcome` |
| `tests/test_structured_output.py` | 28 项：形状与数值判定 23 项（合法/截断/缺字段/容器类型错/部分条目不合规/包装层解包/无 JSON/数值强制转换参数化 8 例/步骤 4 各态/步骤 7 各态/修复提示词内容）+ 节点联动 5 项（修复后通过、两次失败转人工、步骤 4 失败不覆盖上一版、步骤 4 正常落分值、步骤 7 丢弃不可核实条目） |

**修改**

| 文件 | 改动 |
|---|---|
| `backend/agents/performance_agent.py` | 新增 `_execute_step_structured` / `_repair_structured_output` / `_note_structured_warnings`；节点 3/4/7 改用结构化执行；`_record_step_result` 增加 `structured` 参数写入 `structured_status` / `structured_errors`；`_invoke_llm` 增加 `stream` 开关（修复补调不进入前端逐字流，改为推送一行「正在重新生成…」提示） |
| `backend/routes/agent_routes.py` | 状态接口的 `step_summaries` 透出 `structured_status` / `structured_errors`（新增字段，老前端忽略） |

## 10.4 实跑发现的一次真实误判（本批最重要的一条）

第一版把「`scores` 为空」当成阻断性错误。真实 14 步跑到步骤 7 时，模型返回 `{"scores": {}}`，
于是触发了一次修复：多花 **3012 prompt tokens**，拿回一个 6 token 的 `{}`，最后仍然判 `manual_review`。

这不是模型的问题，是判定规则错了：提示词明确要求「没有依据的指标写待核实，不输出分值」，
所以空 `scores` 是**合法结果**。修正后同一份流程重跑：

```
[Step 7] 结构化输出通过，忽略 12 条不合规条目
步骤 3/4/7 structured_status = ok / ok / ok
warnings: 步骤 7 有部分输出未进入结构化结果：
          scores['L3_01_01_01'].actual_score 不是数字，已忽略 … 等 12 条
```

模型确实给 12 个指标写了 `actual_score: "待核实"`——这些条目以前是被**静默丢弃**的，
现在会在 warnings 里点名。这条修正直接把「形状判定」的边界钉清楚了：
**内容层面的「没有值」不是格式错误。**

## 10.5 量化对比

| 指标 | 改造前 | 改造后 | 依据 |
|---|---|---|---|
| JSON 解析失败时的行为 | 静默变空 dict，下游用旧数据继续 | 修一次 → 仍失败则 `manual_review` + 原文保留 | `test_step3_two_failures_fall_back_to_manual_review` |
| 失败原因可见性 | 无 | `structured_errors` + warnings + 状态接口字段 | 实机状态接口验证 |
| 被丢弃的条目 | 静默丢弃 | warnings 点名条目 ID 与原因 | 实机：12 条被点名 |
| 修复重试次数 | 0（不存在） | ≤1 次/步骤，且只在阻断性错误时触发 | `test_step3_repair_then_pass` 断言 `len(repairs)==1` |
| 修复补调对前端流的影响 | — | 不进入逐字流，只推一行提示 | `repairs[0]["stream"] is False` |
| 真实 14 步额外开销 | — | **0 次修复调用**（修正判定后） | 实跑 `[Token] step=7` 仅 1 条记录 |

测试基线（本批改完后）：

```
pytest tests/           84 passed   （Batch3 后 56；新增结构化输出 28）
node --test tests/*.cjs 28 passed   （不变）
test_all_apis.py        46/46       （不变）
frontend_e2e_check.py   63/63       （不变）
wf_e2e.py               35/35       （不变，真实 14 步）
git diff --check        通过
```

## 10.6 可重跑命令

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_structured_output.py
.venv/Scripts/python.exe -m pytest -q tests/            # 全量
# 真实模型验证（需模型配置，约 4 分钟）
NO_PROXY=127.0.0.1,localhost .venv/Scripts/python.exe wf_e2e.py
# 观察结构化判定是否触发（正常应为 0 条「尝试一次修复」）
grep -E "结构化输出" server.log
```

## 10.7 回滚方式

- 删除 `backend/agents/tools/structured_output.py` 与 `tests/test_structured_output.py`；
- `git checkout -- backend/agents/performance_agent.py backend/routes/agent_routes.py`；
- 回滚后步骤 3/4/7 恢复「解析失败静默变空」的旧行为，前端不再收到
  `structured_status` / `structured_errors` 两个可选字段，其它一切不变。

## 10.8 剩余风险与未验证项（诚实列出）

1. **只保证形状，不保证内容**：校验器不判断「指标设计是否专业」「评分理由是否成立」，
   这些仍靠证据引用 + 人工复核 + 后续 eval 集。别把 `ok` 读成「这一步是对的」。
2. **修复重试只有一次**：两次都不合规就转人工。没有做「换模型重试」或「降级到模板」，
   这类策略要等阶段四的模型韧性/降级链一起做。
3. **`manual_review` 不阻断工作流**：步骤 4/6/7 仍会带着上一版数据继续跑，只是 warnings 里明说。
   是否要把「步骤 3 复核未通过」变成硬中断（不让人在坏数据上继续），需要产品决策——
   现状是保留可继续性，把判断权交给审批点的用户。
4. **数值容忍度是经验规则**：`20分`/`20%`/`1,200` 会被强制转换，`"待核实"` 会被丢弃。
   若将来模型常用别的写法（如 `"约 20"`、`"20-25"`），需要补规则或统一在提示词里约束格式。
5. **修复提示词不含原始系统提示**：修复只给「结构要求 + 校验错误 + 原输出」，不重放完整的角色/规则提示。
   好处是省 token、聚焦结构；风险是修复结果可能丢掉原输出里的业务细节（只影响该步骤的结构化字段，
   正文仍是第一次的输出）。
6. **真实环境只覆盖了「一次通过」与「空结果」两种路径**：`repaired` 与 `manual_review` 的真实模型表现
   只有桩测试覆盖，没有故意构造坏模型输出跑通端到端。

---

# Batch 5 · 长文档覆盖度透明化（取消静默截断）

> 一句话：一份 4.2 万字的资料，以前只有前 8000/12000 字进过模型，其余部分**连一句话都没提过**；
> 现在每个步骤都会被告知「资料共 N 字，你只看到 M 字，没看到的是哪几页」，并且那些页会被压缩成要点。

## 11.1 改造前的问题（代码级）

```python
# 改造前 backend/agents/performance_agent.py::_build_human_vars
"raw_text_preview": (state.get("raw_text") or "")[:8000],     # 静默截断
"raw_text_full":    (state.get("raw_text") or "")[:12000],    # 静默截断
```

三个后果，都不是「模型不够聪明」：

1. **材料从未参与**：6 万字资料里后 4.8 万字没进过任何一次调用，报告却写得像看完了全篇；
   项目末段的验收结论、整改情况恰恰最常落在被砍掉的尾部。
2. **无从判定**：提示词、步骤详情、状态接口里都没有「用了多少资料」的信息，
   人工复核无法区分「模型没看出来」和「模型没看到」。
3. **无从追责**：即使发现结论漏了某页，也没有定位符能说明那一页是否被纳入。

## 11.2 本批做法（全部不依赖模型，可离线验证）

| 环节 | 做法 | 位置 |
|---|---|---|
| 切块 | 按定位符切：PDF 页码 `【PDF 第N页】` > 标题 > 段序；超长段按句末标点再切；块间保留 `(1/2)` 序号 | `doc_coverage.split_documents` |
| 选块 | **头部 + 尾部**双端预算（尾部预留 20%），不是一味砍尾巴 | `doc_coverage.build_plan` |
| 声明 | 生成「总字数 / 直接引用字数 / 占比 / 未引用块的文件+页码」，并明确「不要猜测未纳入内容」 | `CoveragePlan.statement` |
| 摘要 | 未引用部分按 6000 字/批、最多 3 批交给模型压成「- 要点 [定位符]」清单（map-reduce，去重截断） | `_digest_omitted` / `digest_batches` / `reduce_digest` |
| 注入 | 声明 + 摘要写进最终 human prompt；摘要段落显式标注「不得当作证据编号引用」 | `_execute_step` |
| 口径一致 | 步骤 1 只用 8000 字节选，声明就按 8000 报（`delivered_chars`），并写明「预算内还有 X 字未放入，也未做摘要」 | `statement(delivered_chars=…)` |
| 可见性 | 状态接口新增 `document_coverage`；步骤详情显示覆盖度徽标（绿=全覆盖，黄=部分覆盖+未引用字数+是否已压缩） | `agent_routes.py` / `frontend/client/index.html` |

不变式（测试与真实运行都断言）：`used_chars + omitted_chars == total_chars`，块按原文顺序排列，定位符可回指原页。

## 11.3 改动清单

| 文件 | 变更 |
|---|---|
| `backend/services/doc_coverage.py` | 新增：切块/预算/声明/摘要四件事，零模型依赖 |
| `backend/agents/performance_agent.py` | 新增 `DOC_PREVIEW_BUDGET=8000` / `DOC_FULL_BUDGET=12000` / `PREVIEW_ONLY_STEPS={1}`、`_document_coverage`（会话内只算一次）、`_digest_omitted`、`_build_human_vars` 改用分块预算与声明、`_execute_step` 注入声明与摘要、`document_coverage` 进状态备份白名单 |
| `backend/agents/states/performance_state.py` | 新增可选字段 `document_coverage: Dict[str, Any]` |
| `backend/agents/prompts/default_prompts.py` | 步骤 1 的「（前8000字）」改为「（节选；未纳入部分见下方资料覆盖度）」 |
| `backend/routes/agent_routes.py` | 状态接口透出 `document_coverage` |
| `frontend/client/index.html` | `wfCoverage` 状态 + `buildCoverageBadge()` + `.wb-coverage` 样式；新会话启动时清空 |
| `tests/test_document_coverage.py` | 新增 19 项 |
| `verify_coverage_live.py` | 新增：真实模型验证脚本（4.2 万字文档，验证摘要质量与节选口径） |

## 11.4 真实运行数据（`verify_coverage_live.py`，deepseek-chat）

构造一份 42,547 字、15 页的 PDF 风格资料，把三条关键事实**只**放在第 8 页（必然落在未引用区间）。

```
总字数 42363｜预算内可选 10020｜未直接引用 32343｜覆盖 23.6%｜模式 head_tail｜切块 29
步骤1 实际收到 8000 字；步骤2 实际收到 10032 字
未引用部分压缩：3 次调用，共 3.0 秒 → 666 字要点
中段关键事实命中：3/3 -> ['上浮12%', '监理日志缺3次', '42平方米']
步骤1 输出 4802 字，提示词 9257 字
提示词含覆盖度声明：True｜含要点摘要：True｜直接引用部分不含中段事实：True
```

模型压出来的要点（原文未进提示词，仅第 8 页有）：

```
- 材料单价较中标价上浮12%，涉及金额186万元 [长报告.pdf 第8页]
- 监理日志缺3次，时段为2025年3月至5月 [长报告.pdf 第8页]
- 项目部档案室面积42平方米，不满足档案存放要求 [长报告.pdf 第8页]
```

步骤 1 的声明（节选口径，数字闭合）：

```
【资料覆盖度】资料共 42363 字，本步骤提示词直接引用 8000 字（18.9%）；
本次预算可选 10020 字，本步骤只放入前 8000 字，其余 34363 字未进入本步骤提示词。
未直接引用的部分：长报告.pdf 第4页（1/2）（2008 字）、… 等 22 段。
预算内还有 2020 字未放入本步骤，也未做摘要。
未直接引用部分的要点摘要（已压缩 14280 字为下方要点，其余未压缩）见「未直接引用部分的要点」。
```

状态接口同步透出（`wf_e2e.py` 刚跑完的真实会话，875 字节样例 → 354 字，走「全部纳入」分支）：

```json
GET /api/agent/workflow/{session_id}/status
"document_coverage": {
  "total_chars": 354, "used_chars": 354, "omitted_chars": 0, "used_ratio": 1.0,
  "mode": "full", "chunks": 1, "omitted": [], "has_digest": false,
  "statement": "【资料覆盖度】资料共 354 字，已全部纳入本次提示词。"
}
```

## 11.5 量化对比

| 指标 | 改造前 | 改造后 | 依据 |
|---|---|---|---|
| 未纳入部分是否被声明 | 无任何说明 | 声明含总字数/引用字数/占比/未引用页码 | 实跑输出（11.4） |
| 中段材料的可见性 | 0 字进入提示词 | 压缩为要点进入提示词，3/3 关键事实命中 | 实跑输出（11.4） |
| token 成本 | 8000/12000 字 | 8000/12000 字 + 一次摘要（3 次调用、3.0 秒、约 1.4 万字原文压缩） | 同上 |
| 步骤 1 声明与实际收到字数 | 不适用（无声明） | 完全一致（8000 vs 8000） | `test_preview_step_declares_only_what_it_really_received` |
| 覆盖度可见性 | 仅代码里能看到 `[:8000]` | 状态接口字段 + 前端徽标 + warnings | `test_to_dict_is_lightweight_and_serialisable`、实机页面 |
| 摘要调用是否污染逐字流 | — | 否（`stream=False`），失败只影响摘要不影响主流程 | `_digest_omitted` 的 try/except + 测试断言 |

测试基线（本批改完后）：

```
pytest tests/           103 passed  （Batch4 后 84；新增覆盖度 19）
node --test tests/*.cjs  28 passed  （不变）
test_all_apis.py         46/46      （不变）
frontend_e2e_check.py    63/63      （不变）
wf_e2e.py                35/35      （不变，真实 14 步，tokens=37499）
git diff --check         通过
```

## 11.6 可重跑命令

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_document_coverage.py
.venv/Scripts/python.exe -m pytest -q tests/                  # 全量
.venv/Scripts/python.exe verify_coverage_live.py              # 真实模型，约 35 秒
# 看某次会话的覆盖度：GET /api/agent/workflow/status 里的 data.document_coverage
```

## 11.7 回滚方式

- 删除 `backend/services/doc_coverage.py`、`tests/test_document_coverage.py`、`verify_coverage_live.py`；
- `git checkout -- backend/agents/performance_agent.py backend/agents/states/performance_state.py backend/routes/agent_routes.py frontend/client/index.html backend/agents/prompts/default_prompts.py`；
- 回滚后提示词恢复 `[:8000]/[:12000]` 静默截断，状态接口不再返回 `document_coverage`，
  前端不显示徽标；其余字段与行为不受影响（新增字段均为可选）。

## 11.8 剩余风险与未验证项（诚实列出）

1. **预算按整块分配，会剩 1~2 千字没用**：42,363 字的资料预算 12000，实际只用 10,020（83.5%）。
   为了保住 `used+omitted=total` 这条可对账的不变式，没有做「半块填充」。要提利用率需改成
   块内切分并重编定位符，属于下一轮可选项。
2. **摘要只覆盖前 3 批（约 1.4 万字）**：42,363 字的资料里仍有 1.8 万字既没进提示词也没进摘要。
   声明里写了「其余未压缩」，但**这部分内容模型完全不知道**。要全覆盖就得加批次数（token 线性上涨）
   或做分层摘要（摘要的摘要），本批没做。
3. **摘要质量取决于模型**：实测要点忠实（金额/时间/主体与原文字面一致），但这是单一样本；
   样板化文本会产生重复要点（如「工作按计划推进」多条），去重只按整行去重，语义相同表述不同的行不会合并。
4. **切块依赖格式**：有 `【PDF 第N页】` 或 Markdown 标题时定位符才有意义；纯扫描件/无空行大段文本
   只能退化为段序或硬切，定位符精度下降（PDF 转换质量是前置条件，见 Batch 1 的能力边界）。
5. **步骤 1 仍只给 8000 字**：这是有意的（资料清单步骤不需要全文），但意味着步骤 1 的判断依据
   比后续步骤更窄；现在至少是**明示**的窄，而不是静默的窄。
6. **覆盖度徽标是会话级，不是步骤级**：同一份资料在各步骤的预算相同，徽标只显示预算内的覆盖比例；
   步骤 1 的「节选口径」只在提示词里体现，页面上没有单独标注。
7. **没有做「覆盖不足就拒绝出报告」**：覆盖 23.6% 仍会照常生成报告。是否设覆盖下限、低于下限是否
   强制提示用户在报告首页标注「本报告基于 M/N 字资料」，属于产品决策，本批只提供数据。

---

# Batch 6 · 检索评估基线（先量出来，再谈优化）

> 一句话：在写任何一行检索代码之前，先固定 29 条「问题 → 期望证据」，
> 把「现在的检索到底找不找得到证据」变成一个数字。答案是 **Recall@3 = 0.172**。

## 12.1 为什么必须先做这一步

线上真正决定「模型看到哪些原文」的是 `backend/services/workflow_evidence.py::retrieve_step_evidence`：

```python
terms = (*STEP_TERMS.get(step, ()), state.get("indicator_type") or "")
score = sum(term.casefold() in hit["text"].casefold() for term in terms if term)
```

它按**步骤号的固定词表**给每个 480 字窗口打分，取项目资料前 3 条。注意两件事：

1. **它完全不使用用户的提问**——函数签名里根本没有 query，只有 `step` 和 `state`；
2. 打分是「词表里命中了几个词」，与「这段文字是否回答了当前问题」无关。

在 Batch 6 之前，项目里没有任何东西能回答「检索好不好」。这种情况下改检索（换向量、加混合、
调条数）无法证明是变好还是变坏——所以本批只做一件事：**把现状钉成一个可重跑的基线**。

## 12.2 交付物

| 文件 | 作用 |
|---|---|
| `tests/rag_eval/corpus.py` | 7 份虚构脱敏文档，271 个块 / 9,925 字：批复可研、施工合同、支付明细、验收自评、监理月报/整改、按月生成的月报汇编与支付台账（干扰页） |
| `tests/rag_eval/samples.py` | 29 条样本，覆盖步骤 1–14，每条给「问题 + 期望证据锚点 + 为什么这句必须取证」 |
| `tests/rag_eval/run.py` | 跑分器：Recall@1/3/5、MRR、精确率、空结果率、引用逐字率、**查询无关性**；支持 `--verbose / --json / --save / --check` |
| `tests/rag_eval/baseline.json` | 本次记录的基线数值（含检索器标识、语料规模、日期） |
| `tests/test_rag_eval.py` | 6 项回归保护：样本自检、语料规模、引用逐字、指标不低于基线、查询感知必须以召回提升为条件 |

语料刻意保留大量「合规但无用」的干扰页（监理月报汇编 19 期、支付台账 19 期、制度条文、会议纪要）：
真实项目的检索难点不是「资料里没有那句话」，而是「有用的一句话淹没在几百页套话里」，
而这些干扰页同样命中步骤词表（项目/金额/时间/验收），正好是关键词打分的盲区。

## 12.3 基线数值（`python -m tests.rag_eval.run`）

```
检索器：workflow_evidence.retrieve_step_evidence（按步骤固定词表打分，项目≤3 条 + 知识≤2 条）
语料：7 份文档 / 271 个块 / 9925 字        样本：29 条

Recall@1 = 0.035   Recall@3 = 0.172   Recall@5 = 0.172
MRR      = 0.086   精确率 = 0.071（返回条目中真正命中的比例）
空结果率 = 0.035   平均返回 2.83 条 / 82.6 字
引用逐字率 = 1.000
查询无关性 = 1.000（同一步骤不同问题拿到完全相同的证据）
```

按步骤（Recall@3）：

| 步骤 | 样本 | Recall@3 | MRR | 空结果 |
|---|---|---|---|---|
| 1 资料清单 | 1 | 1.00 | 1.00 | 0 |
| 2 核心信息 | 10 | 0.00 | 0.00 | 0 |
| 3 指标体系 | 2 | 1.00 | 0.67 | 0 |
| 4 指标分值 | 1 | 0.00 | 0.00 | 0 |
| 5 评分标准 | 2 | 0.00 | 0.00 | 0 |
| 6 评价表 | 1 | 0.00 | 0.00 | 0 |
| 7 指标分析 | 3 | 0.00 | 0.00 | 0 |
| 8 经验做法 | 1 | 0.00 | 0.00 | 1 |
| 9 问题诊断 | 2 | 0.00 | 0.00 | 0 |
| 10 建议 | 1 | 1.00 | 0.50 | 0 |
| 11 综合评价 | 1 | 0.00 | 0.00 | 0 |
| 12 基本情况 | 2 | 0.00 | 0.00 | 0 |
| 13 工作简介 | 1 | 1.00 | 0.50 | 0 |
| 14 最终报告 | 1 | 0.00 | 0.00 | 1 |

## 12.4 三个具体失败样例（返回了什么，而不是返回得「不够好」）

```
S01 step=2  问：项目总投资是多少？        期望：项目总投资 4,860 万元
  1. 04_竣工验收… 第1页 :: 验收时间：2025 年 9 月 26 日。验收组织单位：城西区住建局。
  2. 01_立项批复… 第1页 :: 项目名称：城西片区雨污分流改造工程（二期）。
  3. 01_立项批复… 第1页 :: 项目单位：城西市政建设服务中心。

S15 step=5  问：工期延误的违约金如何计算？  期望：0.5‰ 计违约金
  1. 01_立项批复… 第2页 :: 估算依据：2023 年市政工程消耗量定额及当期材料信息价。
  2. 02_施工总承包合同… 第6页 :: 质量标准：符合《给水排水管道工程施工及验收规范》…
  3. 07_支付台账汇编… 第1页 :: 支付依据：月度计量单、监理签认单、发票及合同复印件。

S22 step=9  问：审计发现哪些资金支付问题？  期望：1 笔支付凭证缺失
  1. 04_竣工验收… 第3页 :: 遗留问题：3 处接驳井标高与设计不符…
  2. 02_施工总承包合同… 第4页 :: 违约条款：每延误 1 日按合同价款的 0.5‰ 计违约金…   ← 正是 S15 想要的
  3. 03_资金支付… 第3页 :: 较序时进度低 10 个百分点，主要原因是二标段征地拆迁滞后…
```

S22 第二行最能说明问题：**正确答案就在库里，但它去服务了上一个步骤的问题**——
因为检索只看步骤词表，而「违约条款」这句话同时命中步骤 5 和步骤 9 的词表。

## 12.5 根因（代码级，留给阶段三）

| 现象 | 代码原因 |
|---|---|
| 检索与提问无关 | `retrieve_step_evidence(db, state, step, include_knowledge)` 没有 query 参数 |
| 「词多者胜」而非「相关者胜」 | `score = sum(term in text)`，长句/套话命中词更多 |
| 同分时顺序 = 文档顺序 | `sorted(candidates, key=lambda item: -item[0])` 是稳定排序，同分保持原顺序 |
| 证据量固定 3 条 / 82.6 字 | `for kind, limit in (("project", 3), ("knowledge", 2))` |
| 超长资料后段不可检索 | `content[:100_000]` 直接截断（与 Batch 5 的覆盖度问题同源） |

## 12.6 可重跑命令

```bash
.venv/Scripts/python.exe -m tests.rag_eval.run              # 打印基线
.venv/Scripts/python.exe -m tests.rag_eval.run --verbose    # 逐条看命中/未命中
.venv/Scripts/python.exe -m tests.rag_eval.run --check      # 低于基线则退出码 1
.venv/Scripts/python.exe -m pytest -q tests/test_rag_eval.py
```

`--check` 的报警能力已实测：把 `baseline.json` 的 Recall@3 人为改成 0.9 后，
`--check` 输出 `低于基线：Recall@3: 0.9 → 0.1724` 并以退出码 1 结束（验证后已恢复原值）。

## 12.7 回滚方式

- 删除 `tests/rag_eval/`、`tests/test_rag_eval.py`；
- 本批**未改动任何生产代码**（检索、提示词、接口、前端一行未动），回滚无副作用。

## 12.8 剩余风险与未验证项（诚实列出）

1. **语料只有 1 万字**：真实项目常见 5 万–50 万字，块数多一个量级，top-3 的相对覆盖率更低，
   所以**本基线偏乐观**，不能拿 0.172 说「线上大概就是这个水平」，只能说「不会更好」。
2. **知识库分支未评估**：`include_knowledge=True` 需要 `KnowledgeBase` 表数据，
   本批只测了项目资料分支（默认 `use_rag=False` 的路径）。
3. **样本是我自己写的**：问题都指向「一句话能说清的事实」，对「需要跨三份文件拼起来才能判断」
   的情形覆盖不足；这类问题的检索难度更高，基线应当更低。
4. **指标衡量的是「取没取到那句话」，不是「够不够做判断」**：取到 3 条相关证据但不足以支撑
   一个指标评分时，Recall 仍记为命中。
5. **未与向量检索对比**：`RAGService`（Chroma + multilingual-MiniLM）目前**不在工作流路径上**，
   本批没有把它接进来对比——那属于阶段三的改造，需要同时解决「向量库写入时机」和「离线可跑」。
6. **样本锚点是「逐字短语」**：模型改写过的同义表述不算命中，这对检索偏严格；
   但评估的是「能不能把原文捞出来」，用逐字锚点是合理的。

## 12.9 顺带发现的缺陷（本批不修，记录在案）

`POST /api/admin/prompts/{prompt_id}/reset` 会 500：

```python
from backend.services.rag_service import get_default_prompts   # 该模块没有这个函数
```

`backend/services/rag_service.py` 里只有 `RAGService / get_rag_service`，
`get_default_prompts` 从未定义，导入即 `ImportError`。默认提示词实际来自
`backend/agents/prompts/default_prompts.py:PromptTemplateFactory`。

不属于本批范围（本批承诺不动生产代码），留作下一批的第一项：改成用
`PromptTemplateFactory.build_raw_system_prompt + "\n\n---\n\n" + build_raw_human_prompt`
拼接（与 `PromptManager.load_from_database` 的拆分约定一致），并补一条 TestClient 回归。

---

# Batch 7 · 查询感知检索（BM25）+ 修掉那个必 500 的重置接口

> 一句话：把「按步骤词表数命中个数」换成 BM25 + 中文二元组，同一批样本 Recall@3 从 **0.172 → 0.414**；
> 同时修掉上一批发现的 `POST /api/admin/prompts/{id}/reset` 必 500。

## 7.1 顺带修复：提示词重置接口必 500

改造前（Batch 6 记录在案的缺陷）：

```python
from backend.services.rag_service import get_default_prompts   # 该模块只有 RAGService / get_rag_service
```

导入即 `ImportError`，管理端点「重置为系统默认」100% 失败。修复后改用真实来源，
并沿用 `PromptManager.save_to_database` 的库内格式约定（`system + "\n\n---\n\n" + human`），
这样 `PromptManager.load_from_database` 才能正确拆出 system / human 两段：

```python
default_data = get_all_default_prompts_as_dict().get(prompt.step_number) or {}
prompt.prompt_content = f"{system}\n\n---\n\n{human}"
```

顺带把两个静默行为改掉：没有默认模板时返回 400（而不是「假装成功」），响应带上新版本号。
回归测试 `test_admin_prompt_reset_restores_builtin_template` 覆盖了 401/403/200/404/400 五条路径，
并断言重置后的内容能被 `"\n\n---\n\n"` 正确拆分。

## 7.2 检索：从「词表计数」到 BM25

改造前：

```python
terms = (*STEP_TERMS[step], indicator_type)
score = sum(term.casefold() in hit["text"].casefold() for term in terms if term)
candidates.sort(key=lambda item: -item[0])     # 同分保持原文顺序
```

改造后（`backend/services/retrieval.py`）：

| 环节 | 做法 | 为什么 |
|---|---|---|
| 分词 | 中文二元组 + 英文/数字连续串；金额千分位归一（`4,860` → `4860`） | 中文无空格，二元组不需要 jieba 也能有区分度；单字不入索引（「的/项/目」无信息量） |
| 打分 | BM25（k1=1.2, b=0.75），idf 由本批语料动态算出 | 「项目」「资金」这类高频词自然降权；长度归一化让长段落不能靠字数取胜 |
| 查询 | 每步一句「步骤意图」（`STEP_QUERIES`），可被调用方覆盖 | 原来的步骤词表从「打分项」变成「查询词」，同样的信息换了正确的位置 |
| 步骤偏好 | 词表命中 ×1.05，知识库同步骤 ×1.1 | 保留旧信号，但权重远小于 BM25，不再决定排序 |
| 回退 | BM25 一个已知词都没命中时，走原来的关键词扫描 | 单字/生僻查询不至于空手而归 |
| 截断 | 单文件 30 万字以上写 `warnings` | 原来的 `content[:100_000]` 是无提示截断，与 Batch 5 修的是同一类问题 |
| 缓存 | 按「块 id + 长度」为键，会话内只建一次索引 | 14 步各建一次索引是纯浪费 |

**刻意没改的两件事**（为了让这次对比干净）：

1. 证据条数仍是「项目 3 + 知识 2」——条数是独立杠杆，见 7.4 的诊断；
2. 线上仍用「步骤意图」当查询，不用用户提问——工作流里没有「每步的具体问题」这种东西。

## 7.3 前后对比（同一批 29 条样本、同一份语料）

| 指标 | 改造前（词表计数） | 改造后（BM25） | 变化 |
|---|---|---|---|
| Recall@1 | 0.0345 | **0.3103** | +0.276 |
| Recall@3 | 0.1724 | **0.4138** | +0.241 |
| MRR | 0.0862 | **0.3621** | +0.276 |
| 精确率 | 0.0714 | **0.1494** | ×2.1 |
| 空结果率 | 0.0345 | **0.0000** | 不再空手而归 |
| 平均返回 | 2.83 条 / 82.6 字 | 3.00 条 / 90.7 字 | 每步都用满预算 |
| 引用逐字率 | 1.0 | 1.0 | 未退化（返回文本仍逐字来自原文） |

按步骤（Recall@3，改前 → 改后）：

| 步骤 | 改前 | 改后 | 步骤 | 改前 | 改后 |
|---|---|---|---|---|---|
| 1 资料清单 | 1.00 | **0.00** | 8 经验做法 | 0.00 | 0.00 |
| 2 核心信息 | 0.00 | **0.20** | 9 问题诊断 | 0.00 | **0.50** |
| 3 指标体系 | 1.00 | 1.00 | 10 建议 | 1.00 | 1.00 |
| 4 指标分值 | 0.00 | 0.00 | 11 综合评价 | 0.00 | **1.00** |
| 5 评分标准 | 0.00 | **0.50** | 12 基本情况 | 0.00 | **0.50** |
| 6 评价表 | 0.00 | 0.00 | 13 工作简介 | 1.00 | 1.00 |
| 7 指标分析 | 0.00 | **0.33** | 14 最终报告 | 0.00 | **1.00** |

**唯一退步的是步骤 1（1.00 → 0.00，只有 1 条样本）**：新检索把「附件清单 / 档案内容 / 台账」
这类句子排在前面，而样本期望的是「合同名称」「验收结论」。这不是 bug，是个真问题——
**「步骤 1 的任务（整理资料清单）」和「复核人员的问题（提供了合同和验收材料吗）」不是同一件事**。
旧实现对上了纯属巧合（词表里正好有「资料/合同/验收」）。

## 7.4 瓶颈定位：问题不在排序，在「问什么」

`--grid` 打印 2×2 诊断（查询 × 证据条数）：

```
查询       条数   Recall@3  Recall@5  MRR     精确率
步骤意图        3   0.414     0.414     0.362   0.149     ← 线上现状
步骤意图       10   0.414     0.483     0.404   0.076     ← 只放开条数：+0.069@5
问题本身        3   0.828     0.828     0.724   0.333     ← 只换查询：+0.414
问题本身       10   0.828     0.897     0.743   0.147     ← 两者都放开
```

结论很硬：

- **条数杠杆价值有限**：3 → 10 条只把 Recall@5 从 0.414 抬到 0.483，代价是精确率腰斩（0.149 → 0.076）
  和每步约 +3,400 字提示词；
- **查询杠杆价值巨大**：同一套 BM25，把「步骤意图」换成「具体问题」，Recall@3 直接翻倍到 0.828。
  也就是说——**排序已经不是主要瓶颈，缺的是「每步该问什么」**。

所以下一批的正确做法不是继续调 BM25 参数，而是给每个步骤生成 2–3 个聚焦子查询
（资金 / 进度 / 结果三个面），用 3 条证据覆盖 3 个面，而不是让一个笼统的查询占满 3 个格子。
本批**没有**顺手做这件事：语料和 29 条样本是我自己造的，在这里现场调查询词极易过拟合，
测出来的提升不可信。先把「查询是瓶颈」这个结论钉住，再单独开一批做 query plan。

## 7.5 改动清单

| 文件 | 变更 |
|---|---|
| `backend/services/retrieval.py` | 新增：`tokenize` / `BM25Index` / `build_index`（零依赖、纯函数） |
| `backend/services/workflow_evidence.py` | 重写排序：`STEP_QUERIES` + BM25 + 步骤偏好 + 回退 + 索引缓存 + 截断告警；`retrieve_step_evidence` 增加可选 `query` / `project_limit` / `knowledge_limit`，其余参数与返回结构不变 |
| `backend/routes/admin_routes.py` | 修 `prompts/{id}/reset` 的 ImportError，改用 `get_all_default_prompts_as_dict()` |
| `tests/test_retrieval.py` | 新增 14 项：分词、排序、长度归一化、查询生效、条数、回退、截断可见且不重复、索引缓存只建一次、证据逐字 |
| `tests/test_engineering_regressions.py` | 新增 `test_admin_prompt_reset_restores_builtin_template` |
| `tests/rag_eval/run.py` | 新增 `--limit` / `--question` / `--grid`；`--save` 保留上一版基线到 `previous` |
| `tests/rag_eval/baseline.json` | 新基线（0.4138）并保留 Batch 6 的 0.1724 作为 `previous` |

## 7.6 真实工作流验证

```
wf_e2e.py                35/35（真实 14 步，tokens=38470，上一批 37499，+2.6%）
structured_status        步骤 3/4/7 = ok/ok/ok
warnings                 仅「21 条 actual_score 不是数字已忽略」（已知的「待核实」）
test_all_apis.py         46/46
frontend_e2e_check.py    63/63
pytest tests/            124 passed（Batch6 后 109；新增 14 检索 + 1 重置接口）
node --test tests/*.cjs  28 passed
git diff --check         通过
```

token 上涨 2.6% 的来源可以解释：以前有些步骤只返回 2.83 条（词表没命中），现在每步都用满 3 条，
块平均长度也从 82.6 字涨到 90.7 字。

## 7.7 可重跑命令

```bash
.venv/Scripts/python.exe -m tests.rag_eval.run            # 当前基线
.venv/Scripts/python.exe -m tests.rag_eval.run --grid     # 2×2 瓶颈诊断
.venv/Scripts/python.exe -m tests.rag_eval.run --check    # 低于 baseline.json 则退出码 1
.venv/Scripts/python.exe -m pytest -q tests/test_retrieval.py
.venv/Scripts/python.exe -m pytest -q tests/              # 全量
```

## 7.8 回滚方式

- `git checkout -- backend/services/workflow_evidence.py backend/routes/admin_routes.py`；
- 删除 `backend/services/retrieval.py`、`tests/test_retrieval.py`；
- `tests/rag_eval/baseline.json` 里的 `previous` 就是 Batch 6 的原始数字，回滚后 `--check` 会对上它；
- 回滚后检索恢复「步骤词表计数」，重置接口重新变回 500（本批修复是独立的一处，也可单独保留）。

## 7.9 剩余风险与未验证项（诚实列出）

1. **0.414 有噪声**：29 条样本，一条样本 = 3.4 个百分点；步骤 1 从 1.00 掉到 0.00 就只差一条样本。
   不要拿小数点后三位当结论，方向（+0.24）才是结论。
2. **查询词是我手写的**：`STEP_QUERIES` 是照着「绩效评价这一步通常该找什么」写的，
   可能存在「对着自己造的语料顺手」的成分。换一份真实资料，绝对数值大概率更低。
3. **查询无关性仍为 1.0**：线上每一步只有「步骤意图」这一句查询，同一份资料同一步骤必然得到同一批证据。
   要在线上吃到 0.828 那一档，需要给步骤生成具体子问题（下一批）。
4. **知识库分支仍未评估**：`include_knowledge=True` 要 `KnowledgeBase` 表数据，本批只测项目资料分支；
   知识块的 `step_number` 亲和加成（×1.1）没有任何测试或基数据支撑，是凭经验加的。
5. **索引缓存的键是「块 id + 长度」**：会话内资料不会变，所以安全；但如果将来支持「中途改资料」，
   同长度改写会命中过期索引。届时要换成内容指纹（可复用 `export_cache.content_fingerprint`）。
6. **30 万字截断仍是截断**：只是从「静默」变成「写进 warnings」。真正的解法是分段索引 + 二次检索，本批没做。
7. **BM25 不理解同义表述**：问「钱花完了吗」不会命中「预算执行率 60%」。这是词法检索的天花板，
   要跨过去得上向量或 LLM 改写查询——但那需要先解决「向量库离线可跑」，属于阶段三剩下的大头。

8. **`backend/services/rag_service.py` 已成死代码**：修掉那处误导入后，全仓库再无任何调用点，
   而它 `import` 时会拉起 `HuggingFaceEmbeddings` / `Chroma`（重依赖）。本批没删（删除属于独立动作，
   且它可能是将来「向量检索」的接入点），但要知道：它现在既不生效，也不该有人以为它在生效。

---

# Batch 8 · 每步取证面融合（一次失败尝试 + 一次小胜）

> 一句话：Batch 7 的诊断说「瓶颈是问什么」，本批按这个方向做多查询，
> 第一次（每面硬取一条）**完全无效**，第二次（软融合）拿到 +0.034。
> 收益远小于预期，原因写在第 8.4 节——**3 个证据槽位才是真正的硬约束**。

## 8.1 先定口径，再改代码

| 项 | 设定 |
|---|---|
| 评测 | 沿用 `tests/rag_eval`（29 样本 / 271 块），对照基线 Recall@3 = 0.4138 |
| 上线口径 | 线上仍无「每步具体问题」，所以只允许用**步骤级**信息构造查询 |
| 不许做的事 | 不许把样本里的事实写进查询词（那是抄答案） |
| 判定标准 | Recall@3 提升 **且** 精确率不下降，才算赢 |

`STEP_SUBQUERIES` 按绩效评价的通用结构写三个面（资金 / 实施 / 结果），
不针对任何一份具体资料，换语料仍应成立：

```python
2: ("总投资 投资估算 资金来源 财政拨款 专项债",
    "合同价款 工期 开工 竣工 参建单位",
    "累计支付 支出进度 预算执行率 实际完成 满意度")
```

## 8.2 第一次尝试：每面硬取一条 —— 失败

规则：3 个面各取 top-1，凑满 3 条；不够再用步骤意图补。

```
Recall@3   0.414 → 0.414     （一动不动）
精确率     0.149 → 0.138     （还降了）
MRR        0.362 → 0.362
```

为什么无效：**3 个槽位被 3 个不同的面占住，反而挤掉了基础查询本来能命中的那条。**
步骤 2 有 10 个不同问题、只有 3 个槽位，把槽位分给「资金/工期/支出」三个面，
覆盖的问题数还是 3 个，但选中的未必是这 10 个问题里最该回答的那 3 个。
步骤 6 从 0.00 涨到 1.00、步骤 3 从 1.00 掉到 0.50，互相抵消。

这个失败本身有价值：它证明「多查询」不会自动带来收益，**槽位数量才是分母**。

## 8.3 第二次尝试：软融合 —— 小胜

规则：分数相加，不抢槽位。

```python
fused[chunk] = base_score(chunk) + FACET_WEIGHT * Σ facet_score(chunk)
```

权重敏感性（同一批样本，只改权重）：

| FACET_WEIGHT | Recall@1 | Recall@3 | MRR | 精确率 |
|---|---|---|---|---|
| 0.0（等于单查询） | 0.310 | 0.414 | 0.362 | 0.149 |
| 0.25 | 0.345 | **0.448** | 0.391 | 0.161 |
| 0.5（采用） | 0.345 | **0.448** | 0.391 | 0.161 |
| 0.75 | 0.345 | 0.414 | 0.379 | 0.149 |
| 1.0 | 0.310 | 0.414 | 0.362 | 0.149 |
| 1.5 | 0.276 | 0.379 | 0.328 | 0.138 |

三点判断：

1. **0.25–0.5 是一段平台**，不是尖峰，说明不是碰巧调出来的；
2. **权重过大反而低于基线**（1.5 时 0.379 < 0.414），说明这个机制真的在改变排序，
   而不是「加了个不起作用的开关」；
3. 收益只有 **+0.034 = 1 条样本**。诚实说法：**方向对，量级小**。

按步骤看，融合把步骤 6（评价表）从 0.00 拉到 1.00、步骤 3 从 1.00 降到 0.67（MRR），
其余多数不变——和「1 条样本」的量级一致。

## 8.4 为什么收益这么小（本批最重要的结论）

三个数字放在一起看：

```
槽位 3  × 步骤 2 的 10 个问题   → 上限 0.30（当前 0.20，接近上限）
槽位 3  × 全部 29 条样本        → 完美排序下上限约 0.76（当前 0.448）
把问题本身当查询 + 3 槽位       → 0.828
```

也就是说：

- 排序还有空间（0.448 vs 0.76 上限），但**多查询不是那个空间**——它改变的是「问什么」，
  而问题不在「问什么」不够多，在**槽位不够**；
- 想吃到 0.828 那一档，要么让每条证据装更多信息（一条 480 字的块只讲一件事），
  要么按问题动态给槽位——两者都需要「每步的具体问题」，而线上没有。

**所以下一批不该继续在查询上做文章。** 更该做的是：
把证据块做小（240 字级别）并提高槽位到 5，用 `--limit` 已经能预演这个成本收益
（`步骤意图 × 10 条` 的 Recall@5 = 0.517，精确率 0.069——噪声换召回，值不值要产品定）。

## 8.5 改动清单

| 文件 | 变更 |
|---|---|
| `backend/services/workflow_evidence.py` | 新增 `STEP_SUBQUERIES`（14 步 × 3 面）、`USE_FACET_PLAN`、`FACET_WEIGHT=0.5`；`_score_map` / `_ordered` / `_pick` 抽出复用；`_plan_rank` 改为软融合；`retrieve_step_evidence` 在「无显式 query」时走融合，显式 query 仍走单查询（评估的问题模式不受影响） |
| `tests/test_retrieval.py` | 新增 5 项：显式查询绕过融合、开关关闭即走单查询、面权重归零等价单查询、融合算式重排（用假索引控制分数）、14 步都有取证面 |
| `tests/rag_eval/baseline.json` | 新基线 0.4483，`previous` 保留 Batch 7 的 0.4138，`previous.previous` 保留 Batch 6 的 0.1724 |

## 8.6 验证

```
pytest tests/            129 passed（Batch7 后 124；新增 5 项融合测试）
rag_eval --check         与基线一致
node --test tests/*.cjs  28 passed
test_all_apis.py         46/46
frontend_e2e_check.py    63/63
wf_e2e.py                35/35（真实 14 步，tokens=33456）
git diff --check         通过
```

token 从上批 38,470 降到 33,456（−13%）：这不是检索省下来的，而是**模型输出长度本身在波动**
（同一份提示词两次跑，正文长度差 10% 很常见）。检索侧的实际变化很小：
平均证据 90.7 → 93.2 字/步，约 +20 tokens/步。不要把这个降幅算成优化成果。

## 8.7 可重跑命令

```bash
.venv/Scripts/python.exe -m tests.rag_eval.run            # 0.448 基线
.venv/Scripts/python.exe -m tests.rag_eval.run --grid     # 2×2 诊断（条数 / 查询）
.venv/Scripts/python.exe -m pytest -q tests/test_retrieval.py
# 关掉融合做 A/B：
.venv/Scripts/python.exe -c "from backend.services import workflow_evidence as w; w.USE_FACET_PLAN=False; from tests.rag_eval import run; print(run.evaluate()['metrics'])"
```

## 8.8 回滚方式

- 一个开关即可：`workflow_evidence.USE_FACET_PLAN = False`（行为退回 Batch 7 单查询）；
- 或 `FACET_WEIGHT = 0.0`（测试已断言与单查询逐条一致）；
- 彻底回滚：`git checkout -- backend/services/workflow_evidence.py`，并删掉 `STEP_SUBQUERIES` 相关测试。

## 8.9 剩余风险与未验证项（诚实列出）

1. **+0.034 就是 1 条样本**：29 条样本下任何小于 3.4 个百分点的变化都不该当成结论。
   本批敢保留它，是因为三个指标同向、且权重过大时会反向（有机制证据），不是因为数值大。
2. **取证面是我手写的**：`STEP_SUBQUERIES` 的 42 条查询词按领域常识写，
   但仍可能对我自己造的语料更友好。换真实资料绝对数值会掉，方向不一定保持。
3. **槽位仍固定为 3**：这是当前最大的硬约束（见 8.4），本批没有动，因为动它就要一起改
   token 预算、提示词长度、导出内容，属于独立一批。
4. **没有做「按问题动态分配槽位」**：那需要每步有具体问题，线上没有；
   如果哪天加了「每步问答」入口，这条路径要重新评估。
5. **知识库分支依旧未评估**：融合只作用于项目资料块，知识块的亲和加成（×1.1）仍是凭经验。
6. **BM25 的同义天花板没变**：问「钱花完了吗」不会命中「预算执行率 60%」，融合也救不了。

---

# Batch 9 · 步骤间一致性检查（零 token，把「数字对不上」说出来）

## 9.1 为什么做这一批

Batch 5 解决了「模型看到多少资料」，Batch 3/4 解决了「输出格式对不对」，Batch 6–8 解决了
「证据选得准不准」。剩下一个没人管的问题：**报告里的数字来自 14 个步骤，彼此之间零校验。**

四个真实会出错的来源，都能在现有代码里指出具体位置：

| # | 情形 | 代码出处 |
|---|---|---|
| 1 | 指标分值不合 100（模型设计漏项），但评分照常汇总，报告写「总分 92 分」 | 步骤 3 输出 → 步骤 11 `_compute_score_summary` 只做 `allocation_valid` 判断，不改数字 |
| 2 | `score_summary` 与 `/score-summary` 接口是**两套独立实现**，口径漂移后报告分与界面分不同 | `performance_agent._compute_score_summary` vs `agent_routes.get_score_summary` |
| 3 | 只有 2/20 个指标可核实时，`total_score` 仍是 `5.00`，等级却是「待核实」——数字和等级自相矛盾 | 步骤 11 `score_line` 分支 |
| 4 | 模型在正文里自己写的「综合得分：88.5 分」与程序算出的分不同 | 步骤 11/14 正文，无任何校验 |

判定口径（写进模块 docstring，测试按此断言）：**只报「两处数字确实对不上」**，
附出处步骤与两个数字；不推断缺失内容、不改写任何产出物。判断权交给人。

## 9.2 检查什么：4 组 / 17 类 issue

| 组 | code | 级别 | 触发条件 |
|---|---|---|---|
| 指标体系 | `INDICATOR_MISSING` | error | 步骤 3 无指标体系 |
| | `INDICATOR_ALLOCATION_D{1,2,3}` | error | 分值层级/总分不合规（复用 `score_validator`） |
| | `INDICATOR_ALLOCATION_D4` | **warning** | 产出+效益 ≥60（策略预警，非硬错误） |
| | `INDICATOR_LEAF_TOTAL` | error | 三级指标**满分**合计 ≠ 100 |
| 评分汇总 | `SCORE_SUMMARY_MISSING` | error | 无程序评分汇总 |
| | `SCORE_SUMMARY_STALE` | error | 落库得分 ≠ 按当前指标/评分重算 |
| | `SCORE_COUNT_INVALID` | error | 已评分数 > 总数 |
| | `PARTIAL_SCORE_PRESENTED` | warning | 评分不完整却仍算出正分（应表述为「待核实」） |
| | `GRADE_MISSING` | error | 评分完整但等级仍是「待核实」 |
| | `GRADE_WITHOUT_BASIS` | error | 评分不完整却给了等级 |
| 报告正文 | `REPORT_SCORE_MISSING` | warning | 评分完整但正文没有「综合得分：X 分」 |
| | `REPORT_PENDING_MISSING` | warning | 评分不完整但正文没有「待核实」 |
| | `REPORT_SCORE_MISMATCH` | warning | 正文分 ≠ 程序分（容差 0.1） |
| | `REPORT_GRADE_MISMATCH` | warning | 正文等级 ≠ 程序等级 |
| 步骤与覆盖度 | `REPORT_WITH_INCOMPLETE_STEPS` | error | 报告已生成但缺步骤 |
| | `REPORT_TOO_SHORT` | error | 正文 < 200 字 |
| | `PROJECT_NAME_MISMATCH` | warning | 报告里没有项目名 |
| | `LOW_DOCUMENT_COVERAGE` | warning | 资料覆盖率 < 50% 且确有未引用内容 |

## 9.3 写这一批时踩到的四个坑（都已修，并用变异测试锁住）

1. **满分合计 ≠ 实际得分合计。** 第一版 `_leaf_total` 用 `actual_indicator_score` 求和，
   于是任何一个**合规**体系只要项目得了 88 分，就会报「三级指标分值合计 88，不等于 100」。
   现在拆成 `_leaf_full_total`（判分值设计）与 `_leaf_actual_total`（与落库得分对账）。
   变异测试把这条改回去 → 10 个用例立刻失败。
2. **维度 4 是策略预警，不是错误。** `score_validator.allocation_valid` 明确把
   `dimension != 4` 才算硬校验；真实指标体系（模板二）产出+效益恰好 60 分，把它当错误会天天报红。
3. **报告整章并入了步骤 11 的正文**，同一个矛盾会被报两条。按 `(类型, 值)` 去重，只留最早出处。
4. **「不合格」会被正则截断成「合格」**。字符类写法在多选一里会取最短匹配，
   改成「长词优先」的 alternation：`(不合格|待核实|优秀|良好|合格)`。

## 9.4 接线时发现的真问题：状态接口返回的是**旧结论**

第一次真实 e2e 就抓到了——不是我构造的用例：

```
[PASS] GET status 带 steps一致性检查结果  codes=['INDICATOR_ALLOCATION_D4','PARTIAL_SCORE_PRESENTED']
[PASS] 一致性检查覆盖四类检查项
一致性结论：错误 0 项 / 提示 2 项
[FAIL] 重跑结论与自动检查一致  4 vs 2
```

根因：`/status` 原样透传步骤 14 落库的 `consistency`。而 e2e 在这之前调过
`POST /workflow/save-report`（把报告换成了 30 字测试内容）——**报告已经变了，徽标还是绿的。**

这正是这个功能本身要防的事，落在自己身上。修复：

- `/status` 在 `final_report` 非空时**按当前状态现算**（实测 30,146 字报告 0.37 ms/次，
  比它上面那条 DB 查询还便宜），保证手改分值/章节后不会看到旧结论；
- 重跑接口改走 `_raw_workflow_values`，**不再复用 `_get_workflow_values`**：后者会给缺 id 的
  指标补「按名称拼」的 id，而 `indicator_score_map` 是按真实 id 存的，两套 id 混用会误报「得分已过期」。

第二次 e2e 的前后对比，说明修复生效：

| 时点 | 报告状态 | 检查结论 |
|---|---|---|
| 步骤 14 刚组装完 | 30,146 字完整报告 | 0 错误 / 1 提示（D4） |
| `save-report` 换成 30 字后 | 30 字 | **1 错误**（`REPORT_TOO_SHORT`）/ 2 提示（D4、`PROJECT_NAME_MISMATCH`） |
| 重跑接口 | 同上 | 与 status 完全一致 |

## 9.5 真实运行看到了什么（不是构造的样本）

- **`PARTIAL_SCORE_PRESENTED`**（第一次 e2e）：`1/15 个指标可核实，综合得分仍算出 2.4`。
  这正是 9.1 的第 3 种情形，真实发生了——报告原本会把它写成「综合得分 2.40 分」。
- **`INDICATOR_ALLOCATION_D4`**（两次 e2e 都有）：`一级产出+效益=60.0, 二级=60.0, 三级=10.0`。
  真实指标体系的产出+效益恰好卡在 60 分线上，按错误报会导致每次运行都红，所以必须是 warning。
- **两次 e2e 的错误数都是 0**：程序算的数字与报告写的数字本身是对的。
  这说明这批没抓到「假问题」——检查器不靠制造噪音来证明自己有用。

## 9.6 改动清单

| 文件 | 改动 |
|---|---|
| `backend/services/consistency.py` | **新增**，4 个检查器 + `check_consistency` + `summary_line`，零模型调用 |
| `backend/agents/performance_agent.py` | `_node_step14` 末尾调 `_run_consistency_check`；写入 `state["consistency"]`，summary 并入 `warnings`（不重复）；备份白名单加 `consistency` |
| `backend/agents/states/performance_state.py` | 新增字段 `consistency: Dict[str, Any]` |
| `backend/routes/agent_routes.py` | status 响应加 `consistency`（报告非空时现算）；新增 `POST /workflow/{session_id}/consistency-check`；新增 `_raw_workflow_values`、`_consistency_report` |
| `frontend/client/index.html` | 第 14 步一致性徽标（ok/warn/err 三态 + 问题清单 + 「重新检查」按钮 + `rerunConsistencyCheck`），CSS 5 条 |
| `tests/test_consistency.py` | **新增** 27 项 |
| `tests/test_engineering_regressions.py` | +2 项接口级（status 现算口径 / 重跑并落库）+1 项越权参数 |
| `tests/_mutate_consistency.py` | **新增**，7 个变异点的可重跑检验脚本 |
| `frontend_e2e_check.py` | +2 项（G 段：非本人会话不泄露） |
| `wf_e2e.py` | +4 项（status 带结果 / 四类检查项 / 重跑 / 重跑与自动检查一致） |

接口兼容性：**未改任何既有路径与语义**，只在 status 响应里新增 `consistency` 字段、新增一个端点。

## 9.7 验证

```
pytest tests             129 → 159 passed（+30：一致性 27 + 接口级 3）
node --test 四个 .cjs     28 passed（不变）
test_all_apis.py          46/46（不变）
frontend_e2e_check.py     63 → 65/65（+2）
wf_e2e.py                 35 → 39/39（真实 14 步，tokens=29,602，213s）
rag_eval --check          与基线一致（本批未触碰检索）
变异检验                  7/7 全部被抓（见 9.3 / 9.7 命令）
git diff --check          通过
```

第一次 e2e 是 38/39，唯一失败项就是 9.4 那个真问题；修完复跑 39/39。

## 9.8 可重跑命令

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_consistency.py     # 27 项
.venv/Scripts/python.exe tests/_mutate_consistency.py               # 7 个变异点，应全部「抓住」
.venv/Scripts/python.exe -m pytest -q tests/test_engineering_regressions.py -k consistency
# 接口级（需服务在跑）
.venv/Scripts/python.exe frontend_e2e_check.py                      # G 段 2 项
.venv/Scripts/python.exe wf_e2e.py                                  # 39 项（会真实调模型）
```

`tests/_mutate_consistency.py` 会临时改写源码再复原（`finally` 保证），只用于检验测试有效性，不属于回归套件。

## 9.9 回滚方式

- 单点关闭：删掉 `_node_step14` 里的 `self._run_consistency_check(state)` 一行，其余全部失效；
- 前端隐藏：`selectWfStep` 里去掉 `if(sn===14)inner+=buildConsistencyBadge();`；
- 彻底回滚：`git checkout -- backend/agents/performance_agent.py backend/agents/states/performance_state.py backend/routes/agent_routes.py frontend/client/index.html`，
  删 `backend/services/consistency.py` 与 `tests/test_consistency.py`、`tests/_mutate_consistency.py`，
  并回退 `wf_e2e.py` / `frontend_e2e_check.py` 的新增断言。

## 9.10 剩余风险与未验证项（诚实列出）

1. **只查「程序能对账的数字」。** 定性结论（因果关系、责任归属、金额口径）不在范围内，
   报告里「资金已全部拨付」与附件「拨付 80%」这种矛盾查不出来。
2. **正则只认固定说法。** 得分只匹配「综合得分/总得分/综合评分 + X 分」，等级只匹配
   「评价等级/综合等级/评定等级 + 等级词」。模型换个说法（「得分 88.5」「评级良好」）会**漏报**，
   不会误报——这是刻意选的偏保守方向。
3. **D4 是 warning 不是 error。** 真实模板经常 ≥60，若改 error 会天天红；代价是这个提示容易被忽略。
4. **状态接口每次请求多一次计算**：0.37 ms + 一行 `score_validator` INFO 日志。
   完成态会话轮询频率低，可接受；若将来 status 被高频轮询，应加短 TTL 缓存。
5. **`_consistency_report` 只在 `final_report` 非空时现算。** 用户把报告删空时会退回旧结论（边缘情形，未处理）。
6. **徽标只显示在第 14 步。** 其它步骤看不到一致性结论；是否有必要在步骤 11（评分汇总）也提示，未做产品判断。
7. **未做跨源数字核对**：报告里的金额/数量与原始资料（步骤 1/2 结构化清单）比对，
   需要先做结构化抽取，属独立一批。

### 顺带发现、未修（超出本批范围）

真实运行两次都出现：

```
[Step 2] 内容索引失败（非致命）: 'dict' object is not callable
```

`ContentIndexer` 用实例属性 `self.index: Dict[str, IndexEntry]` 覆盖了同名方法 `def index(...)`，
调用处 `content_indexer.index(raw_texts)` 必然 `TypeError`，被 `except` 吞成一条 warning。
后果：`content_index`、`effective_data_categorized` **在真实运行中恒为空**，
「每条内容可追溯到原文出处」这个功能实际是死的。

复现（不需要跑工作流）：

```bash
.venv/Scripts/python.exe -c "from backend.agents.tools.content_indexer import ContentIndexer as C; C().index({'a.txt':'x'})"
```

修法明确（属性改名 `_index`，6 处），但它会**真的开始产出内容索引**，进而改变步骤 2 之后的
提示词内容与状态体积——必须单独一批、单独取基线。建议列为下一批第一项。

---

# Batch 10 · 让「原文可追溯」真的跑起来（一个被 `except` 吞掉的功能）

## 10.1 起因：Batch 9 顺手记下的那条日志，是致命的

真实运行日志里每次都有一行：

```
[Step 2] 内容索引失败（非致命）: 'dict' object is not callable
```

根因很朴素：`ContentIndexer.__init__` 里 `self.index: Dict[str, IndexEntry] = {}`
与同名方法 `def index(self, raw_texts)` 撞名，实例属性遮蔽方法，
`indexer.index(raw_texts)` 必然 `TypeError`，然后被 step2 的 `except` 吞成一条
"非致命"warning。**这个子系统从未运行过一次。**

修之前先把影响面查清楚，结果比预想严重。四步提示词用到的资料变量（`default_prompts` 实测）：

| 步骤 | 资料入口 | 索引坏掉时它拿到什么 |
|---|---|---|
| 3 构建指标体系 | `{effective_data_categorized}` + `{content_index_summary}` | `{}` + "暂无索引" |
| 7 指标得分分析 | `{effective_data_categorized}` | `{}` |
| 9 问题及原因 | `{effective_data_categorized}` | `{}` |
| 12 基本情况 | `{raw_text_full}` + `{effective_data_categorized}` | 有原文，分类为空 |
| 4/5/8/10 | `{effective_data_summary}`（步骤 2 的模型输出，3000 字） | 正常 |

注意步骤 3 那一行：**它没有 `raw_text`，这两个变量就是它的全部输入。**
也就是说，指标体系一直是"只给项目名、不给资料"建出来的——
这解释了为什么真实运行的一级指标永远是「决策/过程/产出/效益」四件套。

## 10.2 修之前先量：不封顶会灌多少

| 语料 | 块数 | 建索引 | `get_categorized_content` | 分类内容体积 |
|---|---|---|---|---|
| e2e 样本 354 字 | 1 | 0.6 ms | 0.0 ms | 354 字 |
| 评估语料 9,925 字 | 75 | 1.7 ms | 0.1 ms | 9,922 字 |
| 合成 4.2 万字 | 14 | 3.3 ms | 0.0 ms | 41,226 字 |
| 合成 1,000 短段 | 1,000 | 29 ms | 12 ms | — |

两个结论：
1. **索引本身几乎不花钱**（毫秒级），没有理由为性能犹豫；
2. **注入体积≈语料本身**（1:1），因为 `get_categorized_content()` 就是把段落原文
   按类别重新拼一遍。四个步骤各灌一遍 = 把整份语料复制四份进提示词，
   而步骤 12 提示词里本来就有 `raw_text_full`，纯属重复。

所以修 bug 必须同时加预算，否则"修好一个 bug"等于"每次运行多烧几万 token"。

## 10.3 三层改动

1. **修遮蔽**：`self.index` → `self._entries`（6 处），方法名不动。
2. **注入预算**（`_categorized_excerpt`）：
   - 总预算 `CATEGORY_BUDGET=6000` 字符，单类别 `CATEGORY_ITEM_BUDGET=1500`；
   - 取用顺序 `CATEGORY_ORDER`：项目概况 → 资金管理 → 绩效目标 → 实施过程 → 组织管理 → 其他
     （预算不够时先保证"项目是什么、钱从哪来、要达成什么"）；
   - 每个被截断的类别写清「共 X 字，此处展示前 Y 字，其余 Z 字未放入本步骤」，
     末尾再交代「另有 N 个类别、共 M 字未放入本步骤」——延续 Batch 5 的口径：
     可以少给，不能假装给全了；
   - 步骤 12 跳过（`CATEGORY_SKIP_STEPS={12}`），因为同提示词里已有 `raw_text_full`。
3. **持久化**：`effective_data_categorized` 进备份白名单。否则服务器重启后从磁盘恢复状态，
   步骤 3/7/9 又变回"看不到资料"——同一个 bug 换个入口复发。

## 10.4 顺手删掉一个"一开就是 10 条假警告"的检测器

索引一修好，`_detect_numeric_contradictions`（同类别内任意两个数相差 2 倍即"数值矛盾"）
也跟着复活。在 9,925 字评估语料上产出 **10 条，全部误报**：

```
1. 资金管理 | 差异较大(数值): 2.0 / 4.0 / 3.0        ← 页码、年份、个数
2. 资金管理 | 差异较大(万元): 860 / 912 / 486        ← 工程费/其他费/预备费本来就不同
3. 资金管理 | 差异较大(%):   5.0 / 60.0              ← 满意度(5分制) vs 形象进度
```

要判对必须带「主体 + 口径 + 单位」（同一指标的两种口径不一致才算矛盾），那是抽取任务，
正则做不到。这些条目会进 `warnings`，而 `warnings` 会进提示词——留着只会污染输出。
**直接删掉，不做半成品**；同时修掉 `_detect_factual_contradictions` 的双向重复
（原来 `(已完成, 在建)` 和 `(在建, 已完成)` 各报一条）。保留的"事实矛盾"只剩
显式状态词互斥，9 个关键词，误报率实测为 0。

## 10.5 前后对比（真实 14 步，同一份 354 字样本）

| 指标 | Batch 9（索引坏） | Batch 10（修好） |
|---|---|---|
| `content_index` 条目 | 0 | 1 |
| `effective_data_categorized` | 0 类 / 0 字 | 1 类 / 354 字 |
| 步骤 3 指标体系**引用样本事实** | 3/16 | **16/16** |
| 步骤 7 正文长度 | 1,094 字 | 8,824 字 |
| 步骤 7 **引用样本事实** | 1/16 | **16/16** |
| 一级指标数 | 18 | 23 |
| 最终报告 | 29,928 字 | 47,854 字 |
| 输出 tokens | 29,602 | 43,137（**+46%**） |

样本事实清单（16 项）：`5000 / 3.2 / 1.8 / 4.5 / 220 / 1800 / 100% / 90% / 85% /
4000 / 1000 / 3200 / 64% / 雨水管网 / 路灯 / 行道树`。修后指标体系把它们全接住了——
"改造主路 3.2 公里、新建雨水管网 4.5 公里、安装路灯 220 盏"这类具体内容终于进了指标，
而不是继续输出通用四件套。

**代价要认**：输出 tokens +46%。这不是开销失控，而是"以前那几章本来就没内容"
（步骤 7 只有 1,094 字）。但也要说清：**评分完整度没有改善**（2/23），
因为这份样本只有 354 字，本来就没有可核实的证据。
"修好之后评分会自动变完整"这件事**本批没有验证**（见 10.10 第 1 条）。

## 10.6 提示词体积增量（不调模型，直接渲染模板量）

同一份 9,925 字评估语料，四步 Human Prompt 的前后长度：

| 步骤 | 改前 | 改后 | 增量 |
|---|---|---|---|
| 3 | 82 | 8,208 | +8,126（含 `content_index_summary` 3,182） |
| 7 | 86 | 5,034 | +4,948 |
| 9 | 89 | 5,037 | +4,948 |
| 12 | 9,234 | 9,275 | +41（跳过重复注入） |
| 合计 | 9,491 | 27,554 | **+18,063 ≈ +11.3k input tokens** |

关键性质：这是**常数上限**，不随语料线性增长——预算封在 6000，
40 万字资料和 1 万字资料的注入量一样。调小只要改 `CATEGORY_BUDGET`。

## 10.7 改动清单

| 文件 | 改动 |
|---|---|
| `backend/agents/tools/content_indexer.py` | `self.index` → `self._entries`（6 处）；删除 `_detect_numeric_contradictions`（含调用）；`_detect_factual_contradictions` 用 frozenset 去重；清理未用导入 `Tuple` |
| `backend/agents/performance_agent.py` | 新增 `CATEGORY_BUDGET / CATEGORY_ITEM_BUDGET / CATEGORY_ORDER / CATEGORY_SKIP_STEPS` 与 `_categorized_excerpt`；`_build_human_vars` 改用它；备份白名单加 `effective_data_categorized` |
| `tests/test_content_index.py` | **新增** 16 项（含"跑一次 `_node_step2`"的集成级回归） |

接口、SSE、DB 字段零改动。前端未动（`indexLinks` 面板仍无人填，见 10.10 第 6 条）。

## 10.8 验证

```
pytest tests          159 → 175 passed（+16）
node --test 四个 .cjs  28 passed（不变）
test_all_apis.py      46/46（不变）
frontend_e2e_check.py 65/65（不变）
wf_e2e.py             39/39（真实 14 步，tokens=43,137，220s）
rag_eval --check      与基线一致（本批未触碰检索）
git diff --check      干净
```

**测试有效性**：把属性名改回 `self.index` 再跑，10 项测试立刻变红
（含 `test_step2_actually_populates_index_and_prompt_input`）。
这个 bug 能活这么久的原因就是：索引器单独测、提示词单独测，中间那一环没人看。
所以新增的回归测试刻意从「步骤 2 跑完之后」往回看。

服务端日志对照：修前每次运行都有 `内容索引失败（非致命）`；
修后是 `已切分 1 个内容块 / 索引构建完成: 有效chunk=1, 分类=1, 矛盾=0`。

## 10.9 可重跑命令

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_content_index.py      # 16 项
# 复现原 bug（应报 TypeError；修好后此命令不再出现该异常）
.venv/Scripts/python.exe -c "from backend.agents.tools.content_indexer import ContentIndexer as C; C(use_semantic=False).index({'a.txt':'x'})"
# 量化注入体积与四步提示词增量
.venv/Scripts/python.exe -m pytest -q tests/test_content_index.py -k budget
# 真实端到端（会调模型，约 4 分钟 / 4 万 output tokens）
.venv/Scripts/python.exe wf_e2e.py
```

## 10.10 回滚方式

- 只关注入、保留索引（最小回退）：把 `CATEGORY_SKIP_STEPS` 改成
  `frozenset({3, 7, 9, 12})`，四个步骤全部跳过注入 —— 提示词回到 Batch 9 的样子，
  索引本身仍然建得起来；
- 只调成本：改 `CATEGORY_BUDGET`（默认 6000，减半即约减半注入量）；
- 彻底回滚：`git checkout -- backend/agents/tools/content_indexer.py backend/agents/performance_agent.py`，
  删 `tests/test_content_index.py`。

## 10.11 剩余风险与未验证项（诚实列出）

1. **只在 354 字样本上做了端到端**。那份样本信息很密（一段话含 16 项事实），
   所以"16/16 命中"不能外推到真实长篇资料。真实资料上效果如何**未验证**——
   需要一份可共享的脱敏长文档（本轮手上没有）。
2. **+46% 输出 tokens 是实测**，成本真实存在。输入侧有 6000 上限封顶，
   输出侧没有上限：步骤 3/6/7 变长后，后续步骤的输入也会跟着变长（滚雪球）。
   若成本不可接受，优先调 `CATEGORY_BUDGET`（6000 → 3000，约减半）。
3. **评分完整度仍是 2/23**。"能不能核实"由步骤 7 的判定决定，与资料是否可见不是同一件事；
   本批只证明了资料进去了，没证明评分变准。
4. **定位粒度不一样**：索引给出的是「文件::第N段」，Batch 5 的 `doc_coverage` 给的是
   「第N页/章节标题」。两套定位没打通，报告里的"原文出处"仍然不可点击。
5. **`content_index` 不进备份**（只有 `effective_data_categorized` 进）。重启后
   前端索引表和步骤 3 的"内容索引表"会缺，但步骤 3/7/9 的资料输入不受影响。
   取舍理由：`content_index` 体积≈语料的 1/3 且只有展示用途。
6. **前端「原文索引」面板仍是空壳**：`indexLinks` 有 DOM 容器、没有数据源，
   `content_index` 也没有任何接口暴露。要接通需要新增端点（体积大，需分页）。
7. **矛盾检测只剩 9 个状态词**（已完成/在建/已验收…），同义表达（"尚未完工""主体已封顶"）抓不到。
8. **分类是关键词计分，一段只归一类**：长段落里"资金管理"和"实施进度"混在一起时只会进一个桶，
   所以步骤 3 看到的"分类"未必干净。切块粒度受段落长度影响（我的合成文档 3,000 字/页 → 1 块），
   真实 PDF 行短，块更碎，分类质量可能更好也可能更碎——未测。

---

# Batch 11 · 步骤 7 的「评分没落地」到底是什么问题（先取证，再改）

## B11.1 起点：一个被误读的数字

Batch 10 的交付里写了「评分完整度仍是 2/23」。这个数字本身没错，但它把三件**性质完全不同**的事
压成了一个数：

1. 模型看过资料，明确说「这个我评不了」；
2. 系统把模型给出的、有依据的评分丢掉了；
3. 模型压根没提这个指标。

第 1 种是**正确行为**（提示词就要求「没有依据的指标写待核实，不输出分值」），
第 2 种是缺陷，第 3 种是模型输出不完整。三种混在一起，既没法判断该不该修，也没法验收修完的效果。

所以本批第一步不是改代码，是**逐条对账**。

## B11.2 取证：23 条评分逐条查明原因

方法：状态备份里有步骤 7 的全部输入（指标体系、`evaluation_table`、分类原文、证据池）和
模型的原始输出。本地重放即可**逐字复现**当时的判定，零 token
（`tests/_probe_step7.py`，可直接重跑）。

以备份 `dc0c6294`（Batch 10 那次真实 14 步，23 个叶子）为例：

| 原因 | 条数 | 是不是缺陷 |
|---|---|---|
| 精确命中检索池编号 | 2 | 正常 |
| `actual_score` 写「待核实」 | 16 | **不是缺陷**，提示词要求的合法拒绝 |
| 编号被自由文本包裹（`P1-10-1、P1-12-1、资料「三、绩效目标」`） | 2 | **是缺陷**，被精确匹配丢掉 |
| 只引用原文小节名（`资料「四、资金安排」`） | 3 | **是缺陷**，同上 |

关键发现（代码级）：

- `_node_step7` 的判定是 `evidence in evidence_ids`，而 `evidence_ids` 只有**本步骤检索到的
  3 个 chunk**（`P1-10-1 / P1-12-1 / P1-15-1`）；
- 但提示词（`STEP7_HUMAN_TEMPLATE` + 可追溯证据段）同时下发了 `evaluation_table`，
  里面外露着 `P1-2-1 / P1-11-1 / P1-14-1` —— **这些编号不在检索池里**；
- 也下发了带小节标题的原文（`一、项目概况` … `四、资金安排`）。

也就是说：模型看到三种可引用形式，程序只认其中一种，而且认的那一种并非提示词强调的形式。
这不是「校验太严」，是**校验基准与下发口径不一致**。

## B11.3 三处真实缺陷

| 编号 | 缺陷 | 证据 |
|---|---|---|
| D1 | 引用校验基准 = 检索池，与提示词下发口径不一致 → 真实评分被误杀 | 23 条里 5 条（B11.2） |
| D2 | 合法拒绝被当成格式错误：`warnings` 写「16 条输出未进入结构化结果」，同一份 `step_results[7]` 的 `structured_status` 却是 `ok` | 备份里 warnings 第 1 条原文 |
| D3 | 「没落地」对外只有一个 `missing_count`，原因不可区分 | `/score-summary` 只返回 `missing_count` |

D2 的原文（改前）：

```
步骤 7 有部分输出未进入结构化结果：scores['L3_01_01_01'].actual_score 不是数字，已忽略；
scores['L3_01_02_01'].actual_score 不是数字，已忽略；… 等 16 条
```

这 16 条全是模型写的「待核实」。系统一边说「结构化输出通过」，一边说「16 条被忽略」，
用户只会得出一个结论：系统坏了。

## B11.4 做法

1. **新增 `backend/services/evidence_anchor.py`**（纯程序、零 token）
   把引用解析成五类锚点，按可信度排序，并记录落到了哪一类：

   | kind | 含义 | 判据 |
   |---|---|---|
   | `evidence_id` | 检索证据编号 | id 出现在本步骤检索池 |
   | `material_id` | 材料内编号 | id 出现在**本次真实下发的材料**里 |
   | `evidence_locator` | 文件名 + 行/段号 | `fe_wf_sample.txt-第10段` 的行号与某个 chunk 的 `locator` 一致，且文件名对得上 |
   | `section_ordinal` | 只给序号的小节 | `第四部分` 按材料小节序号还原成 `资金安排` |
   | `heading` | 原文小节名 | 引号片段能对上下发原文的某个小节标题（归一化后互为包含，≥3 字） |

   后两类是第 6 轮 e2e 暴露出来的：提示词里 `render_evidence` 印的是
   `[P1-10-1] fe_wf_sample.txt（提取文本第10行）`，模型把这行读成了「文件名 + 段号」，
   20 条引用写成 `fe_wf_sample.txt-第1段` / `fe_wf_sample.txt-第四部分`。
   **引用是真的，只是换了写法**——按旧的精确匹配，这 20 条全部被判「无法定位」。

   仍然拒绝：编造的编号（`P9-99-9`）、材料里不存在的小节（`资料「八、考核评分」`）、空引用、
   `资料「无」` 这类占位词、**只给文件名不给行号**（`fe_wf_sample.txt`，落不到具体位置）、
   **文件名对不上**（`other.txt-第10段`，不退回「唯一候选」去猜）。
   §B11.6 的变异测试专门验证「闸门没被放开」。

2. **`_step7_material(state)`：校验基准与提示词同源**
   直接拼 `evaluation_table` + `indicator_system` JSON + `_categorized_excerpt(step=7)`，
   即 `STEP7_HUMAN_TEMPLATE` 的三项变量。校验的是「模型实际看到的东西」，
   不是「我们以为它看到的东西」。

3. **处置台账 `score_disposition`**
   逐叶子记录 6 类状态，并单列 `unmatched`（模型给了分但指标 id 不在体系内）：

   | status | 含义 |
   |---|---|
   | `accepted` | 已落地（有分值、有可定位引用、有理由） |
   | `model_pending` | 模型判定资料不足（写「待核实」或 JSON `null`） |
   | `anchor_unresolved` | 给了分值但引用落不到材料里 |
   | `score_invalid` | 分值本身不合法（超满分、条目不是对象、缺字段） |
   | `reason_missing` | 有分有据但没写理由 |
   | `not_mentioned` | 模型没提这个指标 |

4. **`structured_output`：`null` 与「待核实」不再逐条告警**
   `classify_score` 把 `None` / 空串 / 待核实类词算 `pending`（合法拒绝），
   只对真正非法的值（`"很高"`、布尔）记 warning。修掉 D2 的自相矛盾。

5. **对外口径**
   - `GET /workflow/{sid}/status` 新增顶层 `score_disposition`（老会话返回 `{}`）；
   - `GET /workflow/{sid}/score-summary` 的 `data` 新增 `disposition`（新增字段，向后兼容）；
   - 步骤 7 正文前缀与 `warnings` 用同一行结论（去重），替换原来那句
     「部分指标缺少可核实的评分依据」；
   - 前端步骤 7 新增徽标：三态 + 分项计数 + 两张可展开明细表（未落地项 / 体系外 id）。
     标题按主因分四种写法（全部落地 / 模型待核实 / 模型未提及 / 分项见下），
     **不把「模型未提及」说成「资料不足」**——这两句在真实数据里对应完全不同的处置动作。

6. **人工补分后台账跟着变**（`refresh_disposition`）
   否则用户在步骤 7 手补 5 个指标，徽标还显示旧的「1/22」。只重算「已落地」这一栏，
   不重新推断「当时为什么没落地」——那只有步骤 7 当场知道，事后推断等于编。

## B11.5 改动清单

**新增**

| 文件 | 行数 | 作用 |
|---|---|---|
| `backend/services/evidence_anchor.py` | 288 | 引用锚点解析（5 类）+ 分值分类（3 类） |
| `tests/test_evidence_anchor.py` | 30 用例 | 锚点/分类的全部边界，含「必须拒绝」的反例 |
| `tests/test_step7_scoring.py` | 25 用例 | 步骤 7 落地与台账的端到端行为 |
| `tests/test_score_disposition_badge.cjs` | 10 用例 | 直接 eval `index.html` 里的真实徽标函数，喂真实形态台账；查 `undefined/NaN` 泄漏、标题与计数是否一致、转义 |
| `tests/_mutate_step7.py` | — | 变异测试（15 个变异点） |
| `tests/_probe_step7.py` | — | 真实备份重放工具（零 token 复现判定） |
| `tests/_probe_unresolved.py` | — | 列出某一轮里仍未落地的引用原文，用来判断缺哪类锚点 |
| `tests/_smoke_anchor.py` | — | 18 条真实引用形态的冒烟（不依赖 pytest 夹具，便于对照原文） |

**修改**

| 文件 | 改动 |
|---|---|
| `backend/agents/performance_agent.py` | 模块级 `DISPOSITION_LABELS` / `summarize_disposition` / `refresh_disposition`；`_step7_material`；`_node_step7` 重写判定与台账（`chunks` 与 `evidence_ids` 同源，都只取 `kind=="project"`）；台账 accepted 补 `evidence_anchor` 与 `refresh_disposition` 对齐；备份白名单加 `score_disposition` |
| `backend/agents/tools/structured_output.py` | `_validate_score_items` 区分「合法拒绝」与「格式错误」 |
| `backend/agents/states/performance_state.py` | 新增字段 `score_disposition` |
| `backend/routes/agent_routes.py` | status 加 `score_disposition`；score-summary 加 `disposition`；`save-indicator-scores` 刷新台账 |
| `frontend/client/index.html` | 步骤 7 台账徽标（CSS 9 条 + `buildScoreDispositionBadge` + 状态读取/重置） |
| `tests/test_engineering_regressions.py` | 新增 2 项：status 暴露台账、人工补分刷新台账 |
| `wf_e2e.py` | 新增 7 项台账断言 |
| `frontend_e2e_check.py` | 新增 H 段 5 项 |

## B11.6 真实数据前后对比（同一份备份重放，零 token）

这是本批唯一「变量可控」的对比：输入完全相同，只换判定逻辑。

| 备份 | 叶子 | 模型给出 | 改前落地 | 改后落地 | 改后分项 |
|---|---|---|---|---|---|
| `dc0c6294`（Batch 10 那轮） | 23 | 23 | **2**（4.0 分） | **7**（13.0 分） | 已落地 7 / 模型待核实 16 |
| `4a120084`（本批第 2 轮 e2e） | 25 | 25 | **2**（5.5 分）\* | **6**（16.0 分） | 已落地 6 / 模型待核实 18 / 引用无法定位 1 |
| `b20e9f85`（第 6 轮 e2e，五类锚点上线前） | 23 | 23 | **3**（6.0 分） | **15**（30.0 分） | 已落地 15 / 引用无法定位 8 |

\* `4a120084` 这一行必须加注：备份里 `indicator_score_map` 实际有 **5 条、14.0 分**，
不是 2 条。这轮步骤 7 重跑过，`indicator_score_map` 里带着上一轮的评分，
`_node_step7` 的「沿用已有评分」分支把它们保留了。重放工具模拟的旧规则没有复现该分支，
所以「改前 2 条」只是**解析规则单独作用**下的数字，不是这轮真实的历史结果。
`dc0c6294` 没有这个问题（模拟 2 条 = 落库 2 条），那条一致性正好校验了重放方法可信。

第三条 `b20e9f85` 是本批最关键的一次验证：新增两类锚点后，同一份输入从
3/23（6.0 分）变成 15/23（30.0 分）。落在哪一类：

| 锚点类型 | 条数 |
|---|---|
| `evidence_id`（检索池编号） | 3 |
| `section_ordinal`（`fe_wf_sample.txt-第四部分`） | 10 |
| `evidence_locator`（`fe_wf_sample.txt-第10段`） | 2 |
| 仍无法定位 | 8 |

**剩下 8 条我判断不该放行**，理由不是「解析器写不出来」：

- 4 条只写 `fe_wf_sample.txt`，连行号都没有；
- 4 条写 `fe_wf_sample.txt-第1段`，但本步骤只检索到第 10/12/15 行——**第 1 段的原文不在程序手里**，
  无法核对这段引用是否真的支持该评分；
- 这 8 条里 7 条的 `reason` 自己写着「无法判断 / 无法评价，暂按 1.00 分基础分处理」。
  放行它们会把「模型对缺失资料的妥协」记成「已评估」，并把总分从 30.0 抬到 37.5——
  数字更好看，但那是假的。

要想真的支持这类引用，得让检索把「材料第 1 段」也召回，或者按行号回查原文切片。
那是检索侧的改动，不在本批范围（本批是「不丢真实评分」，不是「手工补召回」）。


## B11.7 七轮真实 14 步：跑间差异**远大于**本批收益（本批最重要的结论）

本批前后一共跑了七轮真实 14 步（同一份 354 字样本、同一模型配置）。步骤 7 的实际结果是：

| 轮次 | 备份 | 叶子 | 模型给出的评分数 | 步骤7 正文长度 | 落地 | 主因 |
|---|---|---|---|---|---|---|
| A（Batch 10） | `dc0c6294` | 23 | 23 | 8,824 字 | 7 | 16 条模型主动待核实 |
| B（本批 e2e #2） | `4a120084` | 25 | 25 | 9,483 字 | 6 | 18 条 JSON `null` 拒绝 |
| C（本批 e2e #1） | — | 24 | 6（其中 4 条 id 不在体系内） | 883 字 | 1 | 模型只给了 6 条 |
| D（本批 e2e #3） | — | 22 | **1** | **155 字** | 1 | 模型只给了 1 条 |
| E（五类锚点前） | `b20e9f85` | 23 | 23 | — | 15 | 20 条写「文件名+段号/序号」 |
| F（本批 e2e #4） | `b425eb90` | 20 | 5（其中 2 条 id 不在体系内） | — | 3 | 模型只给了 5 条 |

必须说清楚的三件事：

1. **本批修的是「系统丢分」，不是「模型不给分」。** 在模型输出完整的 A/B/E 三轮，落地数
   从 2 提升到 7、6、15，这部分是实打实的。
2. **在 C/D/F 三轮，约束不在程序侧，而在模型输出完整度**（6 条、1 条、5 条）。这三轮里
   台账明确指向「模型未提及 21/22/15 项」——改前这种情况只会显示
   「部分指标缺少可核实的评分依据」，看不出是谁的问题。
3. **跑间差异大到不能拿单轮数字做验收。** 同一个模型、同一份输入，步骤 7 的输出从 155 字
   到 9,483 字，模型给出的评分数从 1 到 25。所以本批的验收口径是「同一份备份重放」（B11.6），
   不是「跨轮对比」。

指标 id 层级也每轮不同（20/22/23/24/25 个叶子），这是步骤 3 的自由度，本批未触碰。

**F 轮（第 7 轮）暴露了一个已知未支持形态，留作下一批首项**：

```
evidence_id = 'fe_wf_sample.txt 第14行'    score=2.0
evidence_id = 'fe_wf_sample.txt 第14行'    score=5.0
```

两条都是**真实评分**（reason 里有「已支出3200.00万元，支出进度64.00%」这类具体数字），
写法也是最规范的「文件名 + 第N行」——正是提示词 `render_evidence` 印的格式。
但本步骤只检索到第 10/12/15 行，**第 14 行不在 chunks 里**，所以被判 `anchor_unresolved`。

这不是解析器的问题（`第14行` 能正确解析），而是判据问题：现在的 `evidence_locator`
要求行号必须对上某个检索块。要支持它，得新增一类「弱定位」（文件名 + 行号在原文行数范围内），
并且**不能**直接算作 `accepted`——因为它只证明「这一行存在」，不证明「这一行支持该评分」，
而 `第1段` 那 3 条「无法评价、暂按基础分」正是会借这个口子混进总分的东西。
正确做法是新增独立状态（如 `anchor_weak`）：不计入 `total_score`，但单列出来供人工确认。
涉及状态字段、前端、测试与备份白名单（`all_raw_texts` 目前不在白名单，重放拿不到行数），
所以不在本批做。

## B11.8 可重跑命令

```bash
# 单元 / 回归（无需服务器）
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval
.venv/Scripts/python -m pytest tests/test_evidence_anchor.py tests/test_step7_scoring.py -q

# 18 条真实引用形态的冒烟（打印每条的判定与命中，便于和原文对照）
.venv/Scripts/python tests/_smoke_anchor.py

# 真实备份重放：改前/改后各落地几项（零 token，秒级）
.venv/Scripts/python tests/_probe_step7.py                      # 默认 dc0c6294
.venv/Scripts/python tests/_probe_step7.py 4a120084-9d23-41dc-b924-ef96eeed802c
.venv/Scripts/python tests/_probe_step7.py b20e9f85-b888-4f29-b271-f7139690202a

# 还差哪类锚点：列出某一轮里仍未落地的引用原文（含 evidence_id / reason）
.venv/Scripts/python tests/_probe_unresolved.py b20e9f85-b888-4f29-b271-f7139690202a

# 变异测试：确认用例真的会咬人（会临时改源码，结束时校验字节一致）
.venv/Scripts/python tests/_mutate_step7.py

# 需要服务器（本机有代理时务必绕过）
NO_PROXY='*' HTTP_PROXY= HTTPS_PROXY= .venv/Scripts/python test_all_apis.py        # 46/46
NO_PROXY='*' HTTP_PROXY= HTTPS_PROXY= .venv/Scripts/python frontend_e2e_check.py   # 70/70
NO_PROXY='*' HTTP_PROXY= HTTPS_PROXY= .venv/Scripts/python wf_e2e.py               # 46/46（约 4~5 分钟）
node --test tests/test_shared_frontend.cjs tests/test_workflow_sse.cjs \
            tests/test_client_modules.cjs tests/test_error_routing.cjs \
            tests/test_score_disposition_badge.cjs                                   # 38/38
```

## B11.9 回滚方式

先说清文件状态（`git status` 实测，别按惯例假设）：

| 文件 | git 状态 | 回滚含义 |
|---|---|---|
| `backend/services/evidence_anchor.py`、`tests/test_evidence_anchor.py`、`tests/test_step7_scoring.py`、`tests/_mutate_step7.py`、`tests/_probe_step7.py`、`tests/_probe_unresolved.py`、`tests/_smoke_anchor.py`、`tests/test_score_disposition_badge.cjs` | `??` 未跟踪 | 直接删即可，`git checkout` 救不了也管不着 |
| `backend/agents/tools/structured_output.py` | `??` 未跟踪 | **同上，必须手工改回**（本批踩过坑：被变异测试留在变异状态时无 git 兜底） |
| `backend/agents/performance_agent.py` | `M` 已跟踪 | **不要 `git checkout`**：它同时含 Batch 9/10/11 的改动，一 checkout 会把前两批一起回滚 |
| `backend/agents/states/performance_state.py`、`backend/routes/agent_routes.py`、`frontend/client/index.html` | `M` 已跟踪 | 同上，含前几批改动，只能手工删本批新增的那几行 |

手工回退步骤：

1. 删除上面 7 个 `??` 文件（`evidence_anchor.py` + 6 个测试/工具）；
2. `backend/agents/performance_agent.py`：
   - 删模块级 `DISPOSITION_LABELS` / `summarize_disposition` / `refresh_disposition`；
   - 删 `_step7_material` 与 `_score_disposition_summary`；
   - `_node_step7` 换回原判定：`if isinstance(evidence, str) and evidence in evidence_ids and …`；
   - 恢复 `evidence_ids` 的旧写法、去掉 `hits` / `headings` / `ordinals` / `chunks` 四行；
   - 备份白名单去掉 `"score_disposition"`；
3. `backend/agents/tools/structured_output.py`：`_validate_score_items` 恢复逐条
   `actual_score 不是数字，已忽略`（去掉 `classify_score` 判断），删顶部 import；
4. `backend/agents/states/performance_state.py`：删 `score_disposition` 字段；
5. `backend/routes/agent_routes.py`：status / score-summary 去掉两个新增字段，
   `save-indicator-scores` 去掉台账刷新；
6. `frontend/client/index.html`：删 `.wb-dispo*` CSS、`buildScoreDispositionBadge`、
   `wfDispo` 变量与两处引用。

只想关掉效果、不想动代码：把 `_step7_material` 改成返回 `""`（锚点只剩检索池编号，
`heading`/`section_ordinal` 全部失效），或让 `_node_step7` 把 `chunks` 传空列表
（`evidence_locator` 失效）——但这两条都会顺带削弱校验，不建议长期保留。

## B11.10 剩余风险与未验证项（诚实列出）

1. **跑间差异是主要不确定性，未解决**（B11.7）。步骤 7 输出长度在 155~9,483 字之间摆动，
   落地数 1~7。本批没有改提示词（刻意保持单一变量），所以**没有**解决「模型不给分」。
   下一批若做，应针对「要求逐个叶子出现」的输出协议，并单独取基线。
2. **`material_id` 这一类锚点会放宽相关性判断。** 它只验证「这个编号确实出现在下发的材料里」，
   不验证「这个编号与当前指标相关」。旧的精确匹配同样不验证相关性（只看是否在 3 条池子里），
   所以不算放宽，但也不比旧规则更懂语义。风险记在这里。
3. **`reason` 回退同样是格式让步。** 模型把出处写进 `reason`、`evidence_id` 留 `null` 时，
   程序从 `reason` 里找锚点（记录 `anchor_from: "reason"`）。引用是真的，但字段形式不合约定。
   如果以后要严格化，应改提示词而不是继续加回退。
4. **章节名包含匹配可能过宽。** `资料「资金安排」` 能对上 `### （四）资金安排与使用`，
   也可能对上任何含「资金安排」的小节。已设 ≥3 字下限并只认 Markdown 标题与中文序号标题
   （不认 `1.` 列表项），但没有做唯一性要求。
5. **`unmatched` 只按「id 是否在体系内」判定。** 模型写了一个真实存在但属于**别的层级**
   （如二级 id）的情况会被当成 unknown——本轮未遇到，逻辑上也够用，但没有专门用例。
6. **前端徽标只在有 `score_disposition` 的会话上显示。** 本批之前跑的老会话返回空对象，
   步骤 7 面板不会出现该徽标（不会报错）。这是有意的：不假装知道当时为什么没落地。
7. **徽标只有 10 条 node 用例，没有真实浏览器验证。** 用例直接 eval `index.html` 里的函数，
   覆盖了拼串/转义/标题一致性，但**没有**验证 CSS 在真实主题下的可读性，也没有验证
   展开/折叠交互（用的是原生 `<details>`，风险较低）。
8. **只有一份 354 字样本**。四轮全是同一份资料，指标数 22~25、绝大多数指标本就无法核实。
   真实长篇资料上「模型主动待核实」的比例会低得多，落地率会好看，但**未验证**。
9. **`score_disposition` 全量进备份**（25 条 × 每条几十字节，约 3~4 KB），未做裁剪。
   若指标数上千需要改成分页或只存计数。
10. **定位串只在「行号能对上检索块」时才算落地。** `fe_wf_sample.txt-第1段` 这类指向
    「没被召回的那一段」的引用仍然被拒（B11.6 里 4 条）。**第 7 轮 e2e 又撞到一次，
    而且是两条真实评分**（`fe_wf_sample.txt 第14行`，2.0 分与 5.0 分，reason 里有具体金额
    与进度百分比）。这不是解析器能力不足：那一段的原文不在程序手里，无法核对引用是否
    真支持该评分。要支持它必须新增「弱定位」状态（只证明行号存在、不计入总分、
    单列供人工确认），并让 `all_raw_texts` 的行数进入备份，否则重放无法验证。
    **本批刻意不做**，理由见 B11.7 末段——直接算 `accepted` 会让「无法评价、暂按基础分」
    那几条借同一个口子混进总分。
11. **`section_ordinals` 只认中文序号。** `一、项目概况` / `（四）资金安排` 这类能解析，
    纯阿拉伯编号的材料（`1. 项目概况`）解析不出来——这是刻意的：`1.` 在正文里太常见，
    认了就会把列表项当小节，把「第1部分」解析到第一个列表项上，比拒绝更糟。
    真实材料目前都是中文序号，但这是**数据依赖**，换成另一种编号风格会失效。
12. **文件名比对做了归一化**（去掉 `.` `_` 等符号后小写比较），`a_b.txt` 与 `ab.txt`
    会被视为同一文件。单文件场景无影响，多文件同名时可能误配。未遇到，但记在这。
13. **`chunks` 与 `evidence_ids` 已同源**（都只取 `kind == "project"` 的命中），
    但 `material_id` / `heading` 仍然对**整份下发材料**生效——因为模型确实看得到
    `evaluation_table` 里的编号和分类原文的小节名，这两类锚点的语义就是「模型看到过」，
    不是「检索命中」。口径差异是有意的。
14. **`material_headings` 会把步骤 6 自己生成的小标题也当成锚点。** 实测抽出来的标题里
    混着 `分值校验`、`资料缺口提示`、`结构化JSON数据用于导出WordPDF` —— 这些不是项目资料的
    小节，是 `evaluation_table`（上一步模型自己写的）里的 Markdown 标题。模型若引用
    `资料「分值校验」`，会被判为合法锚点，本质是「自己引自己」。
    本轮没有实际触发（真实引用都指向 `一、项目概况` 这类原文小节），所以没改；
    要收窄应让 `_step7_material` 分别返回「原文部分」与「表格部分」，
    标题只从原文部分抽。这是**已知未修**，不是「已验证没问题」。

---

# Batch 12：弱定位独立成项（引用到得了原文，但那一行没进检索证据）

> 一句话：上一批把「引用写法不同」救回来了，但还有一类引用能落到**原文真实存在的行号**，
> 而那一行不在检索块里。这一批给它一个独立身份——**能看见、可核对，但不计入总分**。

## B12.1 起点：两条真实评分被拒，而引用完全正确

第 7 轮真实 14 步（备份 `b425eb90`）里，步骤 7 有两条被判「引用无法定位」：

```
evidence_id = 'fe_wf_sample.txt 第14行'    score=2.0
evidence_id = 'fe_wf_sample.txt 第14行'    score=5.0
```

查原文 `fe_wf_sample.txt`（16 行），第 14 行是：

```
财政拨款4000万元，专项债1000万元。截至2025年6月已支出3200万元，支出进度64%。
```

而模型写的理由是「截至2025年6月已支出3200.00万元，支出进度64.00%，距90%目标差26.00个百分点」
「总投资5000.00万元、已支出3200.00万元，未突破总投资」——**内容完全对得上**。

问题不在引用，在判据：本步骤只检索到第 10/12/15 行，第 14 行不在 `chunks` 里。
模型看到的是**分类原文全文**（提示词里确实下发了），所以它引用的行号系统本来就有，
只是 `evidence_locator` 要求「必须对上某个检索块」，于是把真实引用判死了。

## B12.2 为什么不能直接算「已落地」

这是本批最关键的一个判断。如果只要「行号在原文范围内」就算落地，会发生：

- `b20e9f85` 那轮有 4 条 `fe_wf_sample.txt-第1段`，其中 **3 条的 reason 自己写着
  「无法判断 / 无法评价，暂按 1.00 分基础分处理」**；
- 放行它们，总分从 30.0 抬到 33.0，accepted 从 15 变 18——数字更好看，但那是假的：
  「模型对缺失资料的妥协」被记成了「已评估」。

所以这一批给的是**独立身份**，不是放宽闸门：

| 维度 | `accepted`（已落地） | `anchor_weak`（弱定位） |
|---|---|---|
| 进 `indicator_score_map` | 是 | **否** |
| 计入 `total_score` / `accepted_score` | 是 | **否** |
| 出现在台账 | 是 | 是，单独一栏 |
| 前端徽标 | 「已落地」 | 单独 chip + 说明「不计入总分，请对照原文第 N 行确认后人工补评分」 |

一句话：**能看见、可核对、不假装。**

## B12.3 做法

1. **第 6 类锚点 `material_locator`**（`evidence_anchor._match_material_locator`）
   判据：文件名 + 行号，且**行号落在该文件非空行数范围内**。返回形如
   `fe_wf_sample.txt:第14行`，写进 `matched` 供审计。
   拿不到原文也拿不到行数时返回 `None`——宁可拒绝，也不能只看「有没有写第 N 行」就放行。

2. **行数的两个来源，同一套判定**
   - 线上：`all_raw_texts`（原文全文，`_node_init` 已在内存里）
   - 重放：`raw_line_counts`（新字段，`{文件名: 非空行数}`，几十字节，进备份白名单）
   只存行数不存全文，避免备份被几十万字撑大。两处都走
   `evidence_anchor.doc_line_counts`，不存在两套口径。

3. **文件名去前缀**（`doc_label`）
   `all_raw_texts` 的 key 带上传前缀 `u1_<32hex>_fe_wf_sample.txt`，模型写的是
   `fe_wf_sample.txt`。与 `workflow_evidence._project_chunks` 用同一条正则去前缀，
   否则「文件名永远对不上」。

4. **锚点按强度排序，不按出现顺序**（本批自查发现的降级）
   `fe_wf_sample.txt-第1段及第四部分` 是模型的真实复合写法。加了弱定位后，
   「第1段」先命中 `material_locator`（弱），把本来能落到 `section_ordinal`（强）的
   6.0 分那条降级了。改成**分轮扫描**：
   `evidence_locator` → `section_ordinal` → `material_locator`，取最强的那一类。

5. **标题只从原文抽**（风险 14 收口）
   新增 `_step7_source_text(state)`（只返回 `_categorized_excerpt`），
   `material_headings` / `section_ordinals` 改为对**它**生效，不再对整份
   `_step7_material`（含 `evaluation_table`）生效。
   原因：实测从整份材料抽出来的标题里混着 `分值校验`、`资料缺口提示`、
   `结构化JSON数据供导出WordPDF` —— 那是**步骤 6 自己生成**的 Markdown 标题。
   模型引用它们等于「引用自己上一步的产物」，不是项目资料证据。
   `material_id`（编号）仍然对整份材料校验：那些编号模型确实看得到。

6. **前端**
   `anchor_weak` 不打 `bad` 样式（它不是故障）、不进 `systemSide`（不算系统侧原因），
   单独一条 chip + 一段说明。顺带修掉一个文案缺陷：原来 15 项未提及 + 2 项弱定位
   会被说成「其余 15 项模型未提及」，读者会以为另外 2 项已落地——
   现在只有「其余分项均为 0」时才敢用「其余 N 项都是 X」的句式。

## B12.4 真实数据前后对比（同一备份重放，零 token）

| 备份 | 叶子 | 改前 | B11（五类锚点） | B12（弱定位独立） |
|---|---|---|---|---|
| `dc0c6294` | 23 | 2（4.0 分） | 7（13.0 分） | **7（13.0 分）**，无弱定位 |
| `4a120084` | 25 | 5（14.0 分，实际落库） | 6（16.0 分） | **6（16.0 分）**，无弱定位 |
| `b20e9f85` | 23 | 3（6.0 分） | 15（30.0 分） | **15（30.0 分）+ 3 条弱定位** |
| `b425eb90` | 20 | 1（4.0 分） | 1（4.0 分） | **3（11.0 分）+ 2 条弱定位** |

三条要点：

1. **`b425eb90` 是决定性的一次验证**：重放算出 **11.0 分 / 3 项**，
   与备份里 `score_summary` 当时真实落库的 `11.0 / 3 项` **完全一致**。
   说明新判据与线上实际行为对齐，不是「本地算得好看」。
   其中 2 条弱定位（2.0 与 5.0）**没有**被算进这 11.0 分。

2. **`b20e9f85` 总分不变（30.0），但多出 3 条弱定位**：那 3 条 `第1段` 以前是
   「引用无法定位」（信息更少），现在能说出「落到原文第 1 行、但该行未进入检索证据」，
   且**不计分**。这是净收益：可解释性变强，总分零虚高。

3. **两个工具口径必须统一**（踩过）：`_probe_unresolved.py` 原来自己用
   `_extract_payload` 取**第一段** JSON，而 `_node_step7` 走 `structured_output`
   取**修复后**那段，同一份备份一个说「0 条未落地」、一个说「4 条」。
   已改为直接跑一遍 `_node_step7` 读它写下的台账。

## B12.5 改动清单

**新增**

| 文件 | 作用 |
|---|---|
| （无新文件） | 本批全部落在既有文件里 |

**修改**

| 文件 | 改动 |
|---|---|
| `backend/services/evidence_anchor.py` | 新增 `doc_label` / `doc_line_counts` / `_match_material_locator`；`resolve` 加 `raw_texts` / `line_counts` 参数；段内锚点改为**按强度分轮扫描**（修降级） |
| `backend/agents/performance_agent.py` | 新增 `_step7_source_text`；`headings` / `ordinals` 改从原文抽；`_node_init` 算 `raw_line_counts`；`_node_step7` 传 `raw_texts` / `line_counts` 并新增 `anchor_weak` 分支（**不写 score_map**）；`DISPOSITION_LABELS` 加一类；备份白名单加 `raw_line_counts` |
| `backend/agents/states/performance_state.py` | 新增字段 `raw_line_counts` |
| `frontend/client/index.html` | 徽标识别 `anchor_weak`（warn 非 err、单独 chip、说明「不计入总分」）；修「其余 N 项」文案在混合分项时的误述 |
| `tests/test_evidence_anchor.py` | +12 用例（弱定位 9 + 优先级 3） |
| `tests/test_step7_scoring.py` | +10 用例（弱定位 7 + 标题来源 3） |
| `tests/test_score_disposition_badge.cjs` | +4 用例（弱定位徽标） |
| `tests/_mutate_step7.py` | 变异点 15 → 19 |
| `tests/_probe_step7.py` | `fill_line_counts`：旧备份没有 `raw_line_counts` 时从 `uploads/` 补算 |
| `tests/_probe_unresolved.py` | 重写：改为复用 `_node_step7` 的台账，消除两套口径 |
| `tests/_smoke_anchor.py` | 用例 18 → 21（补 3 条弱定位真实形态：空格分隔行号 / 段号 / 越界拒绝） |
| `wf_e2e.py` | +1 断言：已落地分值合计只累加 accepted（独立重算，抓「弱定位混进总分」） |
| `frontend_e2e_check.py` | +2 断言：HTML 含弱定位文案与「不计入总分」 |

## B12.6 可重跑命令

```bash
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval
.venv/Scripts/python tests/_smoke_anchor.py                       # 21/21

# 四轮真实备份重放（零 token）
.venv/Scripts/python tests/_probe_step7.py b425eb90-89a9-418a-a6b1-5a8c56870b30   # 3/20 = 备份真实值
.venv/Scripts/python tests/_probe_step7.py b20e9f85-b888-4f29-b271-f7139690202a   # 15/23 + 3 弱定位
.venv/Scripts/python tests/_probe_unresolved.py b20e9f85-b888-4f29-b271-f7139690202a # 看弱定位/未落地明细

.venv/Scripts/python tests/_mutate_step7.py        # 19 个变异点，结束时校验字节一致
```

## B12.7 回滚方式

本批全部改在**已跟踪**文件上（`evidence_anchor.py` 仍是 `??` 未跟踪）：

1. `backend/services/evidence_anchor.py`（未跟踪）：删 `doc_label` / `doc_line_counts` /
   `_match_material_locator`；`resolve` 去掉 `raw_texts` / `line_counts` 两个参数；
   段内循环改回「逐段先到先得」。
2. `backend/agents/performance_agent.py`：删 `_step7_source_text`，
   `headings`/`ordinals` 改回 `material_headings(material)` / `section_ordinals(material)`；
   删 `_node_init` 里的 `raw_line_counts` 计算；`_node_step7` 删 `anchor_weak` 分支
   与 `raw_texts`/`line_counts` 传参；`DISPOSITION_LABELS` 去掉 `anchor_weak`；
   白名单去掉 `"raw_line_counts"`。
3. `backend/agents/states/performance_state.py`：删 `raw_line_counts` 字段。
4. `frontend/client/index.html`：`order` 去掉 `anchor_weak` 行、去掉 `weakN` 相关分支与 note。

只关效果不动结构：把 `_node_step7` 里 `line_counts = {}`、`raw_texts = {}` 写死即可
（弱定位全部退回 `anchor_unresolved`，行为等价 B11）。

## B12.8 剩余风险与未验证项（诚实列出）

1. **弱定位只验证「行号存在」，不验证「内容支持该评分」。** 这是设计上的已知让步：
   真要验证内容相关性需要语义比对（模型或向量），不在本批范围。所以它**不计分**，
   只作为人工确认的线索。
2. **行数口径依赖「非空行」。** 原文若有大段空行或表格被 `splitlines` 拆得与模型
   理解不同，行号可能对不上。实测样本（16 行纯文本）对得上，**长篇 Word/PDF 未验证**。
   PDF 抽取出的文本行号与原文页码/段落不是一回事，这类材料的弱定位可信度更低。
3. **`all_raw_texts` 与 `raw_line_counts` 的键都必须去前缀。** 已用同一条正则，
   但若将来上传命名规则变化（不是 `u\d+_<32hex>_`），两处会同时失效——不会静默错配，
   会退化成「全部拒绝」，属于安全失败。
4. **同名文件会冲突。** `doc_label` 只去掉前缀和目录，两个同名文件在
   `raw_line_counts` 里会互相覆盖（后写覆盖）。单文件场景无影响，多文件同名的场景未处理。
5. **弱定位的 3 条基础分仍是「假分」——只是现在不装了。** `b20e9f85` 里那 3 条
   reason 写着「无法评价、暂按 1.00 分基础分」，工具现在把它们显示成弱定位、
   不计入总分。但**模型为什么给基础分**这件事没解决，属于步骤 7 输出协议问题。
6. **`_probe_step7.py` 的「改前」仍是模拟值。** `old_rule` 复现的是旧判定，
   不是历史真值（`4a120084` 那轮实际落库 5 条，模拟 2 条）。B11.6 已加注，
   本批未改这个工具语义。
7. **`anchor_weak` 未在备份白名单之外的地方持久化到 DB。** 台账随
   `score_disposition` 进状态与备份，但没进数据库表——重启后从备份恢复，
   老会话（B12 之前）没有该字段，徽标不会显示弱定位分项（不会报错）。
8. **仍未改提示词。** 本批继续坚持单一变量（只改判定，不改下发内容）。
   「模型只给 1~6 条评分」这个真正的瓶颈原样留着。

---

# Batch 13：修复不得缩水（一次「修好了」，把 20 条改成 5 条）

> 一句话：上一批的收尾里写着「模型只给几条评分这个瓶颈原样留着」。这一批去查它，
> 结果发现**有一半是程序自己吃掉的**——结构化修复链路里有三处会把条目删掉，
> 其中一处让模型**物理上看不见**后半段原文。

## B13.1 起点：正文长度是双峰的，两组没有一条重叠

把仓库里 20 份历史备份的「步骤 7 正文长度」按 `structured_status` 分组（只读 JSON，不跑代码）：

| 组 | n | 最短 | 中位 | 最长 |
|---|---|---|---|---|
| `ok`（一次过，没修复） | 8 | **5,751** | 7,902 | 9,483 |
| `repaired`（修过一次） | 6 | 155 | **1,076** | **3,218** |

两组**没有任何重叠**，最近的两点相差 2,533 字。`repaired` 组的中位数只有 `ok` 组的 **13.6%**。

这不是模型发挥不稳定。模型发挥不稳定会是一条连续的钟形曲线；这里是**断崖**：
只要走了修复这条路，正文就掉到三分之一以下。断崖的形状指向程序，不指向模型。

## B13.2 三条并发的吃条目路径

逐行读修复链路（`_execute_step_structured` → `_repair_structured_output` → `structured_output.repair_messages`），
找到三处，**每一处单独都足以吃掉条目**：

**① 修复提示词在教模型删条目。** `repair_messages` 的 system 原文写着：

```
若原输出因过长被截断，请只保留能确定的部分，宁可少写条目也不要编造。
```

「宁可少写」四个字是程序写给模型的指令。模型照办了，然后我们怪模型只给几条。

**② 回喂原文被截断到 6,000 字。** `MAX_ECHO_CHARS = 6000`，对所有步骤一视同仁。
可步骤 7 的正常输出是 7,352~9,483 字。**模型根本看不到被截掉的那半段**，
它「删掉」的条目其实从未进入它的上下文。

用离线探针把这件事量化（`tests/_probe_repair_echo.py`，纯字符串，零 token）：

```
原文  11780 字，回显   6000 字（51%）
原文提及 20 个指标，回显后模型只能看见 11 个
被截断看不见的：9 个 → L1_12_01_01, L1_13_01_01, …
```

原文越长，看不见的越多——**越需要修复的输出，越修不好**。这是个正反馈。

**③ 修复成功后，首次正文被整体丢弃。** `return repaired, second` 之后 `content` 就是修复版，
原始输出不落盘、不写库。查证：20 份历史备份里 `raw_content` **全部为空**
（这个字段本身就是本批才加的），所以这件事拖到第 13 批才被发现——
**不是没人怀疑，是没有证据可查**。

三处叠加的结果，就是 `b425eb90` 那轮：日志显示首次生成 4,458 tokens（≈20 个叶子），
修复只回了 463 tokens，正文 6,000+ 字 → 1,059 字。

## B13.3 做法（四条，都只改「修复」这一段）

1. **回显上限按步骤分档**：`MAX_ECHO_CHARS_BY_STEP = {7: 24000}` + `mention_limit(step)`。
   其余步骤保持 6,000（它们的正常输出本来就短，放大只会灌 token）。
2. **改掉教删条目的提示词**：
   ```
   必须逐条保留原输出里的每一个条目，不得删减、合并或省略；
   拿不准的条目照原样抄录（分值、引用、理由都不要改），不要因为不确定就删掉。
   ```
3. **加缩水闸门** `mentioned_indicator_ids` + `repair_shrunk`：修复「通过校验」不算完，
   还要比一比覆盖的指标数。只对步骤 7 生效（结构是 `{"scores": {id: {...}}}`，条目数可无损对比）。
   判定缩水时**不采用修复版本**，保留原文、记 warning、交人工复核。
4. **首次正文留档**：`StructuredOutcome.original` 带上被替换掉的原文，
   `_record_step_result` 写进 `step_results[7].raw_content`。备份白名单里 `step_results` 是整体落盘的，
   不需要改白名单，但**这一条是以后能复盘的前提**。

关键取舍：**缩水时宁可交人工复核，也不接受一个更好看但更少的结果。**
这与 B12「弱定位不计分」是同一条原则——不为了让数字好看而放行。

## B13.4 验证：正反两面都要

只证明「闸门会拦」是不够的——一个把所有修复都拒掉的闸门看起来最安全，其实等于关掉了修复功能。
所以本批做了三个方向的验证。

**方向一：截断确实存在，且已消失**（`tests/_probe_repair_echo.py`）

| | 回显字数 | 模型能看见的指标 |
|---|---|---|
| 改前（全步骤 6,000） | 6,000 / 11,780（51%） | **11 / 20** |
| 改后（步骤 7 24,000） | 11,780 / 11,780（100%） | **20 / 20** |

**方向二：缩水的修复会被拒**

```
缩水判定：原文 20 个指标，修复结果 5 个 → repair_shrunk=True（True 表示拒绝采用修复版本）
```

**方向三：真实的修复不被误伤**（真实 14 步，会话 `9141a214`，47 项断言全过）

这一轮**真的触发了一次修复**，而且原因很典型：首次输出把 `scores` 写成了**数组**而不是对象，
校验报「scores 的类型不是对象」。修完之后：

| | `scores` 类型 | 条目 | 数值分 | 正文长度 |
|---|---|---|---|---|
| 首次输出（`raw_content`） | list | 20 | 5 | **7,566 字** |
| 修复后（`content`） | dict | 20 | 5 | 2,201 字 |

条目 20→20、数值分 5→5，`repair_shrunk=False`，正常采用。台账口径一致：

```
5 accepted + 15 model_pending = 20，总分 16.0
[PASS] 已落地分值合计只累加 accepted（弱定位不计分） 独立计算=16.0 台账=16.0 弱定位=0
```

也就是说：**闸门没有把「格式修好了、数据没少」的正常修复一起拒掉**。
同时这是历史上第一次能在磁盘上看到首次正文（7,566 字）——
以前它会被 2,201 字直接覆盖，事后无从查证。

## B13.5 改动清单

| 文件 | 跟踪状态 | 改了什么 |
|---|---|---|
| `backend/agents/tools/structured_output.py` | `??` 未跟踪 | `MAX_ECHO_CHARS_BY_STEP` / `mention_limit` / `mentioned_indicator_ids` / `repair_shrunk`；`repair_messages` 的 system 与回显上限；`StructuredOutcome.original` |
| `backend/agents/performance_agent.py` | `M` | `_execute_step_structured` 加缩水判定分支（拒绝并保留原文 + warning）；修复成功时 `second.original = content`；`_record_step_result` 写 `raw_content` |
| `tests/test_structured_output.py` | `M` | 新增 7 条（提示词不许教删、`mentioned_indicator_ids`、缩水拒绝、完整修复接受、原文留档、回显上限 2 条） |
| `tests/_probe_repair_echo.py` | 新增 | 量化回显截断的零 token 探针 |
| `tests/_mutate_step7.py` | `M` | 变异点 19 → 23；顺手修好 3 个因 B12 重构而失效的锚点 |

没有改：接口路径、SSE 事件名与语义、DB 字段、任何提示词模板（本批仍只动「修复」这一段，
不动「下发」那一段，保持单一变量）。

## B13.6 可重跑命令

```bash
# 全量回归（无需服务器）
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval      # 261 passed
node tests/test_score_disposition_badge.cjs                            # 14 pass
node tests/test_workflow_sse.cjs                                       # 8 pass

# 量化回显截断：改前只看得见 11/20 个指标
.venv/Scripts/python tests/_probe_repair_echo.py

# 变异测试：23 个变异点，结束时校验三个文件逐字节还原
.venv/Scripts/python tests/_mutate_step7.py

# 真实 14 步（会调模型，约 4 分钟；需先起服务）
.venv/Scripts/python -m backend.main
.venv/Scripts/python wf_e2e.py                                         # 47 项检查，失败 0
```

## B13.7 回滚方式

1. `structured_output.py`（未跟踪）：删 `MAX_ECHO_CHARS_BY_STEP` / `mention_limit` /
   `mentioned_indicator_ids` / `repair_shrunk`，`repair_messages` 回显改回 `[:MAX_ECHO_CHARS]`，
   system 换回原文，`StructuredOutcome` 去掉 `original`。
2. `performance_agent.py`：删 `_execute_step_structured` 里 `repair_shrunk` 那个 `if` 块、
   删 `second.original = content`、删 `_record_step_result` 里写 `raw_content` 的三行。
3. `tests/` 三个文件按上表删除新增内容即可（`_probe_repair_echo.py` 直接删）。

只关效果不动结构：把 `MAX_ECHO_CHARS_BY_STEP` 清空、`repair_shrunk` 直接 `return False`，
行为就等价于 B12。

## B13.8 剩余风险与未验证项（诚实列出）

1. **缩水判定只比「键数」，不比「分值」。** 修复可以把 5 个数值分改写成「待核实」而保留 20 个键，
   依然通过。本轮实测 5→5 没有退化，但**机制上没拦这个方向**。下一批应该加
   「数值分个数不得减少」的第二道比较。
2. **只对步骤 7 生效。** 步骤 3/4 的结构是 list，`scores` 键数对比不成立，本批没做等价判据。
3. **24,000 是拍的，没扫过拐点。** 历史最长步骤 7 输出 13,179 字，暂无样本触顶；
   真到 24,000 字以上仍会截断，只是概率低了很多。
4. **`mentioned_indicator_ids` 依赖 id 命名规则。** 正则写死 `L\d_\d{2}_\d{2}_\d{2}`。
   若将来指标体系改成不补零的 id，正则**静默返回空集** → `repair_shrunk` 恒为 False →
   闸门失效且不报错。这是「安全失败方向反了」，值得优先补一条断言。
5. **修复只输出 JSON，JSON 之外的正文会丢。** 本轮 7,566 → 2,201 字，删掉的主要是解释性段落。
   步骤 7 的主数据是 JSON，影响有限；但报告正文里的人工解读会变短。现在至少 `raw_content` 留了档，
   能查、能补。
6. **「缩水」的真值仍是人工判断。** 本批只保证「不比原文少」，不保证「留下的每条都对」。
7. **仍未动步骤 7 的下发提示词。** 本轮 20 条里 15 条是模型自己判「待核实」——
   **这是模型的选择，不是程序吃的**，本批没有改善它。B12.8 第 8 条依然成立：
   真正的瓶颈（为什么一半指标拿不到资料）要等提示词那一批。

---

# Batch 14：闸门不能自带盲区（缩水判定的基准与第二判据）

> 一句话：上一批的收尾里，剩余风险第 1 条是「只比键数不比数值分」、第 4 条是
> 「编号写法一变，正则静默返回空集 → 闸门失效且不报错」。这一批把这两处补上，
> 并且发现旧判据**在真实数据上已经误伤过一次**。

## B14.1 旧判据的两个「失效时不报错」

Batch 13 的判定只有两行：

```python
before = mentioned_indicator_ids(content)          # 正则数正文里提到的编号
after  = set((second.payload or {}).get("scores"))  # 只认 dict
```

两处失效方向，都不会报错：

1. **`before` 为空就直接放行。** 正则写死 `L\d_\d{2}_\d{2}_\d{2}`。指标体系换一套编号写法，
   `before` 恒为空集 → `return False` → 闸门等于不存在，日志里一个字都没有。
2. **`after` 只认 dict。** 模型把 `scores` 写成数组（实测出现过），`after = set()` → 恒为 0。
   这不是「少拦」，是**反向误伤**：条目一条没少的修复会被判成缩水。
3. 附带一处：只比条目数，不比分值。条目数不变、5 个真实分值全变成「待核实」，旧判据放行。
   历史备份里 `effa217d` 修复后就是 11 条 / **0 个数值分**的形态，只是那轮没留下首次正文，
   无法复盘到底丢没丢。

## B14.2 取证：误伤不是假设，已经发生过

拿 22 份历史备份跑「旧规则 vs 新规则」逐条对比（`tests/_probe_shrink_guard.py`）：

| | 结果 |
|---|---|
| 判定发生变化 | **1 份**（`11b16a29`，旧 True → 新 False） |

查这一份：步骤 7 的 `scores` 是 **15 条数组**，正文里也恰好提到 15 个编号。

```
旧规则 after（只认 dict）= 0  →  0 < 15  →  判「缩水」，拒绝修复
新规则 after（认数组）   = 15  →  15 == 15 →  放行
```

也就是说：**如果这轮触发了修复，Batch 13 的闸门会把一份完好的修复判成缩水**，
用户看到的是「待人工复核」，而真正的原因是判据不认数组。

同时确认一个「不是问题」的事实：22 份备份里正常轮次的「提及数」恒等于「条目数」。
所以旧基准平时是对的——**它是靠模型老实才对的**，一旦编号风格或输出形态变化就崩。

## B14.3 做法（换基准 + 加第二判据 + 判不了要出声）

1. **基准换成原文 JSON 里的真实条目**（`coverage_of_text`）：
   解析出 `scores` 就用它数（dict / list / 扁平映射三种形态都认）；
   解析不出来才退回「正文提到的编号」，并标 `source="mention"`。
   退回时 **`numeric=0`**，调用方据此跳过「分值不得减少」这条判据——
   不能拿 0 去比，那是拿没有基准当基准。
2. **加第二判据**：`before.numeric > 0` 时，`after.numeric` 不得减少。
   条目数一样但分值被清零，同样判缩水。
3. **判不了要出声**：`before.entries == 0` 时置 `unverifiable=True`，
   agent 照常采用修复结果，但写 warning + `logger.warning`。
   静默放行是 B13 的遗留写法，本批消灭它。
4. `repair_shrunk` 保留为薄封装（内部调 `shrink_report`），旧调用方与旧用例不受影响。

同一把尺子的原则：**基准和结论都从同一个 JSON 结构里数**，不再一边数正则、一边数字典键。

## B14.4 验证（四个方向）

**① 口径漂移：只改了 1 份，且是修正误伤**

22 份备份逐条对比，只有 `11b16a29` 变化，方向是「原本要拒 → 现在放行」，即 B14.2 那个数组误伤。

**② 两个盲区都能被新判据抓住**（构造用例，旧 False → 新 True）

| 场景 | 旧判定 | 新判定 |
|---|---|---|
| A. 编号风格改成 `ZB-xx`，12 条被修成 3 条 | False（基准空集） | **True**（基准 3 条→ 条目 12→3） |
| B. 条目数不变，5 个分值全变「待核实」 | False | **True**（可计分数值分 5→0） |

**③ 真实修复不被误伤**（两次真实 14 步，都有 `raw_content`）

| 会话 | 首次正文 | 首次条目/分值 | 修复后正文 | 修复后条目/分值 | 判定 |
|---|---|---|---|---|---|
| `9141a214`（B13 那轮） | 7,566 字 | 20 / 5 | 2,201 字 | 20 / 5 | 放行 |
| `7049d7c9`（本轮） | **12,608 字** | 26 / 6 | 2,745 字 | 26 / 6 | 放行 |

注意 `7049d7c9` 这轮：正文从 12,608 字掉到 2,745 字（**掉 78%**），条目和分值却一条没少。
这正好说明 B13 用「正文长度」做的观察只是**诊断线索**，不能当判据——
按长度判，这份完好修复会被误杀。

**④ 回归与变异**

- pytest **268 passed**（结构化 35→42）
- node 徽标 14 / SSE 8
- 变异 **28/28 全抓**（新增 5 个：基准退回正则 / 不比数值分 / 判不了静默 / 不认数组 / 不认扁平映射），
  三个文件逐字节还原
- `wf_e2e.py` **47 项检查 0 失败**（跑了两次，第一次见 B14.5）

## B14.5 顺手发现的一个既有竞态（不在本批范围，未修）

第一次跑 e2e 时有 2 项失败：

```
[FAIL] POST 问卷生成 n=0
[ERROR] 未处理的异常 [POST /api/agent/workflow/questionnaire]:
        [WinError 5] 拒绝访问。: 'state_backups\tmpqfie3sht' -> 'state_backups\<sid>.json'
```

根因不是本批改动，是**两条路径抢同一个文件**：

- 写：`PerformanceAgent.update_state_backup` 先写临时文件，再 `os.replace` 原子替换（持锁）；
- 读：`agent_routes.py:1933` 的问卷列表恢复循环 `open(...).read()`，**不持锁**。

Windows 上只要有读句柄开着，`os.replace` 就报 `WinError 5`。重跑一次 47/47 全过，确认是偶发。
概率随备份文件数增长（现已 24 份），所以会越来越常见。**留作 Batch 15 首项**，本批不掺进来。

## B14.6 改动清单

| 文件 | 跟踪状态 | 改了什么 |
|---|---|---|
| `backend/agents/tools/structured_output.py` | `??` 未跟踪 | 新增 `Coverage` / `coverage_of_payload` / `coverage_of_text` / `ShrinkReport` / `shrink_report`；`_score_items` 认 dict/list/扁平三形态；`repair_shrunk` 变薄封装 |
| `backend/agents/performance_agent.py` | `M` | `_execute_step_structured` 改用 `shrink_report`；缩水时把「条目/分值」具体数字写进 note；`unverifiable` 时采用但写 warning + 日志 |
| `tests/test_structured_output.py` | `M` | 新增 7 条（编号风格、分值清零、正常修复放行、判不了要出声、口径以 JSON 为准、三形态、旧接口兼容） |
| `tests/_probe_shrink_guard.py` | 新增 | 22 份备份的新旧口径对比 + 两个盲区构造用例 + 真实首次/修复对照 |
| `tests/_mutate_step7.py` | `M` | 变异点 23 → 28；修好 1 个因本批改动失效的锚点 |

没有改：接口路径、SSE 事件名与语义、DB 字段、提示词模板。

## B14.7 可重跑命令

```bash
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval      # 268 passed
node tests/test_score_disposition_badge.cjs                            # 14 pass
node tests/test_workflow_sse.cjs                                       # 8 pass

# 新旧口径对比（22 份备份，零 token）：判定只变 1 份，且是修正误伤
.venv/Scripts/python tests/_probe_shrink_guard.py

# 变异测试：28 个变异点，结束时校验三个文件逐字节还原
.venv/Scripts/python tests/_mutate_step7.py

# 真实 14 步（会调模型，约 4~5 分钟）
.venv/Scripts/python -m backend.main
.venv/Scripts/python wf_e2e.py                                         # 47 项检查，失败 0
```

## B14.8 回滚方式

1. `structured_output.py`：`shrink_report` 与 `coverage_*` 三个函数可整体删除，
   `repair_shrunk` 换回 B13 的两行实现（正则 + dict 键数）。
2. `performance_agent.py`：`_execute_step_structured` 里把 `report = ...` 之后的两个分支
   换回 B13 的单一 `if second.ok and structured_output.repair_shrunk(...)`。
3. `tests/`：删掉新增 7 条与 `_probe_shrink_guard.py`，变异点删 5 个。

只关效果不动结构：把 `shrink_report` 的 `problems` 只用第一条（条目数），
并把 `coverage_of_text` 的 JSON 分支短路，即回到 B13 行为。

## B14.9 剩余风险与未验证项（诚实列出）

1. **`before.numeric` 是从「首次正文的 JSON」数出来的，不是权威分值。**
   首次输出的 `actual_score` 可能是字符串 `"2"`，`_coerce_number` 会认；
   但 `"2分左右"` 这种就不认，基准会偏低 → 数值分判据偏松。方向是「少拦」不是「误杀」。
2. **`source="mention"` 时数值分判据完全关闭。** 这时唯一在起作用的是条目数，
   而条目数又是正则数出来的——B14 只是把这种情况**标出来**，没有让它变可靠。
   真正的解法是要求模型必须给 JSON（提示词那一批）。
3. **扁平映射形态的 `entries` 与 `numeric` 是同一个数**（`{"L1_01": 20}` 里每一项既是条目也是分值）。
   这类输出在步骤 7 里没见过，若出现，两条判据会同时触发，结论仍正确但 reason 会冗余。
4. **数组形态里认 `indicator_id` / `id` / `key` 三个别名**，都不是就退化成 `#0..#n` 占位。
   占位只影响去重计数，不影响条目数，但会让「同一个指标出现两次」被算成两条。
5. **仍只对步骤 7 生效。** 步骤 3/4 的结构不同，本批没做等价判据（与 B13 一致）。
6. **B14.5 那个竞态没修。** 它会把问卷接口打成 500，重跑就过——这种「偶发」最容易被忽略，
   已经在 B14.5 里写成 Batch 15 首项，不留在文档里当脚注。
7. **「缩水」的真值仍是人工判断。** 本批保证的是「不比原文少」，不是「每条都对」。



---

# 批次 15：把备份文件的读写竞态关掉（B14.5 挂的账）

## B15.1 起点：这不是「偶发」，是两条路径抢同一个文件

B14.5 记下的失败原文：

```
[ERROR] 未处理的异常 [POST /api/agent/workflow/questionnaire]:
        [WinError 5] 拒绝访问。: 'state_backups\tmpqfie3sht' -> 'state_backups\<sid>.json'
```

当时的结论是「重跑就过，偶发」。这一批不接受这个结论，先把它变成能稳定复现的东西。

用全仓库检索确认了范围——读写 `state_backups` 的地方一共三类：

| 位置 | 操作 | 是否持 `_STATE_BACKUP_LOCK` |
|---|---|---|
| `performance_agent.update_state_backup` | 写临时文件 + `Path.replace` | 持 |
| `performance_agent.load_state_backup` | `open().read()` | 持 |
| `agent_routes.py:1933` 问卷列表恢复循环 | `open().read()` | **不持** |
| `client_routes.py:685` `_remove_report_snapshot` | `unlink` | **不持** |

`_STATE_BACKUP_LOCK` 是进程内 `RLock`。写路径持锁 + `os.replace`，读路径不持锁直接开文件——
Windows 的 `open()` 不带 `FILE_SHARE_DELETE`，只要读句柄还开着，`os.replace` 就必然失败。
这跟「概率」无关，只跟「有没有重叠」有关，而备份文件已有 24 份、列表接口每次全量扫一遍，
重叠窗口越来越大，所以它只会越来越常见。

## B15.2 取证：先让它在 12 秒内稳定红

新增 `tests/test_state_backup_race.py`，第一条用例就是真实复现：起一个线程打开备份文件、
持有 0.2 秒，主线程在此期间调 `update_state_backup`。

改前跑出来的是：

```
E   PermissionError: [WinError 5] 拒绝访问。:
    '...\backups\tmpf3ur7i7v' -> '...\backups\race.json'
```

**与生产日志同一形状**（临时文件 → 正式文件），且 12 秒内必现——从「跑 46 项 e2e 靠运气」变成
「一条用例、一次命中」。

## B15.3 做法（三处，都是最小改动）

1. **写入侧加退避重试。** 新增模块级 `_retry_on_transient_permission(op, budget)`：
   只对 `PermissionError` 重试，预算 `STATE_BACKUP_IO_BUDGET_SEC = 2.0` 秒，指数退避
   （20ms 起，封顶 250ms）；预算耗尽**照原样抛出**，不吞。发布与删除各包一层
   （`_publish_backup` / `_discard_backup`）。
2. **读路径收口。** `agent_routes.py` 的列表恢复循环改用 `PerformanceAgent.load_state_backup(sid)`，
   与写入共用一把锁；`if not backup: continue` 保留「文件坏了就跳过这条」的原有语义
   （改前是 `except Exception: pass`，等价）。
3. **删除路径收口。** 新增 `PerformanceAgent.delete_state_backup(session_id)`（持锁 + 重试），
   `client_routes._remove_report_snapshot` 改调它；文件本来就不存在时返回 `False`，调用方未用返回值，
   行为不变。

顺手把 `NamedTemporaryFile(delete=False)` 换成 `mkstemp(suffix=".tmp")`——不是重构癖，
是为了让「临时文件有没有残留」这件事**可以被断言**（`glob("*.tmp")`）。

## B15.4 验证（四个方向）

**① 单测 6 条全绿**（改前 5 红 1 绿）：

```
tests/test_state_backup_race.py ......                                 6 passed
```

覆盖：真实占用下写入落地、重试循环真的重试了 3 次、占用不放时响亮失败且旧内容完好、
删除走持锁入口、列表接口必须经过 `load_state_backup`（用 spy 断言，退回裸 `open()` 必红）、
坏文件与 `deleted` 备份仍被跳过。

**② 跨进程探针：162/400 → 0/400。** `_STATE_BACKUP_LOCK` 挡不住别的进程，所以专门用
两个进程对打（一个持续开读句柄，一个写 400 次），把「预算」当开关对比：

```
改前（预算 0 秒，失败即抛）: 162/400 次 PermissionError
改后（预算 2 秒，退避重试）: 0/400 次 PermissionError
```

40.5% 的写入会失败——这就是那个「偶发 500」的真身。

**③ 全量回归 268 → 274 passed**（新增 6 条，无既有用例改动）：

```
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval
274 passed, 368 warnings in 30.58s
```

**④ 变异检验：新增 4 个变异体全部被抓，既有两个脚本未受影响。**

```
抓住  发布不重试（目标被占用就直接抛）
抓住  发布后不清临时文件
抓住  删除假装成功（不真删文件）
抓住  列表接口退回裸 open（绕过锁）
```

真实服务上：`test_all_apis.py` **46/46**、`frontend_e2e_check.py` **72 通过 / 0 失败**
（服务已用本批代码重启，非旧进程）。

## B15.5 改动清单

| 文件 | 跟踪状态 | 改了什么 |
|---|---|---|
| `backend/agents/performance_agent.py` | `M` | 新增 `STATE_BACKUP_IO_*` 三个常量、`_retry_on_transient_permission` / `_publish_backup` / `_discard_backup`；`update_state_backup` 改 `mkstemp` + 重试发布；新增 `delete_state_backup` |
| `backend/routes/agent_routes.py` | `M` | 问卷列表恢复循环改走 `load_state_backup`（不再裸 `open`），补 `if not backup: continue` |
| `backend/routes/client_routes.py` | `M` | `_remove_report_snapshot` 改调 `delete_state_backup` |
| `tests/test_state_backup_race.py` | 新增 | 6 条：真实占用复现、重试计数、占用不放要报错、删除持锁、列表必须走持锁入口、坏文件/已删跳过 |
| `tests/_probe_backup_race.py` | 新增 | 双进程对打探针，预算 0 秒 vs 2 秒的失败次数对比 |
| `tests/_mutate_backup_race.py` | 新增 | 4 个变异体 |

没有改：接口路径、SSE 事件名与语义、DB 字段、提示词模板、备份文件格式（仍是单个 JSON）。

## B15.6 可重跑命令

```bash
# 单测（6 条，Windows 上是真实竞态复现）
.venv/Scripts/python -m pytest tests/test_state_backup_race.py -q

# 全量回归
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval      # 274 passed

# 跨进程探针：改前 162/400 失败，改后 0/400
.venv/Scripts/python tests/_probe_backup_race.py

# 变异测试：新增 4 个 + 既有 28 个 + 既有 7 个
.venv/Scripts/python tests/_mutate_backup_race.py
.venv/Scripts/python tests/_mutate_step7.py
.venv/Scripts/python tests/_mutate_consistency.py

# 真实服务端到端（需先启动 uvicorn）
.venv/Scripts/python test_all_apis.py                                  # 46/46
.venv/Scripts/python frontend_e2e_check.py                             # 72 通过 / 0 失败
```

## B15.7 回滚方式

1. 完全回滚：`git checkout -- backend/agents/performance_agent.py backend/routes/agent_routes.py backend/routes/client_routes.py`，
   删掉 `tests/test_state_backup_race.py`、`tests/_probe_backup_race.py`、`tests/_mutate_backup_race.py`。
2. **只关效果不动结构**：把 `STATE_BACKUP_IO_BUDGET_SEC` 设为 `0`，重试立即失效（第一次
   `PermissionError` 就抛），即回到改前行为——这一步可以单独用来复现线上那个 500。
3. 只回滚读路径：把 `agent_routes.py` 那段换回 `with open(...) as f: backup = json.load(f)`。

## B15.8 剩余风险与未验证项（诚实列出）

1. **2 秒预算只覆盖 2 秒以内的占用。** 被占更久（大文件备份软件、在线杀毒全盘扫描）仍会抛错；
   `_persist_state_backup` 会吞掉并记 warning，用户侧表现为「这一步的备份没落盘」而不是 500。
   这是刻意的取舍：宁可留 warning，也不静默丢数据。
2. **锁仍是进程内的。** 多 worker 部署（`uvicorn --workers 4`）下，跨进程只靠重试，
   不靠锁；本批**没有**验证多 worker 场景（本地是单进程）。要彻底解决得上文件锁
   （`msvcrt.locking` / `portalocker`），那是另一批的事。
3. **`load_state_backup` 吞 `OSError` 的行为没变。** 极端窗口里读不到会返回 `{}`，
   列表接口这一轮就跳过该记录（下一轮恢复）。与改前的 `except Exception: pass` 等价，
   但「读不到」和「文件真的是空的」在返回值上无法区分——这是既有设计，本批不动。
4. **只收口了 `state_backups`。** `uploads/`、`exports/`、`state_backups` 之外的磁盘写入
   没有扫同类竞态（例如导出目录的清理定时器与下载请求）。**未验证**，不敢说没有。
5. **探针用读句柄模拟占用，没有真的跑杀毒软件。** 两者在 Win32 层是同一类失败
   （缺 `FILE_SHARE_DELETE`），但严格说不是同一件事。
6. **用例 1 与探针在 POSIX 上恒绿**（`os.replace` 在 POSIX 允许覆盖被打开的文件），
   即这两条只在 Windows——本项目的实际部署目标——具备证伪能力。CI 若跑 Linux，
   它们不会替我们发现回归。


---

# 批次 16：步骤 7 的下发提示词——把「能落地的评分」从 23% 抬到 92%（A1）

## B16.1 起点：不是模型「判断资料不足」，是三道口子同时允许它放弃

`NEXT_STEPS.md` 把这项记成「B13 实测 20 条里 15 条是模型自己判待核实，提示词从未改过」。
本批先把这句话坐实到「哪一条规则、哪一句话」上。

**① 备份台账：9 份带步骤 7 台账，4 份放弃率 73–77%**

| 会话 | 指标数 | 已落地 | 待核实 | 放弃率 | 落地分 |
|---|---|---|---|---|---|
| `7049d7c9` | 26 | 6 | 20 | 77% | 15.5 |
| `e66832c3` | 26 | 7 | 19 | 73% | 13.0 |
| `2cf98e93` | 22 | 6 | 16 | 73% | 19.0 |
| `9141a214` | 20 | 5 | 15 | 75% | 16.0 |

另外 5 份放弃率为 0，但 `anchor_unresolved` / `not_mentioned` 很高（例如 `b20e9f85` 20 条无法定位）
——那是**另一类**故障，本批不碰。

**② 70 条 pending 的理由原文**：65 条是「未提供 X」（「未提供立项批复」「仅有计划量 3.2 公里，
未提供实际完成量」），5 条是「时点未到」等其它表述。逐条看下来，理由本身写得没错，问题是
**它们把「观测值缺失」直接等同于「不能评分」**。

**③ 编号不是被程序吃掉的。** 20 条 pending 里有 18 条引用了材料级编号（`P1-9-1`、`P1-11-1`、
`P1-14-1`），只有 2 条落在本步骤检索池内——但 `evidence_anchor` 支持 `material_id` 解析，
备份里同类引用是能落地的（`L3_01_02_01` 就是 `material_id` 落地的）。所以不是解析器的问题，
是模型拿到锚点仍然选择不给分。

**④ 真正的根因在步骤 5：26 条评分标准里 22 条自带「待核实」**

```
L3_02_01_01 资金到位率：到位率100%得满分；每低5个百分点扣1分，扣完为止。
            ⚠️ 现有资料仅载明资金来源安排，未提供实际到位金额，无法计算，待核实。
```

上游把结论写进了标准里，步骤 7 只是照抄。再叠加提示词里的三处授权：

| 位置 | 原文 | 作用 |
|---|---|---|
| 旧规则第 4 条 | 「对项目资料中无法判断的指标，标注为『无法评价』并说明原因」 | 整批放弃的许可 |
| 通用收尾语（所有步骤） | 「不能核实的内容写『待核实』或『资料不足』」 | 与上一句叠加 |
| 步骤 7 专用后缀 | 「没有依据的指标写待核实，不输出分值」 | 第三次强化 |

**⑤ 真实模型复现（改前，2 份备份 × 2 次）**

| 会话 | 轮次 | `scores` 形状 | 给分数值 | 放弃 | 放弃率 | 落台账 | 落地分 |
|---|---|---|---|---|---|---|---|
| `7049d7c9` | #1 | **数组** | 6/26 | 20 | 77% | 0/26 | 0.0 |
| `7049d7c9` | #2 | 对象 | 6/26 | 20 | 77% | 6/26 | 14.0 |
| `e66832c3` | #1 | **数组** | 7/26 | 19 | 73% | 0/26 | 0.0 |
| `e66832c3` | #2 | **数组** | 7/26 | 19 | 73% | 0/26 | 0.0 |

顺带查出一个此前没记录的故障：4 次里 3 次把 `scores` 写成**数组**，而 `structured_output`
只认对象 → 整轮 26 条一条都进不了台账，`not_mentioned=26`。旧提示词只写了「按指标 ID 映射到
{actual_score, …}」，模型读成数组完全合理。

## B16.2 做法：只改步骤 7 的三处下发文本 + 台账加一个披露字段

1. **重写 `STEP7_SYSTEM_TEMPLATE` 的【评分规则】**（7 条，按顺序执行）：
   - 判分顺序从「找实际值」改成「先看标准有哪几档 → 再找能落到某档的事实」，明确
     「目标值、计划量、合同额、预算金额、支出进度、时间节点、已有检查结论都算事实」；
   - **有相关事实就必须给数值分**，不得因「实际值未提供」整条放弃；
   - 未取得佐证时按标准档位计分，标准无对应档位的计 0 分，理由必须以「未提供佐证：」开头
     并写明缺哪份材料——**这批分值必须引用目标值/计划量所在段落的编号**，不能留空；
   - 「待核实」只留两种情形：时点未到、资料与指标完全不相关（连目标值都没有）；
   - 新增第 5 条：上游标准里自带的「待核实」不得抄成本步骤结论；
   - 新增第 7 条【严禁编造】：不得估算、不得补全、**不得把计划值或目标值当作实际值**。
2. **`STEP7_HUMAN_TEMPLATE` 补输出契约**：覆盖每一个三级指标；`scores` 必须是
   **以指标 ID 为键的对象**（给出可直接照抄的示例），不是数组。
3. **`performance_agent._execute_step` 里步骤 7 的收尾语单独覆盖**：通用口径的
   「不能核实就写待核实」与本步举证责任直接冲突，步骤 7 改走「按第 3、4 条处置」；
   其它步骤（8–14）原文不动。
4. **台账加 `no_evidence_scored`**：`accepted` 条目按理由前缀识别「未取得佐证计分」，
   单独计数并写进 `summary_line`（「其中 N 项按『未取得佐证』计分（非实测结果，
   须在评价局限中说明）」）。理由：这类分值与实测得分在数字上长得一样，但结论含义相反，
   不单列出来就等于用「未提供材料」凑了一个看起来完整的评分表。

**被否掉的替代方案**

- *改步骤 5 的提示词，让它别把「待核实」写进标准*：那会动到所有会话的指标体系，
  属另一批（A2 结构化闸门）；本批只在下游纠偏。
- *放宽 `evidence_anchor`，允许「无依据也给分」*：那是真放水，且会把 B11 修好的误杀/错放边界拆掉。
- *程序兜底：把 `model_pending` 一律按 0 计入总分*：把模型的选择变成程序的臆断，
  报告会显示「实测得 0 分」，事实是「没提供材料」，这是两回事。

## B16.3 验证

**① 先红后绿。** 新增 `tests/test_step7_prompt_discipline.py`（13 条）。把两个实现文件
`git stash` 后退回改前：

```
12 failed, 1 passed   （唯一绿的是「其它步骤保持通用口径」这条守卫用例，本该两版都绿）
```

恢复后：

```
13 passed
```

**② 同一份备份、只换提示词：放弃率 77% → 8%，落地 6 → 24**

| 会话 | 版本 | 形状 | 给分数值 | 放弃率 | 落台账 | 落地分 | 未取得佐证 | 无法定位 |
|---|---|---|---|---|---|---|---|---|
| `7049d7c9` | 改前 #1 | 数组 | 6/26 | 77% | 0/26 | 0.0 | — | 0 |
| `7049d7c9` | 改前 #2 | 对象 | 6/26 | 77% | 6/26 | 14.0 | — | 0 |
| `7049d7c9` | 改后 #1 | 对象 | 24/26 | **8%** | **24/26** | 15.5 | 18 | 0 |
| `7049d7c9` | 改后 #2 | 对象 | 24/26 | **8%** | **24/26** | 15.5 | 18 | 0 |
| `e66832c3` | 改前 #1 | 数组 | 7/26 | 73% | 0/26 | 0.0 | — | 0 |
| `e66832c3` | 改前 #2 | 数组 | 7/26 | 73% | 0/26 | 0.0 | — | 0 |
| `e66832c3` | 改后 #1 | 对象 | 25/26 | **4%** | **25/26** | 19.0 | 0 | 0 |
| `e66832c3` | 改后 #2 | 对象 | 25/26 | **4%** | **25/26** | 13.0 | 18 | 0 |

**「不是靠放水」的两条硬证据**

1. **`anchor_unresolved` 全程为 0，`evidence_id` 覆盖率 100%**（改前 `e66832c3` 只有 26.9%）。
   提示词要求的「硬凑锚点」风险没有出现。
2. **`7049d7c9` 的总分没变**：改前 6 条落地 15.5 分（备份原值）/14.0（重放），改后 24 条落地
   仍是 **15.5 分**——新增的 18 条全部按 0 分计，理由逐条写明缺哪份材料：

```
L3_03_01_01  0.0  "未提供佐证：计划改造主路3.2公里、辅路1.8公里（P1-9-1二、建设内容），
                   但未提供实际完成里程，无法按完成比例计分，标准无未提供档位，计0.00分。"
L3_02_01_01  0.0  "未提供佐证：目标要求到位率100%（P1-11-1），资金来源已载明（P1-14-1），
                   但未提供资金到位凭证及实际到位金额，无法计算到位率，……计0.00分。"
```

`e66832c3` 落地分从 0 变 13.0/19.0，是因为改前**一条都没进台账**（形状不合规），
不是同一批分值被抬高。

**③ 真实 14 步工作流**（新服务、本批代码）：

```
工作流E2E完成: 47 项检查, 失败 0
步骤 7 评分落地 24/24 项（已落地分值合计 26.5 分）；
其中 14 项按「未取得佐证」计分（非实测结果，须在评价局限中说明）
系统侧未落地（引用/分值/理由）：0 项；模型判定资料不足：0 项；未提及：0 项
```

**④ 全量回归**

| 检查 | 改前基线 | 改后 |
|---|---|---|
| `pytest tests/ -q --ignore=tests/rag_eval` | 274 | **287**（+13，无既有用例改动） |
| `node --test tests/test_*.cjs` | 42 | **42** |
| `test_all_apis.py` | 46/46 | **46/46** |
| `frontend_e2e_check.py` | 72/0 | **72/0** |
| `wf_e2e.py` | 47/0 | **47/0** |

**⑤ 变异检验**

```
新增 tests/_mutate_step7_prompt.py：6 个变异体全部被抓
  按未取得佐证计分不再打标 / 汇总行不披露 / 收尾语退回通用口径 /
  允许把计划值当实际值 / 删掉「严禁编造」自我声明 / 删掉未取得佐证处置规则
既有 _mutate_step7.py：28/28（其中「台账不再带 evidence_anchor」的锚点被本批改动挤断，
  已同步锚点后重新确认被抓）
既有 _mutate_consistency.py：7/7
既有 _mutate_backup_race.py：4/4
```

过程中踩到一个自造的坑：两个变异脚本并发跑，都在改 `performance_agent.py`，
互相覆盖导致「漏抓」假象。**变异脚本必须串行**（已写进本文档）。

**⑥ 提示词体积**：28639 → 29906 字符（+4.4%，约 +1.3k 字符 / +170 token），
单次调用成本变化可忽略。

## B16.4 回滚

两种，都不牵动其它批次：

1. **代码级**：`git revert <本批提交>`（只含 2 个实现文件 + 2 个测试文件 +
   `_mutate_step7.py` 的锚点同步）；或
2. **运行级（不用改代码）**：`PromptManager` 优先读 `prompt_templates` 表中
   `is_system=False` 的记录。往该表写一条 step 7 的旧提示词即可即刻回滚；
   删掉该记录就恢复本批版本。当前库里 14 条记录全是 `is_system=1`（不参与加载）。

台账新增的 `no_evidence_scored` 是纯新增字段，旧前端读不到会忽略，无兼容问题。

## B16.5 剩余风险与未验证项（诚实列出）

1. **样本只有 2 份备份、每份 2 次。** 温度 0.7，同一输入两次运行落地分出现 13.0 / 19.0 的差异
   ——差异在「哪些条目算实测、哪些算未取得佐证」的标注上，分数本身都有事实依据，
   但**标注不稳定**。要定论得跑 10 份以上备份取均值，本批没做（token 成本）。
2. **「未提供材料就不得分」是行业惯例，不是文本规定。** 财预〔2020〕10号 的表述是
   「未提供相关佐证材料的，相应指标不得分」之类的通行做法；若评价机构要求对这类指标
   出「待核实」而不是 0 分，应回滚提示词。这是**业务口径判断**，不是技术正确性，
   需要业务方确认后固化。
3. **上游没清理。** 步骤 5 的评分标准仍有 22/26 条自带「待核实」，本批只教步骤 7 忽略它。
   下次重跑步骤 5 还会写出同样的标准——真正的干净做法是在步骤 5 就不写结论（属 A2 范围）。
4. **`no_evidence` 靠理由前缀识别。** 模型不写「未提供佐证：」开头时（改后 1 次运行里
   就有 0 条被标记的情况）这批分值不会被计数，台账会显示「全是实测得分」。
   这是**已知的漏报方向**：宁可少报也不误报，但它削弱了披露的可靠性。
5. **未验证**：其它模型（qwen / openai / anthropic）在新提示词下的表现；多 worker 部署；
   全部 24 份备份逐份重放（本批只做了 2 份）；新分值是否经得起人工复核
   （没有请评价专家看过这 24 条新结果）。
6. **`_probe_step7.py` 只回放已存的模型输出**，本批的 A/B 用的是另写的真实调用脚本
   （`b16_ab.py`，在临时目录，未入库）。要复现「改前」必须 `git stash` 两个实现文件，
   这一点没有做成一条可重跑命令——是便利性缺口。


---

# 批次 17：缩水闸门扩到步骤 3 / 4（A2）

## B17.1 起点：闸门只挂在一步上，另外两步连「基准」都没有

`NEXT_STEPS.md` 记的是「`structured_output` 的缩水判定目前只对步骤 7 生效」。取证后发现，
除了那句提前返回，还有两个更前置的问题：**没有基准**，以及**基准会被数错**。

**① 27 份备份的步骤 3 输出，改前全部数出 0 条**

| 事实 | 数值 |
|---|---|
| 步骤 3 正文长度 | 7,939 – 15,431 字（27 份） |
| 指标体系叶子数 | 13 – 26 个（27 份） |
| 改前 `coverage_of_payload`（步骤 3） | **0 条，全部 0 条** |
| 步骤 4 正文长度 | 1,477 – 5,841 字（26 份） |
| 步骤 4 `scores` 键数 | 26 – 42 个（26 份） |

步骤 3 的输出是 `{"level1_indicators": [ …树… ]}`，而 `coverage_of_payload` 只找 `scores`。
所以就算把 `if step != 7` 那行删掉，步骤 3 也只会拿 0 条去比 0 条——**闸门看着开着，其实没有判据**。

**② 唯一能数出条目的那份，数是错的**

`3c6070b9` 的步骤 3 payload 顶层混着 `warning / legal_basis / total_score`（真实输出），
被「扁平映射」兜底分支当成 `scores`，数出 **5 条**（真实叶子 24 条）。
错的方向还不统一：多数是 0 条（漏），这一份是 5 条（假），两种都会让闸门失效。

**③ 回喂上限比正文短一半**

修复重试要把原输出回喂模型，上限 `MAX_ECHO_CHARS = 6000`，而步骤 3 的正文 7,939–15,431 字。
这意味着：只要步骤 3 触发一次修复，模型就只能「看着前 6,000 字重写全部 20 多个指标」——
B14 在步骤 7 上已经确认过，这是**机械性缩水**，不是模型不努力。

**④ 现状汇总**

| 步骤 | 改前能不能数条目 | 改前判不判缩水 |
|---|---|---|
| 3 | ❌ 恒 0 条（一份数成 5 条假条目） | ❌ `if step != 7` 直接返回「只对步骤 7 校验缩水」 |
| 4 | ✅ 26–42 条 | ❌ 同上 |
| 7 | ✅ | ✅ |

## B17.2 做法：给两步各自定义「条目」，再让同一套判据跑起来

1. **`Coverage` 增加 `shape`**（`scores` / `tree`），把「条目」的口径写进类型里。
2. **步骤 3 的条目 = 叶子指标**：`_iter_leaves` 按**结构**判定（没有 `children` 就是叶子），
   不按 `level == 3`——实测有模型把叶子标成 `level=2`，按层号数会漏。
   `numeric` = 叶子中带可解析 `score` 的个数。
3. **`coverage_of_payload(payload, step=None)` 分段消歧**：
   `step=3` 只走树；`step in (4,7)` 只走 `scores`；`step=None` 时**先认树**再退回 `scores`。
   第三点顺手修掉 `3c6070b9` 那 5 条假条目（老调用方不传 step 也不会再数错）。
4. **`_count_numeric` 支持按步骤给字段别名**：步骤 4 的数组形态里模型写的是 `score`
   （不是 `actual_score`），只认一个字段会让「键数没变、分值被清零」这条盲区在步骤 4 上永远判不出来。
5. **`shrink_report` 覆盖 `SHRINK_CHECKED_STEPS = (3, 4, 7)`**，条目标签按形状给
   （「叶子指标 6→3」/「条目 8→3」）；其余步骤返回**明确的不适用**而不是「没问题」。
6. **回显上限按实测长度定**：`{3: 24000, 4: 12000, 7: 24000}`，
   不产出条目型长正文的步骤（5/6/8/9/10/11）保持 6,000。

**被否掉的替代方案**

- *按 `level == 3` 数叶子*：模型标错层级就漏，而且这是「以标注为准」而不是「以结构为准」。
- *用一级指标个数当条目*：26 个叶子砍到 4 个一级指标，数目只变 4→3，拦不住。
- *给步骤 3 单独写一个 shrink 函数*：判据逻辑与 4/7 相同（条目 + 数值），分叉只会养出两套口径。
- *把闸门做成阻断式（少一条就整步失败）*：步骤 4 给漏了几个 L2 是常见瑕疵，
  整步失败会把可用结果也扔掉；本批仍沿用「拒绝缩水的修复结果、保留原文、转人工复核」。

## B17.3 验证

**① 先红后绿**：新增 `tests/test_structured_output_steps34.py`（18 条）+
改写 `test_structured_output.py` 里的两处（缩水只对 7 / 回显上限只对 7，两处旧前提都被本批推翻）。

```
改前（把 structured_output.py 退回）：14 failed, 47 passed
改后：                              61 passed
```

**② 26 份历史备份回放：0 误判、0 盲区、0 口径偏差**

```
python tests/_probe_shrink_34.py
步骤 3 覆盖：26 份，步骤 4：25 份
数不出条目（闸门仍盲）：无
误判缩水（假阳性）：无
与备份 indicator_system 的叶子数不一致：无
```

逐份对照（节选）：

| 会话 | 步骤 | 数据来源条目 | 修复后条目 | 备份真值 |
|---|---|---|---|---|
| `3c6070b9` | 3 | 24 | 24 | 叶子 24 |
| `7049d7c9` | 3 | 26 | 26 | 叶子 26 |
| `407bea68` | 3 | 13 | 13 | 叶子 13 |
| `7049d7c9` | 4 | 41 | 41 | `indicator_scores` 41 |

`3c6070b9` 这一行就是改前数成 5 条的那份：现在数 24，与真实叶子一致。

**③ 全量回归**

| 检查 | 批次 16 基线 | 本批 |
|---|---|---|
| `pytest tests/ -q --ignore=tests/rag_eval` | 287 | **306**（+18 新增 +1 拆分，无既有断言被放松） |
| `node --test tests/test_*.cjs` | 42 | **42** |
| `test_all_apis.py` | 46/46 | **46/46** |
| `frontend_e2e_check.py` | 72/0 | **72/0** |
| `wf_e2e.py` | 47/0 | **47/0**（新服务、本批代码） |

**④ 变异检验**

```
新增 tests/_mutate_shrink_34.py：8 个变异体全部被抓
  不给树形口径 / 按 level==3 数叶子 / 顶层混键退回扁平分值 /
  缩水只对 7 / 不比数值 / 不支持步骤静默放行 / 步骤 4 只认 actual_score / 回显上限退回 6000
既有 _mutate_step7.py：28/28（同步 3 处被本批重构挤断的锚点后确认全部被抓）
```

## B17.4 回滚

1. **代码级**：`git revert <本批提交>`（`structured_output.py` + 3 个测试文件 + 1 处锚点同步）。
2. **热回滚（不改代码）**：把 `SHRINK_CHECKED_STEPS` 改回 `(7,)` 一行即可让 3/4 恢复旧行为；
   `coverage_of_payload` 的新增 `shape` 字段与 `step` 参数都是向后兼容的，
   老调用方不传 `step` 时行为与改前一致（唯一差别是 `3c6070b9` 那类 payload 不再被数错——这是修正，不是行为漂移）。

## B17.5 剩余风险与未验证项（诚实列出）

1. **闸门在真实流量里还没被触发过。** 26 份备份的步骤 3/4 全部是 `ok`，从未进入修复分支；
   本批证明的是「修复缩水时会被拦住」，**没有**证明「生产里多久会遇到一次」。
   真正让步骤 4 少给分的路径（模型直接给出部分 `scores`，不走修复）本批**没有**覆盖：
   `_node_step4` 只把有值的指标 apply 上去，缺的那些沿用上一版分值，静默通过。
   这是下一步该补的口子（属质量门禁 B3 或单独一批）。
2. **步骤 3/4 的「数值清零」判据多数时候没有基准。** 首次输出 JSON 不可读时，
   `coverage_of_text` 只能退回「正文提到的编号」，`numeric=0` → 只比条目数。
   只有首次 JSON 可读、但因结构问题被驳回时，数值判据才生效（用例里就是这样构造的）。
3. **叶子口径依赖 `children` 字段名。** 模型若改用 `sub_indicators` 等别名，叶子会数成 0，
   此时 `unverifiable` 会明说「无法核验」而不是静默——但它**也不会拦截**。
4. **回显上限放大有成本。** 步骤 3 的修复请求从约 6k 字符涨到约 15k 字符，
   单次修复的 token 与延迟上升；本批**没有**测这部分成本（只在真实 14 步里确认不报错）。
5. **未验证**：其它模型（qwen / openai）的步骤 3/4 输出形状；多 worker 部署；
   前端是否把「修复缩水」的 warning 呈现给用户（只确认写进了 `warnings` 与步骤内容）。

---

# 批次 18 / A5：评分标准栏里不许夹带资料核对结论（A5 为本批新增项）

## B18.1 起点：74% 的评分标准栏写着「无法判断」，同行的分值却已经落地

`NEXT_STEPS.md` 第 7 节原本指向「`_node_step4` 非修复路径静默少给分」。取证后这条**证不出来**：

| 检查（27 份有指标树的备份） | 结果 |
|---|---|
| 步骤 4 漏给分（`missing` 非空） | **0 份** |
| 步骤 3 分值 ≠ 当前分值 | **0 份** |
| 唯一异常 `16f133f0` | 步骤 4 内容为空（`indicator_scores` 0 条）——「从未得到分值」，不是「少给」 |

于是改从「报告内部自相矛盾」这条线查，一查就到底了：

| 事实 | 数值 |
|---|---|
| 叶子指标数量合计 | 543 |
| `scoring_standard` 含「待核实 / 无法判断 / 资料不足 / 现有资料未 / 本项资料未」 | **401 条（73.8%）** |
| 其中字段整栏只有结论（连一句判分用语都没有） | 25 条 |
| 其中把「资料不足怎么办」写成规则分支（「…时标注待核实」） | 42 条 |
| 其中标准后面挂一句结论 | 334 条 |
| 批次 16 之后新跑的两份 | 仍有 67%、81% |

**这些是谁写的**：`scoring_standard` 由**步骤 3** 的 JSON 写入；步骤 5 只把 raw 文本存进
`scoring_standards["raw"]`，不进树、不进报告表。逐份核对：步骤 3 输出的条数与最终树的条数
**完全相同**（400 → 400）。

**为什么这是问题**：批次 16 把步骤 7 的落地率从 23% 抬到 92%（24/26），于是报告里出现
「标准栏：⚠️ 无法判断」+「实际得分：3.0」并排的一行。审计读到这里只会得出一个结论：
这套标准的作者自己都没打算用它评分。

真实形态（`7049d7c9` 改前输出，一字未改）：

```
L3_02_01_01 资金到位率 | 分值=4
  到位率100%得满分；每低1个百分点扣0.2分，扣完为止。⚠️ 现有资料仅载明资金来源构成，未提供实际到位金额，无法判断。
```

## B18.2 做法：把结论请出字段、把信息留在正文、程序侧只记账不清洗

1. **步骤 3 的 `STEP3_SYSTEM_TEMPLATE` 增加【scoring_standard 字段铁律】4 条**：
   ① 这一栏只写「怎么判分」（满分条件 / 扣分幅度 / 档位区间），会被整栏搬进报告；
   ② **严禁**写本项目的资料核对结论，并点名禁掉六个字样（待核实 / 无法判断 / 资料不足 /
   现有资料未提供 / 本项资料未提供 / ⚠️），配一组**反例 / 正例**——只给规则不给例子，
   模型会把它读成「少写几个字」；
   ③ 结论的落点是 **JSON 之外的正文**，标题固定为「## 资料核对提示」，逐条写
   「缺什么资料 → 影响哪些指标 ID → 建议补充哪份文件」，**不得省略**；
   ④ 资料不足**不得成为删减指标的理由**（防止模型为了让标准「干净」而砍指标）。
2. **程序侧新增 `audit_standard_purity(system)`**：数叶子、数命中、返回命中的 id 列表。
   **只计数，不改原文**——在这里顺手清洗等于把模型行为藏起来，报告里的矛盾只是从
   「看得见」变成「看不见」。
3. **`_node_step3` 把审计写进 `state["standard_purity"]`** 并打一条 warning 日志；
   **不进 `warnings` 面板**：74% 的历史会话都会命中，这不是「本份材料有问题」。
   树没建起来（`issue` 分支）时不写这个字段——不能留一个骗人的「0 条命中」。
4. **`PerformanceState` 补 `standard_purity` 声明**（`total=False` 的 TypedDict，
   不声明会在图的状态合并里被丢掉）。

**被否掉的替代方案**

- *在程序里把这些句子从 `scoring_standard` 里删掉*：信息被抹掉、模型行为被掩盖，
  而且「删到只剩结论」的 25 条会变成空字符串——比现在更糟。
- *给指标树加一个 `data_verdict` 字段，把结论搬过去*：改输出契约要连带改前端的指标表渲染、
  报告模板、导出；收益与「搬进正文」相同，代价大一个量级。
- *命中就整步退回人工复核*：74% 的会话会全部卡住，而报告本身仍然可用。
- *只改提示词、不加审计*：这批就没法证明改动的效果，也拦不住「改完更隐蔽」的漂移。

## B18.3 验证

**① 先红后绿**：新增 `tests/test_step3_standard_purity.py`（16 条：提示词 6 + 审计 6 + 节点 4）。

```
改前：14 failed, 2 passed（通过的 2 条是回归锁：JSON 契约未变、失败分支不写假 0）
改后：16 passed
```

**② 真实模型 A/B（只换步骤 3 的提示词，其余不动；DeepSeek `deepseek-chat`）**

| 会话 | 版本 | 叶子 | 含结论 | 分值合计 | 正文缺口段 |
|---|---|---|---|---|---|
| `7049d7c9` | 改前 | 20 | 15（75%） | 100 | 有 |
| `7049d7c9` | **改后** | **26** | **0** | 100 | 有（14,294 字） |
| `e66832c3` | 改前 | 25 | 22（88%） | 100 | **无** |
| `e66832c3` | **改后** | 25 | **0** | 100 | 有（含 15 条逐指标映射） |

两处关键：**叶子没有为「干净」而减少**（20→26、25→25），**缺口信息没有丢**（反而更结构化，
`e66832c3` 改前正文里根本没有缺口段）。

**③ 「结论只是搬家」的排查**

| 字段 | 改后命中 |
|---|---|
| `scoring_standard` | 0/26、0/25 |
| `explanation` | 0/26、0/25 |
| `name` | 0/26、0/25 |

改后模型不再输出 `meta` 字段（改前有 `meta.data_sufficiency`）；全仓 grep 确认
**没有任何下游读 `indicator_system["meta"]`**，信息已落在正文的「## 资料核对提示」里。

**④ 29 份历史备份回放（`python tests/_probe_standard_purity.py`）**

```
备份 29 份，叶子合计 543，命中 401（0.738）
数不出叶子（审计盲）：无
漏掉朴素口径已命中的条目：无
仅由长尾词命中：1 条（「现有资料未提供…」这种写法，三词表抓不到）
  字段整栏只有结论：25 条 / 把「资料不足」写成规则分支：42 条 / 标准后挂一句结论：334 条
```

探针最初那版把「字段整栏只有结论」误报成「审计误报」——是**判据写错了**，不是审计错：
那 25 条恰恰是最严重的一类（标准栏里一个判分用语都没有）。

**⑤ 全量回归**

| 检查 | 批次 17 基线 | 本批 |
|---|---|---|
| `pytest tests/ -q --ignore=tests/rag_eval` | 306 | **322**（+16，无既有断言被放松） |
| `node --test tests/test_*.cjs` | 42 | **42** |
| `import backend.main` | ok | **ok** |
| `test_all_apis.py` | 46/46 | **46/46** |
| `frontend_e2e_check.py` | 72/0 | **72/0** |
| `wf_e2e.py` | 47/0 | **47/0** |

（真实服务检查前先确认 8000 端口上不是旧进程：本次原进程 18:52 启动，已停掉后用本批代码重启。）

**⑥ 变异检验**

```
新增 tests/_mutate_standard_purity.py：11 个变异体全部被抓
  放宽成「可以把结论写进」/ 删反例 / 删「资料核对提示」落点 / 放开「资料不足可删指标」/
  词表收窄成只剩「待核实」/ 审计顺手清洗 / 分母混进一级节点 / 字段缺失直接取值 /
  节点不记账 / 把命中刷进 warnings / 失败分支写假 0
既有：_mutate_step7.py 28/28、_mutate_consistency.py 7/7、_mutate_backup_race.py 4/4、
      _mutate_step7_prompt.py 6/6、_mutate_shrink_34.py 8/8
```

**⑦ 顺带修掉一处残留变异体（与批次 18 无关）**

串行跑 5 个变异脚本时发现 `_mutate_backup_race.py` 的第 4 个变异体「变异点未找到」，
进一步查是**工作树里 `list_questionnaires` 那段被留成了变异体**：注释写着「必须走持锁的读入口」，
代码却是裸 `open()`。已 `git checkout` 还原（HEAD 是正确实现），重跑 4/4 全抓。
单独验证过守门用例是真的能抓住它（注入变异体 → `1 failed`），不是形同虚设。
教训已写进 `NEXT_STEPS.md` 坑表：**跑完变异脚本必须 `git status --short` 复核**，
只应看到自己的改动。

## B18.4 回滚

1. **代码级**：`git revert <本批提交>`（3 个源文件 + 3 个测试/探针文件）。
2. **热回滚（不改代码）**：把 `STEP3_SYSTEM_TEMPLATE` 里【scoring_standard 字段铁律】那 4 条
   整段删掉即回到改前行为；`audit_standard_purity` 与 `state["standard_purity"]` 是**纯增量**，
   留着不影响任何既有逻辑（不读、不判、不拦）。

## B18.5 剩余风险与未验证项（诚实列出）

1. **A/B 只有 2 份样本**，且都属「资料极少」项目；「资料充足的项目里模型本来就不写待核实」
   这个可能性没有被排除——若真是这样，本批的收益只体现在差项目上。
2. **步骤 5 的正文同样夹带结论**（26/26 份含同款字样），本批没动。它不进报告表，
   但进模型上下文——步骤 6/7 仍可能被它二次诱发。这是下一批的候选。
3. **只审计 `scoring_standard`。** 模型若把结论搬进 `explanation`，审计就是盲的；
   本次 A/B 三处字段均为 0，但那是 2 份样本，不是保证。
4. **提示词变长 +21%**（步骤 3 的 system 1,549→2,086 字符，总量 2,543→3,080，约 1,925 token）。
   成本可接受，但**廉价模型是否仍遵守禁令未验证**。
5. **存量报告不会自动变干净**：401 条历史命中仍在备份里，重跑历史会话才会用上新提示词。
6. **未验证**：qwen / openai 的遵守率；多 worker 部署；前端指标表在「缺口段」出现后的渲染
   （只确认 `wf_e2e.py` 47/0 通过）。

---

# 批次 19 / A6：让步骤 7 的引用有编号可引，并把「引用太粗」从「引用无法定位」里拆出来

## B19.1 起点：48 条「引用无法定位」里，只有 36 条还是问题，而其中 21 条其实是「只写了文件名」

`NEXT_STEPS.md` 第 7 节原本指向「步骤 5 正文里的『待核实』」。取证后**否定**了这条：

| 检查 | 结果 |
|---|---|
| 步骤 5 正文含「待核实 / 无法判断 / 资料不足」 | 26/26 份 |
| 步骤 5 的产出被谁读 | 只落盘 + 走 artifacts 接口给人看；步骤 6 读 `indicator_system_full_json`、步骤 7 读 `evaluation_table_markdown`，**都不读它** |
| `indicator_standards` 字段 | 只在备份字段清单里出现，没有任何写入/读取——死字段 |

所以步骤 5 的「待核实」只是**给用户看的死产物**，不构成下游诱因，优先级下降。
改查步骤 7 的台账，找到真正的问题：

**① 台账里 48 条 `anchor_unresolved`，用今天的解析器重放，12 条已经能解析**

台账是会话当时的快照。`b20e9f85` 的注释里就写着「按强度排序」那次修复正是针对它的
`第1段及第四部分`——旧代码的 `anchor_unresolved` 不能当现状。重放后真实剩余 **36 条**：

| 原因（`Anchor.reason`） | 条数 | 真实形态 |
|---|---|---|
| `too_coarse` | **21** | `fe_wf_sample.txt`（只写文件名，落不到行） |
| `unknown_id` | 8 | `P1-5-1` 等——符合块编号规则，但当时没下发 |
| `line_unverifiable` | 5 | `fe_wf_sample.txt-第1段`；重放缺原文行数，线上能落成弱定位 |
| `empty` | 2 | 既没写编号、reason 里也没有锚点 |

**② 21 条「只写文件名」不是模型偷懒，是没东西可引**

| 事实 | 改前 |
|---|---|
| 样例材料行数 | 16 行 |
| 步骤 7 实际下发的项目块 | **3 条**（`PROJECT_LIMIT = 3`） |
| 3 条块的合计字数 | **53 字** |
| 分类原文里的块编号（`P1-x-y`） | **0 个** |

模型看到的是「16 行原文 + 3 个带编号的块」，而 16 行里 13 行没有任何编号。
它只能写文件名。台账把这个记成「引用无法定位」——看起来像系统定位失败，
实际是**可引的东西没下发**。这就是本批要修的东西。

## B19.2 做法：小材料把块发全（含未命中的），失败原因分得开

1. **`effective_project_limit`**：只对步骤 7、只在调用方没显式指定限额时生效；
   材料总字数 ≤ `PROJECT_CHAR_BUDGET = 6000` 时把块数放到全部（上限
   `MAX_ADAPTIVE_PROJECT_BLOCKS = 40`），超过预算仍按 3 条。
2. **`_augment_with_unscored`：光调高 limit 不够。** BM25 只返回命中查询词的块——
   实测把 limit 从 3 调到 16，仍然只回来 8 条。所以要把「同一份材料里、只是没匹配上查询词」
   的块按原文顺序补进去。补的不是放宽判定：编号仍来自真实块，锚点仍要落到行。
3. **`Anchor` 增加 `reason`**（`empty` / `too_coarse` / `unknown_id` / `unknown_file` /
   `line_out_of_range` / `line_unverifiable` / `no_match`），只解释判定、不改判定。
4. **新状态 `anchor_too_coarse`**（「引用只到文件（没到行）」）；其余原因仍归
   `anchor_unresolved`——对用户来说是同一件事（引不到），不必再拆。
   台账明细同时写 `anchor_reason`，供审计按原因计数。
5. **前端**：`systemSide` 与台账 chips 增加该状态（否则计数会漏显示）。

**被否掉的替代方案**

- *加 `anchor_missing`（根本没写引用）*：实测只有 2 条，而且 `evidence_id` 为空时会回退去读
  `reason`（B14 的既有设计），「没写引用」在台账里通常表现为 `no_match` 而不是 `empty`——
  再加一个几乎不进人的状态只增加口径。已写进代码注释。
- *把「第N段」判成 `too_coarse`*：既有口径里「第N段」等同「第N行」（模型把「提取文本第10行」
  写成「第10段」），行数可核验时它落成弱定位。为了分桶好看把它打到 `too_coarse`，
  等于把本来能落的引用判死。
- *把编号池放宽到「本会话所有步骤下发过的编号」*：实测只能多落地 0–1 条，
  收益不足以承担「引用了一个模型没看到的块」的风险。
- *把所有步骤的限额都放开*：其它步骤没有实测证据支持改动；评估的「问句模式」
  （显式传 query）必须保持固定预算，否则 Recall@3 基线不可比。

## B19.3 验证

**① 先红后绿**：新增 `tests/test_step7_anchor_coverage.py`（19 条：覆盖 5 + 原因 7 + 台账 7）。

```
改前：15 failed, 3 passed（通过的是回归锁：其它步骤仍 3 条、显式限额不被改写、编造编号仍拒）
改后：19 passed
```

既有 `test_step7_scoring.py` 改了 1 条的前提（`test_filename_without_line_number_is_still_rejected`）：
**拒绝的结论没变**（`indicator_score_map` 仍为空），变的是台账怎么称呼它
（`anchor_unresolved` → `anchor_too_coarse`）。这不是放松断言，是把两种不同原因分开。

**② 真实模型 A/B（同一份备份，只切覆盖开关；`old` 模式把 `ADAPTIVE_PROJECT_STEPS` 置空）**

| 会话 / 模式 | 块数 | 证据字数 | 落地 | 台账 | 已落地分值 |
|---|---|---|---|---|---|
| `fb265ba3` old（第 1 次） | 3 | 53 | 7/26 | 16 条只到文件 | — |
| `fb265ba3` old（第 2 次） | 3 | 53 | 11/26 | 14 条弱定位 + 1 条只到文件 | 13.0 |
| `fb265ba3` old（第 3 次） | 3 | 53 | 0/26 | 输出被截断 → 人工复核 | 0.0 |
| `fb265ba3` **new**（第 1 次） | **16** | **339** | **26/26** | 全部编号 | — |
| `fb265ba3` **new**（第 2 次） | **16** | **339** | **20/26** | 全部编号 + 5 未提及 | **12.8** |
| `a558644b` old | 3 | 53 | 24/24 | 全部编号 | — |
| `a558644b` **new** | **16** | **339** | 24/24 | 全部编号 | — |

两处关键：**引用形态从「文件名/段」变成「编号」**（旧模式落地条目里混着
`fe_wf_sample.txt第3行`，新模式全是 `P1-x-y`，不同编号从 8 个增到 14 个）；
**总分没有虚高**（13.0 → 12.8，多落地的 9 条多是 0 分）。

抽查 6 条引用与指标是否对得上：`绩效指标明确性 → P1-10-1`（目标1）、
`资金分配合理性 → P1-14-1`（资金安排）、`立项依据充分性 → P1-2-1`（一、项目概况，0 分）
——指向合理，没有出现「为了落地乱引一条」。

**③ 端到端（`wf_e2e.py` 新跑会话 `72111c2c`）**

步骤 7 实际下发 **16 块 / 339 字**（改前 3 块 / 53 字），覆盖改动在真实流水线里生效。

**④ 29 份备份回放（`python tests/_probe_anchor_gaps.py`）**

```
台账记成「引用无法定位」共 48 条
用今天的解析器重放已可解析：12 条 → 台账过期，不是现状缺陷
重放后仍失败：36 条
  too_coarse 21 / unknown_id 8 / line_unverifiable 5 / empty 2
把编号池放宽到「本会话所有步骤下发过的编号」可额外落地：0 条
```

**⑤ 全量回归**

| 检查 | 批次 18 基线 | 本批 |
|---|---|---|
| `pytest tests/ -q --ignore=tests/rag_eval` | 322 | **341**（+19，另有 1 条按新语义更新） |
| `node --test tests/test_*.cjs` | 42 | **42** |
| `import backend.main` | ok | **ok** |
| `test_all_apis.py` | 46/46 | **46/46** |
| `frontend_e2e_check.py` | 72/0 | **72/0** |
| `wf_e2e.py` | 47/0 | **47/0** |

（服务用本批代码重启：原进程 20:19 启动，已停掉。）

**⑥ 变异检验**

```
新增 tests/_mutate_anchor_coverage.py：13 个变异体全部被抓
  取消自适应 / 不补未命中块 / 预算失效 / 条数上限失效 / 放开到其它步骤 /
  覆盖显式限额 / 空引用不单列 / 只到文件判成编造 / 引错文件归到只到文件 /
  行号越界混为一谈 / 映射失效 / 不记原因 / 只到文件被放行
既有：_mutate_step7.py 28/28（同步 1 处被本批挤断的锚点）、
      _mutate_consistency.py 7/7、_mutate_backup_race.py 4/4、
      _mutate_step7_prompt.py 6/6、_mutate_shrink_34.py 8/8、
      _mutate_standard_purity.py 11/11
```

## B19.4 回滚

1. **代码级**：`git revert <本批提交>`。
2. **热回滚（不改代码）**：把 `ADAPTIVE_PROJECT_STEPS` 改成 `()` 即回到「每步 3 条」；
   `Anchor.reason`、`anchor_too_coarse`、前端 chips 都是纯增量——回滚覆盖后
   这些状态不会再出现，但代码留着不报错。

## B19.5 剩余风险与未验证项（诚实列出）

1. **输出截断是本批暴露出来的既有故障，但可能被放大。** 端到端会话 `72111c2c`
   步骤 7 输出在 12,862 字处被截断（JSON 写到一半），修复又缩水 → 转人工复核 → 0/31 落地。
   这不是本批引入的（旧会话 `3c6070b9` 也有 13,179 字的同类输出），但材料看得全之后
   模型写得更长，风险更高。改前 A/B 三次里也有一次截断。**下一步该做「步骤 7 输出的长度纪律」**
   （理由限字数 + 超长时先写 JSON 再写分析），本批未做。
2. **A/B 只有 2 份会话、3+2 次运行**，且都是同一份 16 行材料。结论只覆盖「小材料」这一档；
   大材料（> 6,000 字）仍走 3 条，本批**没有**验证它是否会因块太少而出现同样的粗引用。
3. **总分变化不等于结论变好。** 13.0 → 12.8 说明新落地的分值多是 0 分（未取得佐证），
   但这只是 1 组对照；「落地率提高是否改善了报告质量」本批没有独立判据。
4. **引用正确性只抽查了 6 条。** 「编号真实存在」由程序保证，「这个编号支持这条评分」
   仍靠模型自觉——本批没有逐条人工核对全部 26 条。
5. **评估基线未受影响这一点是设计保证，不是实测**：`query` 显式传入时跳过自适应，
   但 `tests/rag_eval` 本轮没有重跑（它的 Recalling@3 基线 0.448 未被触碰）。
6. **未验证**：qwen / openai 下「小材料全给」是否同样提升引用质量；多 worker 部署；
   前端 chips 在真实会话里的显示（只跑通 72 条 e2e 与工作流 47 项）。

---

## B20.1 起点：评分「没落地」不是模型不会写 JSON，是它根本没写完

批次 19 端到端跑出过一次「步骤 7 全部 0 落地」。本批先把这条查到底。

**① 语文证据：12,862 字的输出，JSON 断在半截**

| 会话 | 输出字数 | JSON 起始位置 | JSON 状态 | 落地 |
|---|---|---|---|---|
| `72111c2c` | 12,862 | 第 9,225 字（72% 处） | 断在 `"evidence_id": "P1-7-1;P1` | **0/31** |
| `3c6070b9` | 13,179 | 无 JSON（只有表格） | 不可解析 | 0/24 |
| `268b184a` | 11,193 | 第 7,011 字 | 完整 | 19/26 |

正文在前、JSON 在后。**撞上长度上限时，先被砍掉的是评分本身**，正文只是「解释」。

**② token 证据：`completion_tokens` 正好 8,192**

`token_usage` 表里 `72111c2c` 的步骤 7 是 `completion_tokens = 8192`——DeepSeek 单次输出硬顶。
而 `.env` 写着 `MAX_TOKENS=4000`。两者不可能同时为真。

**③ 根因：配置的那个上限从来没生效**

`langchain-openai 1.2.1` 把 `max_tokens` 发成 `max_completion_tokens`，DeepSeek 的兼容接口
只认请求体里的 `max_tokens`，于是这个字段被当未知参数丢掉。实地验证（同一句提示词、要求数到 40）：

| 写法 | 请求体里的字段 | `finish_reason` | 实际输出 |
|---|---|---|---|
| 当前线上（`max_tokens=60`） | `max_completion_tokens: 60` | `stop` | **79 token**（上限被忽略） |
| `extra_body={"max_tokens": 60}` | `extra_body: {"max_tokens": 60}` | `length` | **60 token**（生效） |

所以「按 4000 截断」这个假设不成立：真实上限是模型自己的 8192，配置只是躺着。

**④ 但也不能顺手把 4000 变成真的**

`token_usage` 全表按步骤取 `MAX(completion_tokens)`：

| 步骤 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 最大 | 4,468 | 7,605 | 5,835 | 7,669 | **10,352** | 8,434 | 8,192 | 2,147 | 4,834 | 6,130 | 3,206 | 1,653 | 2,451 |

九个步骤的实测最大输出都超过 4000。把全局上限变成真的，等于一次性截断九步——那是另一个
批次的量级与风险。

**⑤ 规模**

28 份有步骤 7 输出的备份：**17/28 的 JSON 不在最前**、**19/28 的正文超过 1,200 字**、
1 份 JSON 不可解析。理由是第二处膨胀源：`268b184a` 一轮 26 条里有 **23 条理由超过 60 字**（最长 127 字）。

## B20.2 做法：先给 JSON、再写字；预算只给这一步；截断必须记账

1. **输出契约改成「先 JSON 后正文」**（`STEP7_SYSTEM_TEMPLATE` + `STEP7_HUMAN_TEMPLATE`
   + `_execute_step` 里那段运行期收尾语，三处同改——只改一处等于没改）。
   并写明理由：一旦撞上限，写在 JSON 前面的正文会把额度花光，评分整轮作废。
2. **字数纪律**：`reason` 每条 ≤ 60 字；正文 ≤ 1,200 字，只写「汇总表 + 3~5 句整体分析 +
   未取得佐证清单」，不许在正文里逐指标重复理由；长度不够时按
   「整体分析 → 汇总表 → reason 细节」舍弃，**JSON 的条目与 actual_score 绝不可省**。
3. **`STEP_OUTPUT_BUDGETS = {7: 6000}`**，由 `_llm_for_step()` 通过
   `extra_body={"max_tokens": budget}` 下发（只对 deepseek 加 `extra_body`，其它 provider
   仍走 `max_tokens → max_completion_tokens`）。绑不上就退回默认实例，不让调用直接失败。
4. **截断记账** `_note_truncation()`：`finish_reason ∈ {length, max_tokens}` 时写
   `state["truncated_steps"]` + 一条 warnings + 日志；流式（取最后一块的
   `response_metadata.finish_reason`）与非流式都覆盖，同一步只记一次。
5. **长度画像** `structured_output.audit_step7_length()`：`total/json/prose/json_first/
   json_parsed/items/reason_over_limit/reason_max_chars` 八项，落 `state["step7_length"]`，
   **只统计不改写**（与 B18 的 `audit_standard_purity` 同一套口径）。JSON 之前允许 200 字
   引导语，超过就算「不在最前」。
6. **两个新字段进备份白名单**：`truncated_steps` / `step7_length`。端到端那一轮已经暴露过
   这个坑——`warnings` 里有截断告警，备份里 `truncated_steps` 却是 `null`（白名单没跟上，
   和 `score_disposition` 当年一模一样）。

**被否掉的替代方案**

- *把 `MAX_TOKENS=4000` 修成真的*：见起点 ④，会截断九个步骤。本批只收紧真正量过的那一步。
- *只调大步骤 7 的 `max_tokens`*：8192 已是模型硬顶，抬不上去。长度必须靠「少写」解决。
- *程序侧截断 reason*：改坏模型输出会掩盖它的真实行为（B18 的结论同样适用），只计数。
- *截断后自动重试一次*：重试仍会撞同一个上限，还可能把两份输出混进 SSE 逐字流；
  先把「可解析」做出来（JSON 在前），再把「被截断」显式暴露给人。

## B20.3 验证

**改前红 / 改后绿**：`tests/test_step7_output_length.py` 29 条。先红 22 条（其余 7 条是
「不许退化」的护栏，本来就绿），改后 29/29。

**回放 29 份备份**（`tests/_probe_step7_length.py`，用新口径量旧输出）：

| 指标 | 结果 |
|---|---|
| 有步骤 7 输出的会话 | 28 |
| ≥8,000 字 | 9（最长 13,179） |
| JSON 不可解析 | 1（`72111c2c`） |
| **JSON 不在最前** | **17/28** |
| **正文超过 1,200 字** | **19/28** |

**A/B 真实模型**（同一份备份 `72111c2c`，31 个叶子；旧版代码用 `git worktree add --detach HEAD`
取，两版只差本批改动）：

| 指标 | 旧（9 轮） | 新（6 轮） |
|---|---|---|
| `finish_reason=length` | **7/9（78%）** | **0/6（0%）** |
| 截断且 JSON 不可解析 | 2/9 | 0/6 |
| 完全 0 落地 | 2/9 | **0/6** |
| 落地率（mean / min / max） | 75% / 0% / 100% | **96% / 94% / 100%** |
| JSON 在最前 | 5/9 | **6/6** |
| 输出字数 | 4,904–12,854 | **4,066–4,540** |
| `output_tokens` | 7,088–8,192 | **2,180–2,519** |
| 总分 | 0 / 0 / 5.5 / 7.5 / 7.5 / 8.0 / 9.5 / 15.0 / 19.5 | 9.0 / 11.5 / 14.0 / 15.0 / 18.5 / 19.0 |
| 超 60 字理由（每轮） | 0–31 | **全 0** |

关于总分：新版均值 14.50 高于旧版 8.06，但**上限没更高**（19.0 ≤ 19.5）。差异来自旧版把整轮
评分丢成 0 分（截断），不是新标尺放宽。新版的 2 条未落地是 `model_pending`（模型按规则拒绝），
不是系统丢分。

**端到端**（真实工作流，新会话 `8b8443a3`）：

| 项 | 结果 |
|---|---|
| 步骤 7 输出 | 3,422 字（JSON 3,334 / 正文 88），**JSON 在最前**、可解析 |
| 步骤 7 落地 | 28/29，落地分值 23.0 |
| 长度画像 | `json_first=True`、超字数理由 0 |
| wf_e2e | **47 项 / 0 失败** |

**变异**：新增 `_mutate_step7_length.py` **13/13 全抓**（去掉 `extra_body`、预算退回 4000、
预算扩到别的步骤、调用不走带预算的实例、`finish_reason` 判定反转、截断不写 warning、
节点不落画像、两处提示词顺序回退、字数上限放宽十倍、审计恒真、审计不数超字数、
备份白名单漏字段）。既有 28 / 7 / 4 / 6 / 8 / 11 / 13 全抓，无锚点失效。

**回归**：`pytest 370 passed`（341 → 370）、`node 42 pass`、`test_all_apis 46/46`、
`frontend_e2e_check 72/0`、`wf_e2e 47/0`。

## B20.4 回滚

- 预算：`STEP_OUTPUT_BUDGETS = {}` 一行即回到「不设上限、由模型自己停」。
- 提示词：`active_prompts` 支持按步骤覆盖，可单独回退步骤 7 的模板而不动代码。
- 代码：`git checkout -- backend/agents/performance_agent.py
  backend/agents/prompts/default_prompts.py backend/agents/tools/structured_output.py
  backend/agents/states/performance_state.py`。
- 记账与审计是纯增量（新字段、新 warnings），回滚提示词后它们只会报告「不符合纪律」，
  不影响评分判定。

## B20.5 剩余风险与未验证项（诚实列出）

1. **只给步骤 7 设了预算，另外九个步骤仍是「跑到模型硬顶」。** 这不是推测：本批的截断记账
   第一次上端到端就抓到 **步骤 5 在 13,349 字处被截断**（`8b8443a3`），工作流照样「completed」。
   下一步该按 token 数据给步骤 3/5/6/9/10 定预算或加长度纪律——**步骤 5 见过 10,352 token，
   8192 的硬顶下它天生写不完**。

   > **勘误（批次 21 复核时更正）**：这里原文还写着「后果是同轮 `scoring_standards` 只剩
   > **1 条**（29 个叶子覆盖 0 条）」——**那是误读，撤回**。`_node_step5` 恒写
   > `state["scoring_standards"] = {"raw": content}`（`performance_agent.py:1411`），
   > `1` 个键是设计如此，不是丢指标；按指标名重新统计，那一轮步骤 5 覆盖 **29/29**，没有漏。
   > **截断本身是真的**（旧版 A/B 的 3 轮里步骤 5 有 2 轮停在 8,192、历史 7/28 次 ≥7,800），
   > 但「只剩 1 条」看错了字段形态——这也正是批次 21 要给这一步补覆盖度审计的原因：
   > 只有把「覆盖了几个叶子」量出来，「截断有没有造成后果」才不是靠读全文猜。
2. **6000 是按「新提示词下单轮 2,180–2,519 token」推的，不是按最坏情况。** 材料变大
   （40 块 / 6,000 字）时仍可能撞顶；撞了会记 `truncated_steps` 并告警，但**没有自动重试或降级**。
3. **`reason ≤ 60 字` 靠模型自觉，程序只统计不纠正。** A/B 6/6 达标、端到端 0 条超标，
   但都是在 354 字的样例材料上；26 个叶子的真实长材料没验过。
4. **`extra_body.max_tokens` 只对 deepseek 生效。** qwen / openai 走的是
   `max_tokens → max_completion_tokens`（OpenAI 侧本来就这么发），没有实测过真实调用；
   换 provider 后预算是否生效未验证。
5. **流式路径的截断只由单测覆盖。** 端到端的 47 项检查是非流式轮询，SSE 实时场景下
   `finish_reason` 是否能稳定取到最后一块没有实测。
6. **`json_first` 的 200 字阈值是拍的。** 端到端输出会被代码在前面拼一行台账
   （约 90 字），200 字刚好容得下；若将来前缀变长，审计会误报「不在最前」——它只影响
   `step7_length` 这个画像字段，不影响评分。

## B21.1 起点：步骤 6 每轮把同一份数据写两遍，而程序一份都没读

批次 20 只给步骤 7 设了预算。`token_usage` 里撞顶更狠的是另外两步：步骤 5 最大
**10,352** token（2 次正好停在 8,192，7/28 次 ≥7,800）、步骤 6 **8,434**。本批先量清
「多出来的那一半是不是有用的东西」。

**① 步骤 6：29/29 都带一份 JSON 副本，占掉整段输出的一半**

`tests/_probe_step56_length.py` 回放 29 份有步骤 6 输出的备份：

| 项 | 结果 |
|---|---|
| 输出字数 | 6,678–18,291 |
| 带 JSON 副本 | **29/29** |
| JSON 副本字数 | 中位 **6,218**，最多 12,576 |
| JSON 占该步输出 | **50%–70%**（`3c6070b9`：18,291 字里 12,576 字是 JSON） |
| 表行数 < 叶子数 | **0 份** |
| 步骤 5 指标覆盖缺口 | **0 份**（29 份都覆盖满全部叶子） |

表格没少写、指标没漏报——所以多出来的那份 JSON 不是「必要的第二份」，而是
**模板明文要求的重写**：步骤 6 的提示词同时要 Markdown 表格和一份 JSON 副本。

**② 没有人解析那份副本，它原样变成报告正文里的裸 JSON**

`evaluation_table` 的下游只有一处（`agent_routes.py:1419`）：
`("三、评价指标体系", values.get("evaluation_table"))`——**原样拼进报告第三章**，
中间没有任何解析。27 份有 `final_report` 的会话，**27 份都含一段裸 JSON 代码块**，
最高占报告 **24%**（`b65e0b2c`：40,439 字报告里 9,842 字是 JSON）。

**③ 步骤 5 的截断是真的，但后果不是「丢指标」**

29 份的覆盖缺口是 0，说明批次 20 那条「`scoring_standards` 只剩 1 条」确实是误读
（勘误见 B20.5）。真实后果是**尾部断在半句**：`8b8443a3` 断在「实」、`ab11649e`
断在「准」。这件事肉眼看不出来——这正是本批要补覆盖度审计的理由。

## B21.2 做法：步骤 6 只写一遍；步骤 5 每指标不超过 6 行；两步都量覆盖度

1. **步骤 6 模板只留一个 Markdown 表格 + 一行校验注**，并写明理由（程序已持有结构化数据、
   实测副本占掉一半篇幅、没有任何程序解析它、它只会变成裸 JSON 拼进报告第三章）。
   模板里直接写「**不要输出 JSON 副本**」——只说「可以省略」不会生效，这是 B16 的教训。
2. **步骤 5 新增【长度纪律 — 每个指标不超过 6 行】**：每个指标只写档次 / 满分条件 / 扣分项；
   不重复指标解释；不写「使用说明」「总体说明」；一个指标的档次最多 3 档；按一级指标分组，
   组前一行 `## 一级指标名称（X 分）`；**不许省略任何一个指标**（少一个指标比多写两百字严重）。
3. **`STEP_OUTPUT_BUDGETS = {5: 8000, 6: 6000, 7: 6000}`**。两步都靠「少写」解决，预算只作为
   「撞了要可预测」的下限；步骤 5 给 8,000 是给长材料留余量（旧版最长 24,570 字）。
4. **覆盖度审计（只统计、不改写）**：`audit_step5_coverage()` / `audit_step6_table()`，
   共用 `_leaf_coverage()`（判据：叶子指标名是否出现在正文里，只会**高估**覆盖——
   漏报比误报更难发现）与 `_markdown_table_rows()`（数数据行，跳过表头与分隔线）；
   落 `state["step5_coverage"]` / `state["step6_table"]`，缺指标时写一条 warnings + 日志。
5. **两个新字段进备份白名单**：`step5_coverage` / `step6_table`。不加的话重启/重放之后
   「这一步少了几个指标」只能靠读全文数——和 `score_disposition`、`step7_length` 同一个坑。

**被否掉的替代方案**

- *组装报告时用正则剥掉 JSON 代码块*：治标，而且半截 JSON 剥完更难看；真正的问题是
  「一份数据在状态里存两次、一次进报告」。留待下一批（见 B21.5 第 1 条）。
- *直接给步骤 5 砍字数上限*：会真的丢评分标准，正是要防的那件事。
- *把全局 `MAX_TOKENS=4000` 修成真的*：会一次截断九个步骤（B20 起点 ④ 同样结论）。
- *给步骤 6 也加「先 JSON 后表格」的顺序*：这里反过来——表格才是人看的产物，JSON 没人看。

## B21.3 验证

**改前红 / 改后绿**：`tests/test_step56_output_budget.py` 22 条。

**回放 29 份备份**（`tests/_probe_step56_length.py`）：见 B21.1 ①——这就是改前基线。

**A/B 真实模型**（同一份备份、同一份 `.env`；旧代码用 `git worktree add --detach HEAD` 取，
两版只差本批改动）：

| 指标 | 步骤 5 旧（3 轮） | 步骤 5 新（5 轮） |
|---|---|---|
| 输出字数 | 9,048–11,968 | **4,186–5,453** |
| `output_tokens` | 5,733–7,476 | **2,824–3,712** |
| `finish_reason=length` | 0/3 | 0/5 |
| 覆盖 | 29/29 | 29/29 |

| 指标 | 步骤 6 旧（3 轮） | 步骤 6 新（3 轮） |
|---|---|---|
| 输出字数 | 14,699–15,591 | **4,871–5,170** |
| `output_tokens` | 7,549 / **8,192** / **8,192** | **3,025–3,217** |
| `finish_reason=length` | **2/3** | **0/3** |
| JSON 副本 | 1 轮检出 8,849 字（另两轮撞顶，代码块没闭合） | **0 / 0 / 0** |
| 表行数 | 39 / 56 / 58 | **33 / 33 / 33** |
| 覆盖 | 29/29 | 29/29 |

表行数的差异要说明：新版恒 33 行 = **29 个叶子 + 4 个一级分组行**，结构可预测；旧版
39–58 行（叶子数相同）多出来的行**没有定位原因**，本批只保证「行数稳定且覆盖 29/29」。

**端到端**（真实工作流，新会话 `73604287`，32 个叶子）：

| 项 | 结果 |
|---|---|
| 步骤 5 | 4,681 字，4 个一级分组行（`## 决策（15 分）` …） |
| 步骤 5 覆盖 | `{"leaves": 32, "covered": 32, "missing_ids": []}` |
| 步骤 6 | 3,993 字（旧同规模会话 12,246 字），**无 JSON 代码块** |
| 步骤 6 审计 | `{"leaves": 32, "covered": 32, "rows": 32, "missing_ids": []}` |
| `truncated_steps` | **None**（步骤 5/6/7 都没撞顶） |
| 报告 | 33,423 字；裸 JSON 3,738 字 = **11.2%**（旧 `8b8443a3` 是 7,080 / 37,727 = 18.8%） |
| wf_e2e | **47 项 / 0 失败** |

剩下的 11.2% 不是步骤 6 的：它来自**步骤 7 的 JSON 评分块**（第四章原样贴进去），
是 B20「先 JSON 后正文」的副作用，本批没动——见 B21.5 第 1 条。

**变异**：新增 `_mutate_step56_length.py` **12/12 全抓**（步骤 6 把 JSON 副本要回来、
步骤 5 行数上限放宽十倍、允许写「使用说明」、预算表退回只管步骤 7、步骤 6 预算压到 4000、
步骤 5 覆盖度恒报全绿、表格行数把表头算进去、节点不落覆盖度 ×2、覆盖不足不告警、
覆盖判据改成按 id、备份白名单漏字段）。既有 8 个脚本 **28 / 7 / 4 / 6 / 8 / 11 / 13 / 13
全抓**，无锚点失效。

> **变异跑之前先验伤**：这轮复核踩到一个工具坑——上一轮被中断的变异把
> `"no_evidence": reason_text.startswith(NO_EVIDENCE_MARKER),` 留在了盘上（写成了 `False`），
> 于是 `_mutate_step7` 报「1 个存活」、`_mutate_step7_prompt` 报「变异点未找到」。
> 真相是文件早被污染，不是用例有洞。规矩补一条：**跑变异前先 `git diff` 对一遍安全副本，
> 跑完除了 `git status --short` 还要 `grep` 关键行**（详见 `NEXT_STEPS.md` 第 7 节）。

**回归**：`pytest 392 passed`（370 → 392）、`node 42 pass`、`test_all_apis 46/46`、
`frontend_e2e_check 72/0`、`wf_e2e 47/0`。

## B21.4 回滚

- 预算：从 `STEP_OUTPUT_BUDGETS` 删掉 5 / 6 两个键，即回到「只有步骤 7 有预算」。
- 提示词：`active_prompts` 支持按步骤覆盖，可单独回退步骤 5 或步骤 6 的模板而不动代码。
- 代码：`git checkout -- backend/agents/performance_agent.py
  backend/agents/prompts/default_prompts.py backend/agents/states/performance_state.py`。
- 覆盖度审计是纯增量（两个新字段 + warnings），回滚提示词后它只会报告「缺指标」，不影响评分判定。

## B21.5 剩余风险与未验证项（诚实列出）

1. **报告第四章仍然原样贴步骤 7 的 JSON 评分块**（新会话 3,738 字 / 占报告 11.2%）。
   这是 B20 把「JSON 放前面」的直接副作用，本批只清掉了第三章那一份。正确的修法不是正则剥离，
   而是**把步骤 7 的输出拆成两个字段**（`step7_scores_json` 与 `step7_prose`），组装报告只取正文；
   这要动状态字段与导出，属于下一批。
2. **步骤 5 的 8,000 是「新版 2,824–3,712 token」推的**，不是按最坏情况。历史最长一档
   24,570 字（`3c6070b9`）没有在新提示词下重跑过，仍可能撞顶；撞了会记 `truncated_steps`
   并告警，但**没有自动重试或降级**。
3. **覆盖度判据是「指标名出现在正文里」，只会高估覆盖。** 模型在「本节略」里写一遍名字也算覆盖。
   要可靠得逐条对齐（导出侧已有逐行比对），这里只作为「漏报的报警器」。
4. **表行数 33 依赖「一级分组行」这个约定。** 两次实测一次 32 行（无分组行）一次 33 行，
   `rows` 会随之变化，但不影响 `covered` / `missing_ids`——判断缺指标只看后两者。
5. **只管了步骤 5 / 6 / 7 三步。** 步骤 4（7,669）、2（7,605）、3、9、10 仍跑到模型硬顶。
6. **A/B 用的是 354 字的样例材料**，长材料（40 块 / 6,000 字）在新提示词下的体量没验过。

## B22.1 起点：报告第四章有 11.2% 的篇幅是一段没人读的 JSON

批次 21 清掉了步骤 6 那份 JSON 副本（第三章），剩下的问题在第四章。

**① 证据：新会话 `73604287` 的报告 33,423 字，其中 3,738 字（11.2%）是评分 JSON**

贴在「四、指标得分及分析」开头，形态是：标题与一行台账之后，紧跟着一个 JSON 代码块
（围栏 + `{"scores": {"L3_01_01_01": {"actual_score": 2.0, …`），再往后才是正文。

**② 来源不是模型乱写，是「组装 = 直拼原文」**

B20 把「先 JSON 后正文」写进契约后，步骤 7 的原始输出就是「JSON + 正文」两段；
而报告组装直接把 `state["indicator_analysis"]`（= 那段原始输出）拼进第四章。
**三条组装路径都这么做**：

| 路径 | 取的内容 |
|---|---|
| `PerformanceAgent._assemble_final_report`（步骤 14 写 `final_report`，报告查询首选） | `state["indicator_analysis"]` |
| `agent_routes._assemble_report_from_steps`（内存态回退） | `values["indicator_analysis"]` |
| `agent_routes._assemble_report_from_backup_steps`（磁盘投影回退） | `step_results["7"]["content"]` |

**③ 规模**：27 份有 `final_report` 的会话，27 份含 JSON 代码块，最高占 **24%**
（`b65e0b2c`：40,439 字里 9,842 字）。

## B22.2 做法：落盘时拆一次，组装只取正文

1. **`structured_output.split_step7_output(text) -> (json, prose)`**：用既有的 `extract_json`
   取 blob，**先 `json.loads` 验一遍**，合法才剥；剥的时候连包裹的代码块围栏一起剥，
   正文里的连续空行压成一个空行。识别不到合法 JSON 就整段当正文返回——半截 JSON 切一半
   比留着更难读，不做猜测式切割。
2. **步骤 7 落两个新字段**：`step7_scores_json`（原样 JSON 文本）与 `step7_prose`（正文）。
   **`indicator_analysis` 保持原始全文不动**——步骤 9/11 的提示词与 refine 都读它，
   改语义等于顺手改了别的步骤的输入。
3. **三条组装路径统一「优先正文、回退原文」**：`_assemble_final_report` 新增
   `_chapter_text(state, key)`（自定义章节排序的两条分支也走它），路由侧新增
   `_chapter_from_values(values, key)`，备份组装对 step `"7"` 取 `backup["step7_prose"]`。
4. **人工改步骤 7 跟着重算**：`PUT /workflow/{id}/steps/7` 走 `_apply_step_edit`，
   写完 `indicator_analysis` 后同步重算这两个字段，否则报告里还是改前那段正文。
5. **两个字段进备份白名单**：不加则重启/重放之后拼出来的报告又把整段 JSON 贴回去。

**被否掉的替代方案**

- *组装时用正则把 JSON 代码段剥掉*：治标——半截 JSON 剥完更难看，而且下次换个模型又回来。
- *把 `indicator_analysis` 直接改成正文*：步骤 9/11 的提示词与 refine 都读它，
  等于顺手改了三个步骤的输入，超出本批范围。
- *拆完就把 JSON 丢掉*：导出、复核、将来的逐指标追溯都要它，只是不该出现在正文里。

## B22.3 验证

**改前红 / 改后绿**：`tests/test_report_no_raw_json.py` 18 条，改前 **15 红 3 绿**
（3 条绿的是「不许退化」护栏：`indicator_analysis` 保持原文、score_map 不受影响、
老备份回退可用）。

**变异**：新增 `_mutate_report_json.py` **11/11 全抓**（拆前不校验合法性、不剥围栏、
不压空行、返回值带围栏、节点不拆、组装退回原文、回退被删、values 路径不用正文、
备份路径不用正文、人工改完不重算、白名单漏字段）。既有 9 个脚本
**28 / 7 / 4 / 6 / 8 / 11 / 13 / 13 / 12 全抓**，无锚点失效。

**端到端**（真实工作流，新会话 `d08be1e4`，32 个叶子）：

| 项 | 结果 |
|---|---|
| 步骤 7 原始输出 | 4,003 字，**含代码块围栏** |
| `step7_prose` | 608 字，无围栏 |
| `step7_scores_json` | 3,381 字，`json.loads` 可解析 |
| 报告 | 27,657 字，**0 个代码块、0 处 `"scores"`**（上一轮 `73604287`：33,423 字 / 3,738 字 JSON = 11.2%） |
| 第四章实际内容 | 一级指标得分汇总表（决策 8.50 / 过程 12.00 / 产出 12.00 / 效益 0.00 / 合计 32.50）+ 整体分析 + 未取得佐证清单 |
| 导出 Word | 61,354 字节，正文 XML 里**无代码围栏、无 `"scores"`、无 `actual_score`** |
| wf_e2e | **47 项 / 0 失败** |

两轮报告字数不同（33,423 → 27,657）不能直接比：材料同为 354 字样例，但模型每次产出的
正文长度本来就不同；能比的是「JSON 在不在正文里」。

**回归**：`pytest 410 passed`（392 → 410）、`node 42 pass`、`test_all_apis 46/46`、
`frontend_e2e_check 72/0`、`wf_e2e 47/0`。

## B22.4 回滚

- 组装回退：把 `_chapter_text` / `_chapter_from_values` / 备份组装那三处改回直取原字段，
  立刻回到旧行为（JSON 回到报告），不需要动数据。
- 代码：`git checkout -- backend/agents/performance_agent.py
  backend/agents/tools/structured_output.py backend/agents/states/performance_state.py
  backend/routes/agent_routes.py`。
- 两个新字段是纯增量：留着不影响任何读方，删了也不影响老会话。

## B22.5 剩余风险与未验证项（诚实列出）

1. **历史报告不回洗。** 27 份已组装的报告存在 DB 的 `final_report` 里，本批不改写；
   要清掉得重跑工作流或单独写迁移脚本（未做，也没做「哪些该重跑」的评估）。
2. **只剥第一个 JSON 块。** 模型若输出两个 JSON 代码块（比如评分一份、汇总一份），
   第二份会留在正文里。实测没出现，但没有专门防。
3. **`step7_prose` 为空时回退原文。** 如果模型只给 JSON、不写正文，第四章还是会出现 JSON
   （等于老行为）。这轮实测正文 608 字，长材料下模型「只给 JSON」的概率未测。
4. **`_apply_step_edit` 是唯一重算入口。** 若将来新增别的写 `indicator_analysis` 的路径
   （比如批量补分），必须同步调 `split_step7_output`，否则报告会显示旧正文。
5. **单步骤导出（`export-step`）不受影响也不受保护**：它导的是步骤原文（含 JSON），
   这是设计如此——那是给人看「模型到底写了什么」的地方。

## B23.1 起点：启动/关闭还在用废弃的 `on_event`，后台任务没人拿住也没人停

**① 两处 `@app.on_event`**（`main.py:133` / `main.py:245`）：`startup_event` 干活，
`shutdown_event` 只打一行日志。FastAPI 早已弃用这套写法，全量回归里 **368 条 warning**
就有一部分来自它。

**② 清理任务 `asyncio.create_task(_cleanup_loop())` 没有引用**

`asyncio` 只对任务持弱引用，事件循环之外没人拿住就有被 GC 的风险；更实际的问题是
**关闭时没人 cancel 它**——进程退出靠操作系统收尸，正在写的文件/库连接没有收尾机会。

**③ `shutdown_event` 里没有任何实质动作**

```
@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    logger.info("应用关闭...")
```

**④ 顺带排查**：`backend/` 里 `asyncio.create_task` 只有这一处，没有别处裸调。

## B23.2 做法：换成 lifespan，把任务拿住并在关闭时等它收尾

1. **`@asynccontextmanager` 的 `lifespan`** 替代两处 `on_event`：
   `await _on_startup()` → `yield` → **`finally: await _on_shutdown()`**。
   用 `finally` 而不是顺序语句：正常退出、异常退出、被取消，都要收干净。
2. **启动/关闭各抽成一个函数**（`_on_startup` / `_on_shutdown`）：内容与改前**逐行一致**
   （配置自检 → 建库建表 → 默认管理员 → 默认模板 → 清理定时器；DB 失败只记日志、照常起服务），
   抽出来才好在测试里直接调。
3. **任务存进模块级 `_CLEANUP_TASK`**，并给任务起名 `file-cleanup`（排查时一眼看出是谁）。
4. **`_start_cleanup_scheduler()` 幂等**：已有未结束的任务就返回同一个，不会起第二条循环。
5. **`_stop_cleanup_scheduler()` 先 cancel 再 `await`**：只 cancel 不 await 等于把清理逻辑
   留在半路（它可能正在写文件或写库）；等它退出后再返回，并清掉全局引用。
6. **关闭阶段的异常不往上抛**：`_on_shutdown` 里 try/except 包住，进程退出不该被清理任务卡住。

**被否掉的替代方案**

- *只把 `on_event` 换成 `lifespan`、任务照旧不持引用*：deprecation 消失了，但「关闭时任务
  被砍在半路」这个真问题还在——这正是本批要修的。
- *在 `shutdown_event` 里用 `asyncio.all_tasks()` 遍历取消*：会连请求处理任务一起砍，
  而且拿不到「哪个是清理任务」。
- *给任务加超时强杀*：清理逻辑没有幂等保护，砍在一半比不砍更糟；先保证「等它自己退」。

## B23.3 验证

**改前红 / 改后绿**：`tests/test_lifespan.py` 14 条，改前 **14 条全红**（`_on_startup` /
`_on_shutdown` / `_CLEANUP_TASK` / `_stop_cleanup_scheduler` 都不存在）。

**变异**：新增 `_mutate_lifespan.py` **12/12 全抓**（lifespan 不跑启动、退出不跑关闭、
用 except 代替 finally、应用不挂 lifespan、任务不持引用、启动不去重、任务不命名、
关闭只 cancel 不 await、关闭不清引用、关闭异常不兜、DB 起不来也起任务、启动不建表）。
既有 10 个脚本 **28 / 7 / 4 / 6 / 8 / 11 / 13 / 13 / 12 / 11 全抓**，无锚点失效。

**真实 uvicorn 优雅关闭**（`uvicorn.Server` + `should_exit`，端口 8011）：

| 项 | 结果 |
|---|---|
| 启动日志顺序 | 启动中 → 数据库初始化成功 → 管理员账号检查完成 → 默认Prompt模板已同步 → 清理定时器已启动 → `Application startup complete` |
| 关闭日志顺序 | 应用关闭... → 文件保留清理定时器已停止 |
| 退出后 `asyncio.all_tasks()` | **`[]`**（没有残留任务） |

**端到端**：`test_all_apis` 46/46、`frontend_e2e_check` 72/0、`wf_e2e` **47 项 / 0 失败**。

**回归**：`pytest 424 passed`（410 → 424）、`node 42 pass`；
warning 总数 **368 → 364**（`on_event` 那几条弃用告警不再出现，`grep on_event` 为 0）。

## B23.4 回滚

- 代码：`git checkout -- backend/main.py`（本批只动这一个文件）。
- 若只想临时恢复旧行为：把 `app = FastAPI(..., lifespan=lifespan)` 的 `lifespan=` 去掉，
  启动/关闭都不会跑——注意这不是"回到 on_event"，是"什么都不跑"，仅用于排查。
- 新增的两个函数（`_start_cleanup_scheduler` / `_stop_cleanup_scheduler`）是内部接口，
  没有对外契约变化。

## B23.5 剩余风险与未验证项（诚实列出）

1. **Windows 上进程级终止不会走关闭流程。** `taskkill /F` 直接杀进程，lifespan 的 finally
   不会执行——这是操作系统行为，不是本批引入。本批验证的是「收到优雅关闭信号时能收干净」
   （`uvicorn` 的 `should_exit` 与 TestClient 上下文的进出）。
2. **多 worker 下每个进程都会跑一条清理循环。** `MemorySaver` 与清理任务都还是单 worker 假设
   （与 C2 同源）；真上多 worker 要先解决这个，否则同一时刻会有 N 个清理任务并发写库。
3. **`_stop_cleanup_scheduler` 吞掉 `CancelledError`。** 若外层（uvicorn）此时正在取消，
   最坏情况会延迟一点关闭；清理循环本身的取消点是 `asyncio.sleep`，实测很快返回。
4. **启动失败仍照常起服务。** DB 不通时只记 error、不起清理任务、其余接口照旧 500——
   这是**沿用改前行为**，本批没有改，也不打算在这一批改。
5. **只覆盖了 `main.py` 里的任务。** 已确认 `backend/` 里只有这一处 `asyncio.create_task`；
   将来新增后台任务时，同样要「持引用 + 关闭时 await」，没有框架级约束。

## B24.1 起点：四件事里有两件是「现在就能被人用」的洞

**① `admin` 至今仍在用源码里写死的口令**

`create_admin_if_not_exists()` 硬编码 `Admin@123456`，还把这个口令打进日志。本机库里实测：

```
=== 默认密码命中检查 (Admin@123456) ===
  DEFAULT PWD -> 1 admin
```

也就是说，任何看过这个仓库的人都能直接登进管理员后台。这不是"理论上可能"，是**现在成立**。

**② 管理员详情接口把口令哈希一起返回**

```python
"password_hash": user.password_hash,
```

`admin_routes.py:230`。哈希本身是可离线爆破的凭据材料（salt 也在里面），而前端任何位置都用不到它。

**③ 第三方 API Key 明文落库，列表接口还把前 8 位交出去**

```python
api_key=req.api_key,                                   # admin_create_model，明文入库
model.api_key = req.api_key                             # admin_update_model，明文更新
"api_key": m.api_key[:8] + "***" if m.api_key else None  # admin_list_models，泄露前缀
```

多数厂商 Key 的前缀是固定的（`sk-`、`sk-proj-` 之类），前 8 位的遮蔽作用接近于零。
一次 `mysqldump`、一份误传的备份，密钥就全泄了。

**④ 备份文件差点进仓库**

本批要改库结构，按规矩先 `mysqldump` 备份到 `.backup/`。备份完顺手 `git check-ignore`：

```
$ git check-ignore -v .backup/pre_b24_ai_grade_db.sql
NOT IGNORED
```

`.gitignore` 里有 `state_backups/`、`backend/data/`，**唯独没有 `.backup/` 和 `*.sql`**。
下一步如果执行 `git add -A`，这个 2.7MB、含真实密钥与口令哈希的 dump 就会推到 GitHub。
已补规则；同时确认历史干净：`git log --all -- '*.sql' '*.dump'` 为空，
`git rev-list --all --objects | grep -iE "\.sql$|\.dump$"` 为空，`git log -p -- .env` 为空。

**⑤ 顺带查出来的潜伏 bug**

`create_admin_if_not_exists()` 的替代分支：

```python
password_hash=hash_password("Admin@123456"),   # 返回 (hash, salt) 元组
```

`hash_password` 返回 `(hashed, salt)`，这里直接把**元组**塞进 `String(255)` 列。
一旦走到这条分支，那个账号以后每次登录都会因为 `":" not in stored_value` 失败。
（当前走不到：`Admin@123456` 满足密码复杂度，永远走分支一。但它是坏的。）

**⑥ 取证：这个字段其实没有运行时消费点**

全仓 grep `api_key` 后确认：`ChatOpenAI(...)` 的 5 处入参全部来自 `settings.DEEPSEEK_API_KEY`
或 `llm_helper.get_model_credentials()`（读环境变量）。`model_configs.api_key` 除 admin 的
增删改查外**没有任何读取方**。所以加密改造不会影响推理链路，风险边界很清晰——
这也意味着不能靠"跑一次评价看看正不正常"来验证它，只能靠单元测试和库里的字节。

## B24.2 做法：写时加密、读时兼容；初始口令一律拦到改完为止

**1. `backend/services/secret_box.py`（新增，约 170 行）**

- 写：`encrypt_secret(plain)` → `"enc:v1:<fernet-token>"`。带版本前缀，将来换算法可按前缀分流。
- 读：`decrypt_secret(stored)`。**没有前缀就原样返回**——历史明文不需要停机迁移，
  属于"写时加密、读时兼容"，老数据下一次被编辑时自然变成密文。
- 幂等：已经是密文就原样返回，重复保存不会二次加密。
- 空值 → `None`（不要留一个"加密后的空串"，否则 `has_api_key` 永远为真）。
- 解不开 → 返回 `""` 而不是抛异常。这个值最终要喂给 LLM 客户端，抛异常会把
  "密钥配错了"升级成"整个配置页 500"；返回空串还能落到既有的"未配置 API Key"提示分支。
- 密钥来源 `SECRET_ENCRYPTION_KEY`，**允许填任意口令**（内部 SHA256 派生 32 字节再 base64），
  不被 `Fernet.generate_key()` 的 44 字符格式绑住；同一口令永远派生同一把钥匙，重启不会解不开。
- 没配密钥：**降级明文 + 告警一次**（`_warned_plaintext` 做一次性）。安全加固不该让一个
  跑着的系统起不来，但也不能让人以为它生效了。

**2. 掩码不看原文**

```python
def mask_secret(value):
    return SECRET_MASK if value else None     # 固定 "********"
```

不保留任何片段，也不随长度变化（长度本身也是线索）。

**3. `admin_routes` 接入**

| 位置 | 改前 | 改后 |
|---|---|---|
| `admin_list_models` | `m.api_key[:8] + "***"` | `mask_secret(m.api_key)` + `has_api_key: bool` |
| `admin_create_model` | `api_key=req.api_key` | `api_key=encrypt_secret(req.api_key)` |
| `admin_update_model` | `model.api_key = req.api_key` | 判掩码后 `encrypt_secret(...)` |
| `admin_get_user` | 返回 `password_hash` | **删掉该字段**，改为返回 `must_change_password` |

`admin_update_model` 的掩码判断是必要的：前端把列表里的 `********` 原样回传，
不判就会拿它覆盖掉真正的密文。

**4. 首登强制改密**

- **加列**：`User.must_change_password`，`nullable=False, server_default="0"`。
  必须 NOT NULL 且有默认值，否则 ALTER 加列时老行是 NULL，判定逻辑拿不到确定的布尔值。
- **幂等迁移**：项目没有 Alembic，`create_all()` 只建不存在的表、**不给已存在的表加列**。
  新增 `backend/utils/schema_patches.py`：先查 `information_schema.COLUMNS` 再决定是否
  `ALTER TABLE`；不用 `ADD COLUMN IF NOT EXISTS`（MySQL 8 不支持）。挂在 `init_db()` 里，
  每次启动跑一遍，重复执行无副作用。
- **精准打标，不一刀切**：`mark_default_password_users()` 对每个用户**逐个试算**已知初始口令
  （`KNOWN_DEFAULT_PASSWORDS`），只有真的还在用初始口令的才置位。
  一刀切会把"自己改过密码的人"也锁住，那是误伤。
- **置位时机**：默认管理员创建时、管理员建号时、管理员重置密码后。
  自助注册**不**置位（密码是本人设的）。
- **拦截点放在 `get_current_user`**：135 个端点共用这一个依赖，改一处覆盖全部。
  白名单只放 5 条：改密接口本身、`/api/auth/me`、login/logout/refresh。
  未改密用户打这些之外的接口一律 403。

**5. 403 必须能被前端区分开（否则是死循环）**

`api.js` 原来把 401 和 403 **一并**当 `code: 'auth'`：先尝试 refresh，refresh 也失败就
`onAuthFailure()` → `clearAuth()` + 跳登录页。如果"要改密"也用 403 且不区分，
用户会被反复踢回登录页，而登录后第一个请求又 403 —— **永远改不了密，也永远进不去**。

做法：403 响应带 `X-Password-Change-Required: 1`（CORS `expose_headers` 里也加上），
前端在 `_send` 里**先于** auth 分支判断这个头，命中则抛
`code: 'password_change_required'`、**不清 token**、跳 `#settings`。
detail 保持字符串不动（改成 dict 会波及所有既有断言的响应形状）。

登录响应与 `/api/auth/me` 都加 `must_change_password`，登录页据此直接送去改密面板，
而不是让用户挨个点出 403。

**6. E2E 脚本的凭据隔离**

`test_all_apis.py` / `frontend_e2e_check.py` / `wf_e2e.py` 原本都硬编码 `admin/Admin@123456`。
admin 被打标后它们会整片 403 —— 但那是安全策略在工作，不是回归。
新增 `_e2e_auth.py`：给自动化测试**单独**建一个 `e2e_admin`（随机强口令、
`must_change_password=False`），凭据写 `.e2e_credentials.json`（已 gitignore），
首次生成、之后复用、登录失败就重设。**不动 admin 的强制改密状态，也不硬编码任何口令。**

**被否掉的替代方案**

- *只把明文换成哈希*：密钥要还原才能用，哈希不可逆，语义不对。
- *给强制改密开一个环境变量豁免开关*：一个变量就能绕过整个护栏，等于自己开后门。
- *让 E2E 脚本走"改密 → 记住新口令"*：跑一次就把 admin 口令改掉，下次还得再猜——
  而且把"两个已知口令"变成新的暴露面。
- *把 `detail` 改成 `{"code": ...}`*：所有既有前端提示与测试断言都从 `payload.detail`
  取字符串，改形状的波及面远大于加一个响应头。

## B24.3 验证

**改前红 / 改后绿**：新增两个测试文件，共 **52 条**。

- `tests/test_secret_storage.py` **27 条**：加密往返、版本前缀、幂等、空值、
  历史明文兼容、无密钥降级、只告警一次、换钥匙返回空、坏密文兜底、任意口令派生、
  掩码不泄露任何字符、掩码不随长度变化、路由写加密/读掩码/不识别掩码覆盖、
  `admin_get_user` 不回显哈希。
- `tests/test_force_password_change.py` **25 条**：列约束（NOT NULL + server_default）、
  补丁幂等、默认管理员置位（走真实创建流程）、口令不进日志、替代分支拼 `salt:hash`、
  精准打标（改过密码的人不误伤）、白名单、403 标记头、禁用优先于改密、
  改密清标志、登录/`/me` 响应带标志、管理员重置置位、注册不置位。

**真实库迁移**（`.backup/pre_b24_ai_grade_db.sql` 已先备份）：

```
列定义: [('must_change_password', 'tinyint(1)', 'NO', '0')]
被打标用户:
  (1, 'admin', 'ADMIN', 1)
```

64 个用户里**只有 admin** 被打标，其余 63 个（E2E 残留账号）全部 `0`，没有误伤。

**端到端闭环**（真实 HTTP，测完原样还原现场）：

| 步骤 | 结果 |
|---|---|
| `POST /api/auth/login` admin/Admin@123456 | 200，`must_change_password: true` |
| `GET /api/admin/dashboard`（未改密） | **403**，`X-Password-Change-Required: 1`，`首次登录请先修改初始密码` |
| `GET /api/auth/me`（未改密） | 200（白名单），`flag = true` |
| `PUT /api/client/password` 改密 | 200 |
| 新口令登录 | `must_change_password: false` |
| `GET /api/admin/dashboard`（已改密） | **200** |

**安全回归进 E2E**：`frontend_e2e_check.py` 新增 3 条断言，把本次护栏纳入日常回归——
"初始口令登录 → 标记必须改密""未改密的管理端请求 → 403""403 带标记头"。

**变异**：新增两个脚本，**18/18 + 20/20 全抓**。

- `_mutate_secret_storage.py` 18 个：掩码退回前 8 位、掩码泄露长度、写库不加密、
  去掉幂等、空值存空串、历史明文不兼容、静默降级、无钥匙直接报错、解不开抛异常、
  坏密文不兜底、任意口令不派生、`is_configured` 永远真、明文入库（create/update）、
  掩码覆盖、列表返回原值、`has_api_key` 缺失、回显哈希。
- `_mutate_password_change.py` 20 个：不拦截、白名单全放行、白名单前缀过宽、
  403 无标记头、先判改密后判禁用、改密不清标志、管理员不置位、口令进日志、
  元组塞列、一刀切打标、跳过已置位、重置不置位、建号不置位、注册误伤、
  登录不带标志、列可 NULL、无 server_default、补丁不查存在、前端不识别头、被迫改密清 token。

前端两个变异体用 `node --test tests/test_shared_frontend.cjs` 跑 —— 那两条用例是
**真的加载 `api.js`、喂一个 403 响应、看它清不清 token**，不是 grep 源码。

**回归**：`pytest 424 → 476 passed`、`node 42 → 45 pass`；
`test_all_apis` **46/46**、`frontend_e2e_check` **75/0**（原 72 + 新增 3 条安全断言）、
`wf_e2e` **47 项 / 0 失败**；warning 总数保持 364。

## B24.4 回滚

- **加密层**：`git revert` 本批即可。因为 `decrypt_secret` 对无前缀值原样返回，
  **回滚不会让已加密的数据变成乱码**——旧代码读到 `enc:v1:...` 会当普通字符串返回，
  结果是"密钥看起来是密文、调用会失败"。所以要回滚，先把 `api_key` 手工清空重填。
- **改密列**：`users.must_change_password` 是新增列，回滚代码后残留这一列**无害**
  （旧代码不读它）。不需要 `DROP COLUMN`；真要删就
  `ALTER TABLE users DROP COLUMN must_change_password`。
- **数据库**：`mysql ... ai_grade_db < .backup/pre_b24_ai_grade_db.sql`（迁移前全量备份）。
- **E2E**：`_e2e_auth.py` 与 `.e2e_credentials.json` 都是新增文件，删掉即回到硬编码口径；
  但那时 admin 已被强制改密拦着，脚本会红——这是预期。

## B24.5 剩余风险与未验证项（诚实列出）

1. **`SECRET_ENCRYPTION_KEY` 目前没配。** 本机 `.env` 里为空，所以启动日志会告警、
   新写入的 Key 仍是明文。这是**故意**的：先让机制就位、把告警打出来，
   配不配钥匙是部署侧的决定。要紧的是别再让人以为它已经生效。
2. **没有历史密文需要迁移。** 实测 `model_configs` 只有 1 行 `system_config`（`api_key` 为 `NULL`），
   全库没有明文密钥。所以"老明文平滑迁移"这条路径**只有单元测试覆盖**，
   没有真实数据验证过。这是它当前最大的不确定性。
3. **换钥匙等于数据报废。** `decrypt_secret` 在解不开时返回 `""`，用户会看到"密钥变成未配置"，
   需要重新填一遍。没有做密钥轮换（多把钥匙按 `key_id` 尝试）——那是另一件事。
4. **强制改密只拦 HTTP 层。** 直接连库、或用 `_e2e_auth.py` 那种"绕过 HTTP 直接写库"的路径
   都能避开它。它拦的是"拿默认口令走进来的人"，不是"已经有库权限的人"。
5. **`admin` 现在仍是默认口令 + 已打标。** 也就是说它**能被登录、但登录后什么都做不了**。
   这是当前状态的准确描述：护栏生效了，口令本身还没换。收尾动作是管理员自己登录改一次。
6. **`.e2e_credentials.json` 里是一个真管理员口令。** 已 gitignore，但它在磁盘上是明文。
   共享机器/打包分发时要记得删。没有做加密存储——那会让"脚本自愈"变复杂。
7. **`mask_secret` 丢掉了"密钥是不是配错了"的信号。** 以前能从前 8 位看出填的是哪家 Key 的格式，
   现在只能看到一个常量。要判断"配没配"看 `has_api_key`，"配得对不对"只能靠实际调用报错。
8. **`init_db()` 里做 ALTER，多进程同时启动有竞争。** 两个 worker 同时起来可能都判定
   "列不存在"然后都执行 ALTER，后者报 `Duplicate column`——已被 `apply_schema_patches`
   的 try/except 吞掉并记 error，不会拦住启动，但日志里会有一条噪音。
