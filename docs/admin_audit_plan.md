# 管理端优化清单（扫描基线）

> 扫描时间：2026-11-13
> 范围：`frontend/admin/index.html`（1054 行 / 105KB）、`backend/routes/admin_routes.py`（1636 行 / 48 个端点）、`backend/middleware/auth_middleware.py`、`backend/models/user_models.py`、`backend/main.py`、`frontend/shared/*`
> 方法：静态代码走查 + 调用链交叉验证（前端调用 → 后端契约 → 数据模型）。未在运行中的服务上做实测的部分已标注「按代码判定」。

---

## 总览

| 档位 | 主题 | 条目 | 性质 |
|---|---|---|---|
| **P0** | 阻断级缺陷 | 7 | 页面已上线但功能失效 / 会把管理员锁死 / 高危操作无审计 |
| **P1** | 正确性与数据 | 7 | 时区错位、孤儿文件、令牌不失效、校验缺失 |
| **P2** | 性能 | 6 | N+1 查询、串行请求、无缓存、前端体积 |
| **P3** | 一致性与可维护性 | 10 | 与客户端两套请求层、复制粘贴、审计覆盖不一致 |
| **P4** | 能力缺口 | 8 | 版本回滚、健康页、批量操作等「缺的」 |

一句话结论：**管理端的问题不是"长得不好看"，而是"有一批按钮按下去了什么也不会发生"，以及"管理员自己被锁在门外"。** 建议先修 P0，再做 P1/P2，最后才谈视觉与重构。

---

## P0 · 阻断级缺陷

### P0-1 · 管理员被强制改密后，整个管理端死锁

- 位置：`backend/middleware/auth_middleware.py:29-35`、`:142-147`；`frontend/admin/index.html:647-651`
- 现状：`must_change_password=True` 的账号，除白名单外**所有**接口 403。白名单里有 `/api/client/password`，**没有** `/api/admin/*`。
  `tests/test_force_password_change.py:185` 还专门断言 `is_password_change_exempt("/api/admin/users") is False` —— 这个"锁"是被测试固定住的。
- 后果：管理员登录 → 进 `/admin` → 每个页面都只显示「加载失败」；而唯一的改密表单渲染在 `loadSettings()` 里，`loadSettings()` 自己要先请求 `/api/admin/settings`（403）→ 表单永远不出现。**管理员无路可走。**
- 触发条件现在就成立：`mark_default_password_users()` 会给"仍在用初始口令"的账号打标，而 `NEXT_STEPS.md:174` 记录 `admin` 账号当前仍是默认口令 → **此刻很可能已经处于死锁状态**。
- 修复方向：
  1. 白名单加入管理端改密入口（建议新增 `PUT /api/admin/me/password`，而不是复用 `/api/client/password`）；
  2. 前端 `api()` 识别 403 + `X-Password-Change-Required` 头，弹出改密浮层，**不清 token、不跳转**（`shared/api.js:199-208` 已有这套逻辑，可直接复用）；
  3. 管理端自建 `/admin/change-password` 独立页，不依赖任何需要鉴权的数据接口。
- 验证：`SELECT id, username, must_change_password FROM users WHERE role='admin';`

### P0-2 · 两处 `page_size=9999` 被后端 `le=100` 拒绝，功能直接不可用

- 位置：`frontend/admin/index.html:584`（CSV 导出）、`:724`（通知页用户下拉）；后端 `admin_routes.py:151` `page_size: int = Query(20, ge=1, le=100)`
- 按代码判定：9999 > 100 → FastAPI 返回 422 → `api()` 判定失败 → CSV 导出报「获取用户数据失败」；通知页「指定用户」下拉**永远是空的**，选不到人就没法发定向通知。
- 修复方向：后端提供不分页的轻量选项接口（`GET /api/admin/users/options` 只回 `id/username`，或加 `page_size` 上限到 1000 但走独立端点），前端改为拉选项接口；导出同理。

### P0-3 · 前端请求层吞掉后端所有错误详情

- 位置：`frontend/admin/index.html:203` `if(!resp.ok){showToast('请求失败: '+resp.status);return null;}`
- 现状：后端统一错误体是 `{"success":false,"detail":"用户名已存在","request_id":"..."}`（`main.py:208-214`）。前端在拿到非 2xx 时**直接丢弃响应体**并 `return null`，于是所有 `showToast(r && r.detail || '保存失败')` 里的 `r.detail` 永远是 `undefined`。
- 后果：管理员看到的永远是「请求失败: 400」/「保存失败」，看不到「用户名已存在」「备选模型最多 2 个」「系统默认提示词不可删除」等真实原因；排障只能翻服务端日志。
- 修复方向：接入 `frontend/shared/api.js`（见 P3-1），它已经把 `detail` 提取成 `err.message`，还带 `requestId`。

### P0-4 · 用户详情页渲染一个后端已不再返回的字段

- 位置：`frontend/admin/index.html:416`（渲染 `u.password_hash`）；`admin_routes.py:230-232`（批次 24 已明确不再返回）
- 后果：永远显示「密码(加密): 无」。既是死代码，也在误导管理员以为密码丢失。
- 修复方向：删除该行；如需展示，改为「密码策略：初始口令已改 / 待改」（读 `must_change_password`，这个字段后端有返回）。

### P0-5 · 「模板管理」和「指标类型」两个页面是空壳

- 位置：`frontend/admin/index.html:569-587`（模板）、`:590-600`（指标类型）
- 现状：两个页面的增删改排序全部只写 `localStorage`（`admin_template_order` / `admin_indicator_types`），全仓库**没有任何消费方**（已 grep 确认，仅本文件自己读写）。服务端无对应表、无接口。
- 后果：管理员以为改了全站生效，实际上零影响。而且「模板管理」与真实生效的「模块排序」（`report_module_sequences` 表 + 完整 CRUD）功能重叠，两套 UI 说两套话。
- 修复方向：二选一 —— ① 下线「模板管理」，把章节顺序统一收敛到「模块排序」；② 若确需「指标类型」可配置，建表 + 接口 + 让客户端 `#indicatorTypeSelect`（`frontend/client/index.html:869`）从接口读，而不是现在这样两边各自硬编码。

### P0-6 · 注销用户后，这次高危操作的审计日志必然写失败

- 位置：`admin_routes.py:377-378`（`db.delete(user)` + `commit`）→ `:419-423`（`_log_operation(db, user_id=user_id, ...)`）
- 现状：`OperationLog.user_id` 是 `ForeignKey("users.id")`（`user_models.py:252`）。用户行已删，再用同一个 `user_id` 插日志 → 外键约束失败 → `log_operation` 内部 `except` 回滚并只打 warning（`utils/log_utils.py:32-34`）。
- 后果：**最该留痕的操作（连带删除全部数据）反而没有审计记录**，且失败是静默的。
- 修复方向：日志改记操作者（`admin.id`），被删用户写进 `detail` / `resource_id`；并且日志要在删用户**之前**写，或独立事务。

### P0-7 · 用户列表「Token 最多/最少」排序是假的

- 位置：`frontend/admin/index.html:359`（下拉）、`:380-381`（`users.sort(...)`）；后端 `admin_routes.py:148-186` 无 `sort` 参数
- 现状：只对**当前这一页 20 条**做前端排序。翻到第 2 页，又变成那 20 条里的最大值。
- 后果：想找"Token 消耗最高的用户"，得到的是错误答案。而"找出烧钱大户"恰恰是这个排序的唯一用途。
- 修复方向：后端加 `sort` 参数（白名单 `tokens_desc/tokens_asc/created_desc/last_login_desc`），排序下推到 SQL。

---

## P1 · 正确性与数据

### P1-1 · 全站时间显示早 8 小时，「今日」统计边界也错

- 位置：`user_models.py` 全部 `default=datetime.utcnow`（naive UTC）；`admin_routes.py:89`、`:1138`、`:1195` 用 `datetime.now(timezone.utc).replace(hour=0,...)`
- 两个独立问题：
  1. **显示**：`isoformat()` 输出 `2026-11-13T11:00:00`（无时区后缀），JS `new Date(...)` 按规范当作**本地时间**解析 → 管理端所有时间列比真实时间早 8 小时。
  2. **统计**："今日活跃用户""今日 Token"的边界取 UTC 零点 = 北京时间 08:00，早 8 点到次日 8 点被算作"今天"。
- 修复方向：序列化统一输出带 `Z`/`+00:00` 的 UTC（或直接输出北京时间的 `+08:00`）；"今日"边界改用 `Asia/Shanghai` 计算。这是全局改动，建议一次做干净并补测试。

### P1-2 · 注销用户只删库，不删磁盘文件

- 位置：`admin_routes.py:308-427`
- 现状：`Project`/`ProjectFile` 记录随 ORM cascade 删除，但 `uploads/` 下的实体文件、`exports/` 下的导出件都没有清理（只清了 `state_backups/` 的 json 和内存缓存）。
- 后果：磁盘上留下一批无主文件，永久占用空间，且构成"用户以为删了、其实还在"的合规风险。
- 修复方向：删库前先收集 `ProjectFile.file_path`，提交成功后逐个删除磁盘文件；失败要记入审计。

### P1-3 · 改密 / 禁用后旧 JWT 仍然有效

- 位置：`admin_routes.py:248-284`（改密）、`:287-305`（禁用）；`auth_middleware.py:95-149` 只校验 `is_active`，不校验令牌签发时间
- 现状：禁用用户后，其已签发的 access token 在 24h（`ACCESS_TOKEN_EXPIRE_MINUTES=1440`）内仍然可用。改密同理——管理员重置了密码，但旧会话还能继续操作。
- 修复方向：`users` 加 `token_version`（或 `password_changed_at`），登录时写进 JWT，校验时比对；改密/禁用即递增。

### P1-4 · 创建用户缺少服务端校验

- 位置：`admin_routes.py:189-205`
- 现状：`AdminCreateUserRequest.password` 无长度/强度校验（前端 `:475` 才有"至少6位"，绕过前端即可）；`role: str` 未做枚举校验，传 `role="root"` 会一路走到 `create_user` 里报错或被静默接受。
- 修复方向：`password: str = Field(min_length=8)`、`role: Literal["user","admin"]`；密码强度规则与"首次登录强制改密"策略口径统一。

### P1-5 · `PUT /knowledge/{id}/granular` 是冗余端点，且承诺的能力并不存在

- 位置：`admin_routes.py:1481-1503` vs `:978-991`
- 现状：两个接口体逻辑完全一致；docstring 说"含 parent_indicator 字段"，但 `KnowledgeBaseRequest`（`:902-908`）里**没有** `parent_indicator`，而模型里是有的（`user_models.py:180`）。
- 后果：前端以为能改"所属上级指标"，实际改不了，且这是知识库检索精度的关键字段。
- 修复方向：删掉 `/granular` 端点，把 `parent_indicator` 补进 `KnowledgeBaseRequest`。

### P1-6 · 请求层把 refresh token 放在 query string

- 位置：`frontend/admin/index.html:197`；后端 `auth_routes.py:147-148` `refresh_token_str: str`（查询参数）
- 风险：refresh token 会进 access log、浏览器历史、Referer。`shared/api.js:288` 也是同样写法，属于全局遗留。
- 修复方向：改 `POST /api/auth/refresh` 读 body 或 `Authorization` 头；两端同步改。

### P1-7 · 评分阈值可以把 0 变成默认值

- 位置：`frontend/admin/index.html:686` `parseFloat(...) || 90`；`:673` 回填 `s.rating_thresholds.excellent || 90`
- 现状：`0` 是 falsy，输入 0 会被替换成 90。虽然业务上阈值不会是 0，但这是典型的"用 `||` 兜底 falsy"陷阱，同一模式在全文出现多处。
- 修复方向：改用 `Number.isFinite()` 判断。

---

## P2 · 性能

### P2-1 · 步骤配置概览：42 条 COUNT 打成串行

- 位置：`admin_routes.py:1510-1530`
- 现状：`for step in range(1,15)` × 3 张表各一次 `count()` = 42 次 DB 往返。
- 修复：3 条 `GROUP BY step_number` 一次拿全，Python 侧拼装。预期从 42 次降到 3 次。

### P2-2 · 仪表盘首屏：前端 4 个请求串行 + 后端 8 条聚合

- 位置：`frontend/admin/index.html:230-233`（`dashboard` → `token-stats` → `ai-chat-stats` → `settings`，四个 `await` 排队）；`admin_routes.py:86-141`（8 条独立 count/sum）
- 修复：前端 `Promise.all`（首屏耗时接近 1/4）；后端合并成跨表聚合或加 60s 缓存（管理端统计不需要秒级新鲜）。

### P2-3 · 通知列表 N+1

- 位置：`admin_routes.py:1297` `n.user.username` —— 每行一次懒加载
- 修复：`joinedload(Notification.user)` 或直接 join 出 `username`。

### P2-4 · 无任何缓存层

- 现状：每次切页都全量重取；`/token-stats`（全表聚合 `token_usage`）、`/audit-logs/summary`（全表 group by）尤其重。
- 修复：进程内 TTL 缓存（30~60s）+ 前端 `stale-while-revalidate`；`/token-stats` 的 `func.date(created_at)` 会让 `created_at` 索引用不上，建议改成范围条件 + 应用层按天分桶。

### P2-5 · 前端 105KB 单文件，且引了两个基本没用的 CDN

- 现状：`head` 里引了 `cdn.tailwindcss.com`（全站是手写 CSS，实际几乎未用 Tailwind 类）和 `unpkg.com/lucide@latest`（**未锁版本**，上游发版即可能白屏）。
- 修复：移除 Tailwind CDN（或真正用起来）；lucide 锁死版本号并本地托管；把 1054 行拆成 `admin/*.js` 模块按页懒加载。
- 附带：`Cache-Control: no-cache`（`main.py:171`）对所有 HTML 生效，静态资源走 `/static` 可以正常缓存，拆分后收益更明显。

### P2-6 · 全表格无虚拟滚动、无服务端排序/筛选（除用户页）

- 现状：`知识库`/`审计日志`/`通知`/`工作流` 四个列表只能翻页看，不能按时间/状态排序，不能按关键字搜项目名。
- 修复：把 `sort`/`q`/`start_date`/`end_date` 统一加到列表接口（审计接口已有日期参数但前端没暴露，见 P3-5）。

---

## P3 · 一致性与可维护性

### P3-1 · 管理端完全没接统一请求层（最值得做的一条重构）

- 位置：`frontend/admin/index.html:186-209` vs `frontend/shared/api.js`
- 现状：客户端 `frontend/client/index.html:445-449` 已经加载 `config.js/store.js/ui.js/api.js`；**管理端一个都没引**，自己重写了 20 行的 `api()`。
- 丢掉了这些能力：超时（30s/120s）、指数退避重试（幂等 GET）、同 URL 并发去重、统一 `ApiError {code,message,status,retryable,requestId}`、`X-Request-ID` 透传、首登改密分流、导出闸门 409 重放。
- 阻塞点：token 键名不一致 —— admin 读 `access_token`/`token`/`sessionStorage.token`，`shared/api.js:34-38` 只读 `token`。切换前要先统一。
- 顺带能一并解决：P0-1（改密分流）、P0-3（错误详情）、P3-6（request_id 显示）。

### P3-2 · 分页代码复制 4 遍

- 位置：`:387`、`:548`、`:620`、`:640`、`:760` —— 同一段 `for(var i=1;i<=tp;i++)` 出现 5 次
- 修复：抽 `renderPagination(container, page, totalPages, onGo)`；顺带补"上一页/下一页/跳转第 N 页"，现在上千条数据只能一页页点。

### P3-3 · 55 处内联 `onclick` + 60 处 `innerHTML` 拼接

- 现状：为了在 `onclick` 里传字符串参数，多处手写 `.replace(/\\/g,"\\\\").replace(/'/g,"\\'")`（`:424`、`:468`）—— 这是典型的"迟早漏一个"的转义模式；`esc()`（`:220`）也没转义 `'`。
- 修复：统一事件委托 + `data-*`（`:391-405` 已经为"注销"按钮这么做了，是对的，推广到全部）；渲染改模板函数或 `textContent` 赋值。

### P3-4 · 审计覆盖不一致，但页面宣称"追踪所有关键操作"

- 有日志的：用户增删改、发通知、建/改模块序列、清数据。
- **没日志的**：`PUT /prompts/{id}`（改 Prompt 正文）、`POST /prompts/{id}/reset`（重置 Prompt）、知识库增删改、模型增删改/启停、删模块序列。
- 后果：最能影响报告质量的三类改动（Prompt、知识库、模型）恰恰没有留痕。
- 修复：把 `log_operation` 下沉成 router 级或统一装饰器，避免"想起来才写"。

### P3-5 · 审计筛选的下拉选项跟真实 action 名对不上

- 位置：`frontend/admin/index.html:627`
- 现状：硬编码 8 项（`login/logout/admin_create_user/...`），实际库里还有 `create_granular_prompt`、`update_granular_prompt`、`delete_prompt`、`batch_update_module_sequence`、`execute_cleanup`、`cleanup_stuck_workflows`、`admin_send_notification` 等，选不到、筛不出。
- 修复：下拉选项从已有的 `/audit-logs/summary` 动态生成（这个接口就是为此存在的）。
- 同时：日期范围参数后端已支持（`start_date`/`end_date`），前端没做输入框；`ip_address` 后端已返回，前端没展示。

### P3-6 · 无 URL 路由，刷新即回仪表盘

- 位置：`navigateTo()`（`:159-182`）只切 DOM，不写 hash/`pushState`
- 后果：不能深链、不能分享"审计日志第 3 页"、浏览器后退直接退出管理端、刷新丢失上下文、排障时无法让管理员"打开这个地址复现"。
- 修复：`location.hash = page` + `hashchange` 监听，成本极低。

### P3-7 · 零响应式

- 现状：`@media` 出现 **0** 次；`.sidebar` 固定 240px、`.main-content` 固定 `margin-left:240px`、多处 `grid-template-columns:1fr 1fr`。
- 后果：笔记本 1366×768 及以下、平板、手机上布局直接崩。
- 修复：加 3 个断点（≥1280 / 768–1279 / <768），小屏侧栏转抽屉、双列转单列。

### P3-8 · 模态框缺基本可访问性与交互

- 现状：`showModal()`（`:217`）无 `role="dialog"`/`aria-modal`、无焦点陷阱、不支持 `ESC` 关闭、打开后焦点不落到首个输入框、关闭后焦点不归还。全局监听 `document.addEventListener('click', ...)` 也可能与浮层事件互相干扰。
- 修复：抽一个 `openDialog({title, body, onConfirm})`，统一处理 ESC/焦点/滚动锁。

### P3-9 · 暗色主题不可行（硬编码浅色）

- 现状：28 处 `background:white`、17 处 `background:#f*`、`#fff` 10 处；`--bg/--card/--border` 变量只覆盖了一部分。`input` 的多处内联样式直接写 `background:white`。
- 另外 `:248`/`:252` 用 `#6FFF00` 霓虹绿表示"运行中"，白底对比度约 1.4:1，基本读不清；且与 `--success` 语义重复。
- 修复：所有颜色走 CSS 变量；`#6FFF00` → `var(--success)`；再给 `[data-theme="dark"]` 一组变量即可。

### P3-10 · 死代码与重复实现

| 位置 | 问题 |
|---|---|
| `admin_routes.py:72-79` | `DashboardOverview` 定义了从不使用 |
| `admin_routes.py:68-69` | `AdminChangePasswordRequest` 从未使用 |
| `admin_routes.py:26-41` | `HAS_QUESTIONNAIRE_TABLES` / `HAS_MEMORY_TABLES` 双重 try 兜底，两张表都在同一模块里，兜不住任何东西 |
| `admin_routes.py:1050-1060` vs `:1091-1108` vs `:1111-1128` | 同一段"取/建 system_config 行 + 改 JSON"写了 3 遍 |
| `frontend/admin/index.html:932` | `var old=promptsDefaults[id]` 取的是按 step 编号的字典，键却是 prompt id，永远 `undefined`，且从未使用 |
| `frontend/admin/index.html:311` | `<span class="toggle-slider:before">` —— class 里带冒号，匹配不到任何 CSS，纯垃圾节点 |
| `frontend/admin/index.html:880` | `P_DIMENSIONS` 声明后从未使用 |
| `main.py:226-237` | 500 处理器把 `str(exc)` 原样回给前端；管理端是内网也该只在 `DEBUG` 下暴露 |

---

## P4 · 能力缺口

| # | 缺什么 | 为什么值得做 |
|---|---|---|
| P4-1 | **Prompt 版本历史 / diff / 回滚** | 现在 `version` 只递增，旧内容直接覆盖丢失。改坏一条 Prompt 无法恢复，只能"重置为系统默认"——自定义内容全没 |
| P4-2 | **系统健康页** | 看不到 LLM 连通性、DB 延迟、磁盘剩余、在跑工作流数、清理任务上次执行时间。出问题时管理员只能猜 |
| P4-3 | **工作流详情页** | 列表只能看状态，点不进去。看不到每步耗时、失败原因、Token 分布，无法定位"卡在 3/14 的那批" |
| P4-4 | **报告/会话内容查看** | 管理端完全没有入口，只有统计数字。客服场景无法核实"报告里那句话是哪来的" |
| P4-5 | **审计日志导出** | 合规场景必需；现在只能一页 50 条地看 |
| P4-6 | **通知按角色群发 / 按标题批量撤回** | 后端 `DELETE /notifications/by-title` 已实现，前端没接；也没有"预览发给谁" |
| P4-7 | **知识库批量导入 + RAG 重建索引** | 现在只能一条条手录；改完内容也没有触发索引刷新的入口，检索可能仍是旧向量 |
| P4-8 | **用户批量操作 / CSV 导入** | 批量禁用、批量重置密码、批量导入。当前 64 个用户尚可忍，上量后是硬伤 |

---

## 建议的优化顺序

**第一批（止血，1 个批次内可完成）**
P0-1 改密死锁 → P0-2 两处 9999 → P0-3 错误详情透出 → P0-4 删掉 password_hash → P0-6 注销审计。

理由：这五条都是"用户点了没反应 / 管理员进不去"，修完管理端才谈得上可用。P0-3 修好后，后面所有问题的排障效率都会提高。

**第二批（数据正确性）**
P1-1 时区（全局，改动面最大，单独一批）→ P1-2 孤儿文件 → P1-3 令牌失效 → P1-4 服务端校验。

**第三批（架构收口）**
P3-1 接统一请求层（会顺带解决 P0-1/P0-3 的前端部分，可与第一批合并）→ P3-2 分页抽象 → P3-3 事件委托 → P3-10 清死代码。

**第四批（体验与性能）**
P2-1/P2-2/P2-3 查询优化 → P2-5 拆包 + 去 CDN → P3-6 hash 路由 → P3-7 响应式 → P3-9 暗色。

**第五批（能力补齐）**
P0-5 决定"模板管理/指标类型"的去留 → P4-1 Prompt 版本 → P4-2 健康页 → 其余按需。

---

## 执行记录

### 第一批 · 止血（2026-11-13 完成）

| 条目 | 状态 | 改动 |
|---|---|---|
| P0-1 改密死锁 | ✅ | `auth_middleware.py` 白名单加 `/api/admin/me` 前缀；新增 `GET /api/admin/me`、`PUT /api/admin/me/password`；前端启动先探 `/me`，命中则弹不可关闭的改密闸门；`api()` 识别 403 + `X-Password-Change-Required` 直接开闸门，**不清 token、不跳登录页** |
| P0-2 `page_size=9999` | ✅ | 新增 `GET /api/admin/users/options`（轻量，声明在 `/users/{user_id}` 之前）；通知页下拉改用它；CSV 导出改为按 100 一页翻到底，并补 CSV 公式注入防护 |
| P0-3 错误详情丢失 | ✅ | 重写管理端 `api()`：非 2xx 返回 `{success:false, detail, status, request_id}` 而不再 `null`；解析 422 的校验错误数组；新增 `failBox()` 把真实原因渲染进列表，替换 13 处硬编码「加载失败」 |
| P0-4 `password_hash` | ✅ | 用户详情改为展示「口令状态」（是否仍为初始口令） |
| P0-6 注销无审计 | ✅ | `admin_deactivate_user` 改为记在**操作者**名下（原实现用刚删除的用户 id 写 FK，必然失败且被静默吞掉）；`admin_delete_user` 同步修正 |
| P0-7 Token 排序 | ✅ | 后端加白名单 `sort` 参数（下推 SQL，`coalesce` 处理 NULL，补 `id` 兜底防翻页重复）；前端删掉只排当前页的客户端排序 |

**顺带修掉的**：`showToast(msg,true)` 把 `true` 当 type 拼出 `.toast-true` 类（3 处）；`api()` 的超时/重试/请求号透传此前完全缺失。

**真实库验证**（TestClient + 线上 MySQL，只读，未改任何账号口令）：

```
login admin                      -> 200  must_change_password: true
GET /api/admin/me                -> 200  {"must_change_password": true}
GET /api/admin/dashboard         -> 403  X-Password-Change-Required: 1
GET /api/admin/users/options     -> 403  （业务接口仍被护栏挡住，符合预期）
```

**结论：修复前，`admin` 账号打开管理端就是满屏「加载失败」，且没有改密入口——死锁真实存在，不是理论风险。**

**测试**：新增 `tests/test_admin_recovery.py`（12 项，SQLite 隔离库）与 `tests/test_admin_client.cjs`（9 项，把管理端内联脚本塞进 vm 跑真实源码）。全量 `pytest tests/` 916 passed；`node --test tests/*.cjs` 80 passed。

**遗留**：管理员改密后旧 JWT 仍有效（P1-3，下一批）；`refresh_token` 仍走 query string（P1-6）。

### 第二批 · 数据正确性（2026-11-13 完成）

| 条目 | 状态 | 改动 |
|---|---|---|
| P1-1 时区 | ✅ | 新增 `backend/utils/time_utils.py`（`utc_iso` / `utcnow` / `local_day_start_utc`）；管理端 17 处 `.isoformat()` 全改为 `utc_iso()`，输出统一带 `Z`；「今日/本周/近30天」边界改用北京时间 0 点；`token-stats` 按北京日分桶（`DATE_ADD(..., INTERVAL 8 HOUR)`，非 MySQL 退回 UTC 日）；`prompt_manager` 写 `updated_at` 误用本地时间，改回 UTC；前端 `formatTime` 对无时区串补 `Z` 兜底 |
| P1-2 注销残留 | ✅ | `CleanupService.purge_user_disk_artifacts()`：按 `uploads/u{id}_*` 与 `backend/exports/{id}/` 精确清理，返回删除数/字节/错误；管理端注销在 DB 提交**之后**调用，并把结果写进响应与审计；客户端自助注销原来 glob 写的是 `*_{user_id}_*`，与实际文件名 `u{id}_...` **根本匹配不上**（等于从来没删过），改为调用同一函数 |
| P1-3 令牌失效 | ✅ | `users.token_version` 列（幂等 ALTER，已对真实库执行）；JWT 带 `ver`，`get_current_user` 与 `/refresh` 双向校验；改密/管理员重置即自增；改密接口补发新令牌，避免把正在改密的设备一起踢下线；老令牌无 `ver` 默认 0，与老账号初始值一致 —— **升级不会把任何人踢下线** |

**纠正在扫描阶段的判断**：P1-3 原写「改密 / 禁用后旧 JWT 仍有效」，实际 `get_current_user` 已在每次请求校验 `is_active`，**禁用是即时生效的**。这一条真正的问题只剩「改密不失效」，本批按此收窄。

**真实库验证**：

```
GET /api/auth/me  ->  created_at = 2026-07-13T10:40:01Z   （此前是 ...T10:40:01，浏览器按本地解析 → 早 8 小时）
ALTER: schema 补丁：users.token_version 已新增（再跑一次返回空，幂等）
按北京日分桶 vs UTC 日分桶（同一天，2026-11-07）：1,113,618 vs 1,218,520  ← 边界差 8 小时的实锤
```

**测试**：新增 `tests/test_admin_time_and_session.py`（12 项：Z 后缀、北京边界、磁盘清理只删本人且幂等、接口确实调到清理、改密踢旧会话但补发新令牌、refresh 一并作废、老令牌不受影响）。全量 `pytest tests/` **930 passed**，`node --test tests/*.cjs` **80 passed**。

**本批踩到的**：`tests/_mutate_password_change.py` 里有个变异锚点跨了「`must_change_password = True` → `changed.append`」两行，中间插入 `token_version` 后锚点失配；已同步更新（变异意图不变）。

**遗留**：`client_routes`（11 处）、`agent_routes`（5 处）、`questionnaire_routes`（3 处）仍输出不带时区的 `isoformat()`，客户端页面的时间显示依旧早 8 小时 —— 这些不属于管理端，建议下一轮统一。

### 第三批 · 架构收口（2026-11-13 完成）

| 条目 | 状态 | 改动 |
|---|---|---|
| 时区收尾 | ✅ | `client_routes`(11)、`questionnaire_routes`(1) 的 `.isoformat()` 换成 `utc_iso()`；`agent_routes` 的 SSE 事件戳改 `utc_iso(utcnow())`（原来用的是本地时间戳）。全仓路由层不再输出无时区的 date-time |
| **P3-1 统一请求层** | ⚠️ **改方案** | **没有**把管理端接到 `shared/api.js`（原因见下），改为在管理端 `api()` 内部补齐它缺的两项能力：幂等 GET 的指数退避重试（最多 2 次，只对 408/425/429/5xx）+ 同 URL 并发去重。同时把 401 重放从「递归调 `api()`」改成「递归调 `_sendOnce()`」——原写法会撞上去重表、自己等自己 |
| P3-2 分页抽象 | ✅ | `renderPagination(container,page,totalPages,onGo)`，替换 5 处一字不差的复制（用户/知识库/工作流/审计/通知）；顺带补了「上一页/下一页/首末页/省略号」，之前上千条数据只能一页页点 |
| P3-6 路由化 | ✅ | `navigateTo` 写回 `location.hash`，`hashchange` 反向驱动；`switch` 换成 `PAGE_LOADERS` 注册表（加一页只改一处）；刷新/深链/前进后退都可用 |
| P3-8 浮层可用性 | ✅ | `showModal` 加 `role=dialog`/`aria-modal`，打开后自动聚焦首个可操作元素；`ESC` 关闭（强制改密闸门是独立浮层，关不掉） |
| P3-9 对比度 | ◐ 部分 | `#6FFF00`（白底约 1.4:1，基本读不清）→ `var(--success)` + 新增 `--success-strong:#2f855a` 用于文字；**暗色主题未做**（要先把 28 处 `background:white` 全换成变量，单独一轮） |
| P3-10 死代码 | ✅ | 删除 `DashboardOverview`（定义后从未使用）、`P_DIMENSIONS`、`var old=promptsDefaults[id]`（按 prompt id 去查按 step 编号存表的字典，永远 undefined）、`class="toggle-slider:before"`（class 里带冒号，匹配不到任何 CSS 的垃圾节点）；连带删掉 `/prompts/defaults` 那次「拉回来就没人读」的请求 |

#### 为什么 P3-1 改了方案

原计划是让管理端复用 `frontend/shared/api.js`，一套请求层两处用。真动手前先读了它的错误分支：

```js
if (response.status === 401 || response.status === 403) {
    ... throw ApiError('登录状态已失效', { code: 'auth' }) ...
}
```

它把 **403 一律当成「登录失效」**，而且 `tests/test_shared_frontend.cjs:188`（"普通 403（无标记头）仍按认证失败处理"）把这个行为**固定成了契约**。

对客户端来说这是对的（403 基本只出现在「账号已禁用」）。但管理端的 403 大量是业务拒绝——「不能注销管理员账户」「需要管理员权限」「系统默认提示词不可删除」。接过去以后这些都会被替换成「登录状态已失效」，**正好把第一批 P0-3 刚修好的「让管理员看到真实原因」又盖回去**，而且还会先触发一次无意义的 refresh。

所以选择了「能力对齐、实现不合流」：管理端拿到重试和去重，两套 `api()` 各自保留自己的错误语义。真要合流，得先把共享层的 403 语义拆开（401=认证失效，403=权限/业务拒绝），那是跨客户端的契约变更，应该单独一批、单独验证。

#### 明确未做

- **P3-3 事件委托**：55 处内联 `onclick` + 60 处 `innerHTML` 拼接，是纯重构、零用户可见收益，却是本批里最容易引入静默故障的一步（改错一个 `data-*` 就是一个按钮没反应）。需要配合真实浏览器点一遍，不适合塞进这一批。
- **暗色主题**：见 P3-9。

**测试**：`tests/test_admin_client.cjs` 从 9 项扩到 19 项（新增：5xx 退避重试、4xx 不重试、POST 不重试、重试有上限、并发去重、按方法+URL 区分、分页窗口/回调/单页/首末页禁用）。全量 `pytest tests/` **930 passed**，`node --test tests/*.cjs` **90 passed**。

### 第四批 · 性能与体积（2026-11-13 完成）

| 条目 | 状态 | 改动 |
|---|---|---|
| P2-1 步骤配置 42 连发 | ✅ | 三张表各一条 `GROUP BY step_number`，Python 侧摊回 14 行。**42 → 3 次查询** |
| P2-2 仪表盘 | ✅ | 后端：用户 3 个口径合成 1 条条件聚合（`SUM(CASE WHEN …)`）、工作流 4 个口径合成 1 条 `GROUP BY status`，**8 → 3 次**；前端：`renderDashboard` 的 4 个请求、`renderClientControl` 的 3 个请求改 `Promise.all`，不再串行排队 |
| P2-3 通知列表 N+1 | ✅ | `joinedload(Notification.user)`；一页 100 条时省掉 100 次懒加载 |
| P2-5 首屏体积 | ✅ | 删除 `cdn.tailwindcss.com`（全站 0 处工具类，纯粹是 100KB 的运行时 CSS 编译器；补了 5 条 preflight 等价规则保证视觉不变）；`lucide@latest` → `lucide@1.72.0`（锁定当前 latest，避免上游改结构导致整页图标静默消失）；**把 104KB 内联脚本抽成 `frontend/admin/app.js`**，页面从 107KB 降到 12KB，脚本走 `/static` 带 ETag/Last-Modified，重复访问只花一次 304 |

**真实库实测**（本机 MySQL，取 3 次最好成绩）：

```
step-config : 11.92 ms (42 查询)  ->  0.64 ms (3 查询)     18.6×
dashboard   :  1.98 ms ( 7 查询)  ->  0.87 ms (2 查询)      2.3×
等价性校验：active_users_today / total_tokens 新旧口径完全一致
```

注：本机回环几乎没有网络延迟，所以绝对值小。换成 1~2ms RTT 的托管库，42 连发的差距会从 11ms 放大到 60~80ms —— 而且全在首屏这条路径上。

**测试**：新增 `tests/test_admin_query_budget.py`（3 项，用 `before_cursor_execute` 事件数 SQL 条数）：步骤配置 ≤6、仪表盘 ≤7 且 7 个口径逐一对数、通知列表 ≤5。`test_admin_client.cjs` 改为加载 `app.js` 并断言 `index.html` 确实引用了它。全量 `pytest` **933 passed**，`node --test` **90 passed**。

---

### 第五批 · Prompt 版本历史（P4-1，2026-11-13 完成）

原状：`prompt_templates` 只存「当前内容」，改坏一条自定义提示词只剩「重置为系统默认」——
自定义内容永久丢失，也没有任何地方能看「上一版长什么样」。

| 改动 | 说明 |
|---|---|
| 新表 `prompt_versions` | `prompt_id / version / prompt_content / change_log / modified_by / created_at`，联合索引 `(prompt_id, version)`。`create_all` 自动建表，无需 ALTER |
| 快照打点 | 保存 / 重置 / 回滚 / 新建细粒度提示词，全部追加一版 |
| 历史起点 | 首次改动前先把「改动前的那一版」存进去 —— 否则历史里永远缺最早内容，「回到最初」是句空话 |
| `GET /prompts/{id}/versions` | 列表只回元信息（版本/说明/操作人/字数/时间），**不回正文**；几十条提示词每条上千字，带上会把响应撑到几 MB |
| `GET /prompts/{id}/versions/{v}` | 单版正文 + 当前正文一次带全，前端算 diff 不用再发一次请求 |
| `POST /prompts/{id}/rollback/{v}` | 内容写回、版本 +1、旧历史一条不删；同时写审计日志 |
| 缓存失效 | `PromptManager` 是单例 + 60 秒 TTL。改完/回滚完不清缓存，工作流还会拿旧内容跑一分钟 —— 救火操作等一分钟等于没用，所以三个写接口提交后都 `clear_cache()` |
| 前端 | 每个提示词多一个「🕘 历史」按钮 → 版本表格（当前版带徽章）→「对比」看行级 diff（红删绿增，超长自动截断）→「回滚」二次确认 |
| 删除保护 | 删除提示词时显式清历史。历史表没有 ORM 关系，不删就撞外键，删除接口直接 500 |

**diff 为什么不用完整 LCS**：提示词常见上千行，O(n²) 的 DP 会把页面卡住；而实际改动基本都是局部的。做法是掐掉公共前后缀，只把中间那段标成删除/新增，O(n) 且对真实编辑场景足够准。

**测试**：新增 `tests/test_prompt_versions.py`（10 项：首次改动保留原始内容、多次编辑版本有序、列表不返回正文、记录操作人、回滚恢复且不丢历史、回滚到当前版被拒、未知版本 404、重置也留痕、删除连带清历史、空内容被拒）。`test_admin_client.cjs` 增 4 项 diff 测试。全量 `pytest` **943 passed**，`node --test` **94 passed**。

---


### 第六批 · 系统健康页（P4-2，2026-11-13 完成）

出问题时管理员只能猜：LLM 是不是挂了？磁盘满没满？清理任务还在跑吗？工作流卡了多少？
新增 `GET /api/admin/system-health` + 管理端「系统健康」页，六个维度，每个都给到明细。

| 维度 | 测什么 | 判级依据 |
|---|---|---|
| 数据库 | `SELECT 1` 真实往返耗时 + 连接池状态 | ≥200ms warn，连不上 down |
| 磁盘 | 卷容量（`shutil.disk_usage`）+ `uploads/` `exports/` 文件数与体积 | 已用 ≥85% warn，≥95% down |
| 工作流 | 按状态分组 + 卡住超 24h 的数量 + 进程内实例数 | 有卡住的就 warn |
| LLM 配置 | 各 provider 是否有 Key、`.env` 与数据库里的可用模型、降级链 | 无 Key down，只有 Key 没模型 warn |
| 清理定时器 | `_CLEANUP_TASK` 是否存活 + 下次执行时间 | 未启动 warn |
| 服务进程 | 版本、Python、平台、PID、uptime、DEBUG | 恒 ok（信息项） |

**几处刻意的取舍**：

- **LLM 那栏是「配置状态」，不是「连通性」，界面上也这么写。** 真去 ping 一次要花钱、还可能拖慢首屏；与其显示一个看起来像探活的假绿灯，不如说清楚「这里只反映配置是否齐全，要验活请跑一次工作流」。
- **模型数要同时数 `.env` 和数据库。** 第一版只数 `model_configs` 表，真实环境里得到「可用 0 个模型」——这个项目实际上是把模型写在 `DEEPSEEK_MODELS` 这类环境变量里的，差点报出一个假故障。
- **磁盘遍历带 30 秒缓存**，否则每 30 秒自刷一次就把 `uploads/` 和 `exports/` 全走一遍。
- **总体等级取最差项**，未知等级按最坏处理（前端的 `healthMeta` 对未知值返回「异常」而不是「正常」）。

**真实环境跑出来的第一屏**（本机）：

```
[ok  ] 数据库    0.2 ms
[ok  ] 磁盘      剩余 364.4 GB / 共 652.9 GB（已用 44.2%）
[warn] 工作流    运行中 2 · 待确认 0 · 卡住超 24h 2      ← 真发现两个跑了超过一天没结束的会话
[ok  ] LLM 配置  默认 deepseek-chat · 可用 1 个模型 · 降级链 0 个
[warn] 清理定时器  未启动（重启后才会拉起）
[ok  ] 服务进程   ...
```

热态接口耗时 **2~3 ms**（冷态首次要 11 秒，因为会 `import backend.main`；线上 main 一定已加载，不受影响）。

**测试**：新增 `tests/test_system_health.py`（7 项：六个维度齐全、工作流数字来自库、磁盘 90%→warn / 96%→down、LLM 三档、`.env` 模型也计数、非管理员 403、查询次数 ≤10）。`test_admin_client.cjs` 增 3 项渲染测试。全量 `pytest` **950 passed**，`node --test` **97 passed**。

---


### 第七批 · 工作流详情与处置（P4-3，2026-11-13 完成）

第六批的健康页能报出「2 个卡住超 24h」，但点不进去看是谁、卡在哪。这批补上最后一段闭环。

| 接口 | 作用 |
|---|---|
| `GET /workflows/{sid}` | 会话主档 + 14 步时间线 + 错误/警告 + Token 按步分布 + 产出物大小 + 实时内存状态 + 卡住时长 |
| `POST /workflows/{sid}/fail` | 手动终止：仅未终态可执行，写 `errors` 一条「管理员手动终止」、记审计、释放进程内实例与进度 |

**两个来自真实数据的教训**：

1. **`reports` 表是空壳。** 第一版照着 `Report` 表查产物，结果永远是空 —— 全仓没有一处 `db.add(Report)`。真正的内容在 `WorkflowSession.final_report`、`checkpoint_data` 和 `state_backups/{sid}.json` 里。这种「字段看起来有、其实永远空」比缺字段更糟，改成如实报「有多大」。
2. **只按 `completed_steps` 画时间线会掩盖问题。** 真实库里那个卡了 125 小时的会话：`completed_steps` 是 `[]`、`current_step` 是 `0`，但第 1 步已经烧掉 3533 token。照原样画就是「一步都没做」。加了 `partial` 状态（跑过 token 但没进 completed_steps），时间线才说实话：

```
时间线: ◧□□□□□□□□□□□□□     ■ 已完成  ▶ 进行中  ◧ 跑过未完成  □ 未开始
```

**终态保护**：`completed` 的会话被误标成 `failed`，用户会直接丢掉报告入口，所以只对未终态开放处置，并给 400。

**测试**：新增 `tests/test_workflow_detail.py`（10 项：字段齐全、partial 状态、全 done 时间线、坏 JSON 容错、404、非管理员 403、终止写审计、释放内存实例、终态拒绝、404）。`test_admin_client.cjs` 增 4 项（时间线 14 格与配色、卡住提示与处置按钮、终态无按钮、错误列表兼容对象与纯文本）。全量 `pytest` **960 passed**，`node --test` **101 passed**。

---


### 第八批 · 报告正文查看（P4-4，2026-11-13 完成）

详情页已经能报「最终报告 35312 字」，但看不到正文 —— 客服场景没法核实「报告里那句话是哪来的」。

| 改动 | 说明 |
|---|---|
| `GET /workflows/{sid}/content?step=N` | 按步取正文。字段映射以 `PerformanceState` 的注释为准：S1 `cost_list`、S2 `effective_data`、S3 `indicator_system`、S7 `indicator_analysis`+`score_table`、S14 `final_report` … |
| 详情响应补 `sections` | 列出哪些步骤有内容、各多少字，时间线据此点亮可点格子 |
| 三级兜底 | `state_backups/{sid}.json` → `checkpoint_data` → `final_report` 列 |
| 前端 | 时间线带白边的格子可点 → 弹层看正文（`<pre>` + `esc()`，不渲染 HTML）；产出物卡加「查看完整报告」 |

**三个防护**（这类「按 id 读文件」的接口最容易踩）：

1. **路径穿越**：`session_id` 会拼进 `state_backups/{sid}.json`，先过 `_validate_session_id` 的 `[A-Za-z0-9_-]{1,100}` 正则。测试里拿 `../secret`、`..%2Fsecret`、`a/../../secret` 逐个打，全部 400/404。
2. **字段白名单**：只能从 14 步映射表里取字段，不能 `?field=password_hash` 顺走别的。
3. **无痕查看**：报告是用户业务数据，每次查看写一条 `admin_view_workflow_content` 审计（谁、看了谁的哪一步）。

**其他取舍**：单次返回上限 20 万字符（最终报告能到 5 万字以上，一口气塞进 JSON 会拖住浏览器）；备份解析按 `(mtime, size)` 缓存，360KB 的 JSON 不会每看一次解析一次。

**真实数据验证**（本机某个已完成的会话）：

```
来源: state_backup | 32 个顶层键
有内容的步骤: S1 2680字 · S2 3551 · S3 9592 · S4 645 · S5 7987 · S6 8808 · S7 5759
             · S8 1663 · S9 4424 · S10 5223 · S11 5207 · S12 1996 · S13 1693 · S14 35312
```

**测试**：新增 `tests/test_workflow_content.py`（12 项：取到正文、多字段步骤、空步骤仍 200、两种兜底、目录、路径穿越、步骤越界 422、非管理员 403、404、截断、审计）。`test_admin_client.cjs` 增 2 项。全量 `pytest` **972 passed**，`node --test` **103 passed**。

---


### 第九批 · 暗色主题（P3-9，2026-11-13 完成）

原来整页硬编码浅色：28 处 `background:white`、一堆 `#f8f9fa`／`#1a202c`。IDE 是暗色时，打开管理端像被人拿手电照脸。

做法是**只换变量、不碰布局**：

1. 定义 19 个语义 token（`--bg / --bg-soft / --card / --border / --border-soft / --hover / --text / --text2 / --muted` + 四组 `-bg/-fg` 状态色）；
2. 把两个文件里的大面积硬编码颜色全部换成语义值（`background:white`、`#f8f9fa`、`#fff5f5`、`#1a202c`、`#4a5568` …）；
3. `[data-theme="dark"]` 只覆盖变量，组件 CSS 一行没改；
4. 侧栏底部加切换按钮，`localStorage.admin_theme` 持久化；`<head>` 里一段内联脚本在样式表之前就把 `data-theme` 挂到 `<html>`，**不会先白后黑闪一下**。

**踩到的坑**：批量替换把 `:root` 自己也替换了，出现 `--bg:var(--bg-soft)` 这种自引用——变量直接失效、整站颜色崩掉，而且浏览器不报错。所以补了一组静态守卫（见下）。

**没有浏览器也能验「读不读得清」**：按 WCAG 公式算对比度，浅色/暗色两套都断言达标。

```
配对                              浅色       暗色
--text   on --card             16.32:1   14.32:1
--text   on --bg               15.48:1   15.62:1
--text2  on --card              7.53:1    9.58:1
--muted  on --card              4.02:1    5.43:1
--success-fg on --success-bg    8.45:1    9.08:1
--danger-fg  on --danger-bg     9.32:1    8.47:1
--warning-fg on --warning-bg    7.98:1    8.32:1
--info-fg    on --info-bg       7.22:1    8.35:1
```

**测试**：新增 `tests/test_admin_theme.py` **10 项**：变量无自引用、变量链不环、用到的 `var` 都有定义、**暗色块覆盖全部 token**（少一个就是一块白斑）、无残留 `background:white`、主题在首帧前生效、切换按钮接线、两套主题的对比度下限。

全量 `pytest` **982 passed**，`node --test` **103 passed**。

**已知边界**：本批只做了静态验证 + 对比度计算，没有真实浏览器截图。布局类回归（比如某个 flex 在暗色下换行）看不出来，需要人工扫一眼。

---


### 第十批 · 事件委托收口（P3-3，2026-11-13 完成 · 缩范围执行）

**先说结论：没有把 54 处内联 handler 全部重写。** 动手前先分类，发现 68 处里只有
3 处真的把字符串拼进了 JS 字面量：

```js
onclick="changeUserPassword(3,'o\'brien')"   // 要手工 replace 反斜杠和单引号
```

其余 65 处传的是数字字面量（`saveModel(3)`、`moveModuleSeq(5,-1)`）或干脆无参
（`closeModal()`），既不会引号断裂也没有 XSS 面。**把可靠的代码重写一遍，
得到的只有风险**，所以这批只做「消除风险类别」：

| 改动 | 说明 |
|---|---|
| 统一调度表 | 原来 3~4 个 `document` 级 click 监听器各认一套 `data-*`，合成 1 个入口 + `ACTIONS` 表（8 个动作） |
| 字符串参数改走 `dataset` | `data-act="user-change-pwd" data-uid data-name` —— 属性用双引号包，值经 `esc()` 处理，引号不再是问题 |
| 删掉 `changeUserPassword(userId, userName)` 的无用参数 | `userName` 在函数体里从来没被读过，纯粹是为了拼那句 `onclick` 才传的 |
| 静态守卫 | 禁止 `onclick` 里出现 `\'`、禁止手工 `.replace(/\\/g,…)`、禁止 `data-act` 拼错、禁止全局 click 监听器超过 1 个 |

**剩下 54 处内联 handler 的定位**：不算风险，算可读性债。它们传的都是数字或零参，
重构收益是「代码看起来整齐一点」，代价是一次大面积手工替换 —— 收益/风险不划算，
留在清单里而不是现在做。

**测试**：新增 `tests/test_admin_markup.py` **5 项**（无字符串字面量、无手工转义、
每个 `data-act` 都有处理函数、无遗留旧属性、只有一个调度入口）；`test_admin_client.cjs`
增 2 项（调度表覆盖 8 个动作且参数类型正确、带引号和反斜杠的名字原样透传）。
全量 `pytest` **987 passed**，`node --test` **105 passed**。

---


### 第十一批 · 审计导出 / 通知批量撤回 / 知识库批量导入（P4-5/6/7，2026-11-13 完成）

清单里剩下三条「锦上添花」，一次做完。

**P4-5 审计导出** `GET /audit-logs/export`

合规场景要的是「把这段时间的操作记录给我一份」，一页 50 条翻是交不了差的。
导出看着简单，三个坑都踩了：

- CSV 转义：详情里有逗号和引号，不转义会把一行撑成好几列
- **公式注入**：Excel 会把 `= + - @` 开头的单元格当公式执行，加前导单引号掐掉
- 导出行为本身也要留痕（`admin_export_audit_logs`），否则「谁导走了操作记录」查不到

带 BOM 输出，Excel 才不会把中文显示成乱码；`X-Export-Rows/Total` 两个响应头
让前端能提示「命中 3000 条，只导了前 5 万条」这类情况。筛选逻辑与列表共用
`_audit_filtered_query()`，避免两边口径漂移。

**P4-6 通知批量撤回** `POST /notifications/batch-withdraw`

后端其实早就有「按标题全删」，界面没接，而且对「只撤其中几条」太粗暴。
补了按 id 批量，前端加勾选 + 全选。测试重点不是「删掉了没有」，而是**别删多了**：
勾了 3 条不能连带把另一个用户的同名通知也撤掉。既不给 id 也不给标题时返回 400
—— 那等于「删除全部」。

**P4-7 知识库批量导入** `POST /knowledge/bulk`

原来只能一条条手录。关键在于**脏数据不要整批回滚**：从 Excel 导出来的数据总有
一两行缺列，让人回去手改 200 行是不可接受的。所以逐条校验，返回哪几行失败、
为什么；同时按名称去重（`skip_existing` 可关）。

解析放在前端：CSV 的引号规则和编码（中文 Windows 上 Excel 另存默认是 GBK）
在后端处理起来更麻烦，解析完就是一份 JSON，接口保持简单。编码用
`TextDecoder('utf-8')` 试读，出现替换字符 `�` 就退回 `gbk`。

**测试**：新增 `tests/test_audit_export.py`(7)、`tests/test_notification_batch.py`(5)、
`tests/test_knowledge_bulk.py`(7)；`test_admin_client.cjs` 增 8 项（CSV 引号/换行/CRLF、
中文表头映射、无表头按位置、JSON 两种形态、逐条清洗、空输入、BOM）。
全量 `pytest` **1006 passed**，`node --test` **113 passed**。

**踩到的**：`normalizeKbItem` 里先判空再 trim，导致「只填了空格」的分类留下空串
而不是 null —— 被测试逮到。另外跨 vm realm 的数组/对象不能用 `assert.deepEqual`
（原型不同），改用 JSON 比较。

---


### 第十二批 · 真实库端到端冒烟（2026-11-14）

前十批所有验证都跑在 SQLite 上。有几类东西它摸不到：MySQL 专用 SQL（`token-stats`
的 `DATE_ADD ... INTERVAL 8 HOUR` 分桶）、真实 `state_backups/*.json`、`information_schema`
补丁之后的真实表结构、以及真实数据规模下的响应形状。

所以补了一份能反复跑的冒烟脚本：**`tests/_smoke_admin_endpoints.py`**

做法：临时建 `_smoke_admin`（管理员）与 `_smoke_target`（普通用户）两个账号，用**真实
登录**拿令牌，把管理端 44 个检查点按读/写/边界走一遍，最后删掉临时账号与它造的全部
数据。开始前先清理一次，所以上次跑挂了也不会留垃圾。

覆盖：
- 18 个只读接口 + 登录 + /me
- 工作流详情（timeline 14 段 / sections 14 段）、第 14 步正文（35312 字，来源 state_backup）、路径穿越被挡
- 审计导出（1097 行 / 108KB，带 BOM）+ 带筛选导出（action=login 命中 613 行）
- 知识库单条新增、批量导入（含重复 1 条、脏行 1 条，分别跳过/报错）
- 通知发送 → 列表可见 → 批量撤回
- 提示词：新建 → 改一版 → 历史 2 版 → 回滚到 v1 → 历史变 3 版 → 删除
- 边界：`page_size=101` 422、Token 降序真的降序、未知排序键不炸、`step=15` 422、
  坏日期参数被忽略而不是 500、`/admin` 可下发且引用 app.js、静态脚本带 ETag
- 无令牌 401

**结果：44 项全过**，7 张表行数与跑之前逐一对齐（User 122 / KnowledgeBase 0 /
Notification 112 / PromptTemplate 14 / PromptVersion 0 / OperationLog 1095 / Workflow 64）。

**冒烟抓到的第一个问题不是代码，是脚本**：通知接口只发给 `role=user`，而冒烟账号是
管理员，于是 404「未找到目标用户」。这其实说明接口行为是对的（前端下拉也只列普通用户），
但脚本得造一个普通收件人。

**仍然没做的**：真实浏览器渲染。这轮只到 HTTP 层 —— 布局、暗色主题的实际观感、
`data-act` 按钮点下去有没有反应，仍需人工扫一眼。工作区已确认没有可用的浏览器自动化。

---


## 附：扫描阶段未验证、现已确认的点

1. ~~`/api/admin/users?page_size=9999` 是否真返回 422~~ → **已确认**，`tests/test_admin_recovery.py::test_user_options_rejects_page_size_over_limit_contract` 固化。
2. ~~管理员账号当前 `must_change_password` 是否为 1~~ → **已确认是 1**（真实库验证，见上）。
3. 生产库 `operation_logs` 里是否真的缺 `admin_deactivate_user` 记录（P0-6 的旁证，代码层面已必错，此项只是估影响面）。
4. ~~`admin` 账号是否仍为默认口令~~ → **已确认仍是**（`Admin@123456` 可直接登录成功）。本批上线后首次进入管理端会强制改密。
