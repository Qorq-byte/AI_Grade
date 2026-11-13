# 云海睿评工程扫描与问题清单

## 2026-11-13 · 客户端实时步骤

- 截图对应 frontend/client/index.html；独立 workflow 页面此前已有按步骤缓存实现。
- client 的 delta 回调只写 DOM，不保存 wfStepContents，切换即丢失；回调按执行步骤过滤却向当前查看面板写入，导致串步。
- step_start/step_complete 无条件覆盖内容；updateWfStepper 清除 active；每次 delta 强制滚动外层，实际滚动区为 .wf-content-body。
- 服务端可能先发 token_delta 再发 step_start，开始事件不能清掉提前收到的增量；完成正文应覆盖增量，避免重复。
- 保留原有未提交的 client 问卷返回逻辑及 tests/test_frontend_contract.py、问卷探针改动。

基线为 `main` 的 `0d24ab7`。开始时工作区有两个用户未跟踪文件，均予以保留。行号指当前工作区，明确标注基线的条目使用原始行号。

## 扫描范围

- 从 `backend/main.py:60` 的页面路由和 `backend/main.py:230` 的 API 注册追踪登录、令牌、角色及客户端和管理端调用。
- 从 `backend/routes/agent_routes.py:409` 的上传入口追踪到 `backend/services/file_processor.py:16` 的格式分派，以及 `backend/agents/performance_agent.py:611` 的工作流资料解析。查看了 LangGraph 初始化、步骤 1–14、评分、问卷、编辑和导出。
- 对照客户端和独立工作流页的流读取、审批、状态和下载流程。沿工作流记录、内存、checkpoint、备份文件核查保存和恢复顺序。
- 查看管理端鉴权、用户管理、工作流监控、保留期预览与执行，核查启动配置、Windows 脚本、Docker 入口、依赖和迁移脚本。逐一检查了五个验证脚本的地址、写入和模型调用。

## 已复现并修复

1. **P0 · Python 语法阻断**。基线 `backend/agents/tools/multi_model_compare.py:258` 有孤立引号。以编译全部 Python 源文件复现，基线 47 个文件中 1 个失败。验收为零语法错误、导入应用及比较模块成功，比较服务的自定义回调和内置 Agent 路径均能保留成功与失败的模型结果。
2. **P0 · 私有工作流、报告和问卷越权**。基线 `backend/routes/agent_routes.py:823`、`:949`、`:1014`、`:1095`、`:1241`、`:1269`、`:1558`、`:1805` 由调用者提供会话 ID，缺少服务端归属校验。用两个普通用户、管理员和匿名请求覆盖状态、审批、保存、导出、答案和删除。验收为匿名拒绝、跨用户返回 404、所有者及管理员可走相应业务路径。现行校验集中在 `backend/routes/agent_routes.py:67`，下载见同文件 `:1199`。
3. **P0 · 客户端报告列表和删除绕过归属**。基线 `backend/routes/client_routes.py:633`、`:741`、`:795` 可从全局缓存及磁盘补入他人报告，单删和批删有 SQL 查无所属记录后仍清理缓存或文件的路径。第二个用户请求第一用户的记录即可复现。验收为仅列出数据库确认归属的记录，跨用户删除既不改数据库也不删备份，合法删除先提交 DB 再清理本地投影。实现见 `backend/routes/client_routes.py:633`。
4. **P0 · 旧用户管理路由与空签名密钥**。基线 `backend/routes/auth_routes.py:86`–`:135` 有公开用户管理入口。空 JWT 密钥也可用于构造签名。验收为旧管理路由需要管理员，空密钥拒绝令牌，登录签发失败时返回 503。实现见 `backend/middleware/auth_middleware.py:26`。
5. **P0 · 启动路径可接受任意本地资料路径**。基线 `backend/routes/agent_routes.py:501`、`:786` 直接向图传入请求的 `file_paths`，由 `backend/agents/performance_agent.py:611` 解析。隔离用例使用另一用户上传的实际路径。验收为仅接受本账号上传目录内的文件，未获授权的路径在启动前被拒绝，上传及启动的响应字段仍可由原入口消费。见 `backend/routes/agent_routes.py:581`。
6. **P1 · 重复启动、审批和 SSE 状态错误**。基线 `backend/routes/agent_routes.py:482`、`:595`、`:718`、`:949` 重用同会话执行实例，后台与流循环重复写终态，部分输出后的异常也能走完成落库路径。验收为数据库唯一会话登记、重复启动 409、无可恢复检查点或执行中的审批 409，断流后仍有终态，失败永远不会走 SSE 的完成分支。见 `backend/routes/agent_routes.py:610`、`:641`、`:977` 和 `backend/routes/admin_routes.py:514`。
7. **P1 · 重启恢复与人工编辑**。基线 `backend/routes/agent_routes.py:1014`、`:1095`、`:1269`、`:2732` 的报告读取和导出有不同的长度阈值，评分与章节编辑只写内存；基线 `backend/agents/performance_agent.py:545` 的节点备份会覆盖问卷。验收为短报告由 MySQL 记录优先读取和导出，评分、章节或样式的人工更新写磁盘投影，备份合并保留问卷并采用同目录原子替换。隔离测试清掉内存后再读，保留已有备份字段和旧版数字字符串步骤键。见 `backend/routes/agent_routes.py:1038`、`:2540` 和 `backend/agents/performance_agent.py:576`。
8. **P1 · 公开问卷生成和问卷删除串联错误**。基线 `frontend/questionnaire/index.html:201` 从公开页调用生成接口，后端提交接受不存在的问卷；删除问卷路径会删工作流及备份，却未删答卷。验收为公开链接只读已有题目并可提交，缺失问卷 404；生成、列表、答案和删除按私有资源校验，删除问卷保留报告并清除答卷。公开页同时兼容历史和当前题目字段。见 `backend/routes/agent_routes.py:1398`、`:1719` 和 `frontend/questionnaire/index.html:192`。
9. **P1 · 上传和前端流状态**。基线 `backend/routes/agent_routes.py:337`、`:396` 整文件读取后才检查 50 MiB 限额，单文件的超限路径还会先创建目标文件，资料解析在事件循环同步执行。两个前端入口对 HTTP 错误、结束和断开行为也不同。验收为已知超限不读内容，未知大小以 1 MiB 分块读取，失败清理半成品，单文件解析移到线程池，两个页面用相同终态规则。见 `backend/routes/agent_routes.py:409` 和 `frontend/workflow/sse.js:1`。
10. **P1 · 保留期清理依赖过期会话**。基线 `backend/services/cleanup_service.py:67` 在没有过期会话时提前返回，单独上传的过期文件不清理。用临时目录中的一旧一新两个文件及零过期会话复现。验收为删旧留新、计数准确，并保留上传根目录。见 `backend/services/cleanup_service.py:27`。
11. **P2 · 测试假绿与部署配置**。`test_all_apis.py:15` 原先把错误字符串计为通过，弱密码导致注册失败后跳过普通用户分组。前端联通性脚本依赖本地某个头像和 DeepSeek 配置。Docker 入口曾把环境变量直接插进 Python 源码，并回显 Redis URL。验收为测试断言真实通过、脚本使用合成头像和模型列表结构、入口用环境变量传参且日志无 Redis URL。应用显式声明直接导入的 LangChain 集成和验证脚本的 HTTP 依赖。

## 待验证风险

- **P1 · 跨进程恢复**。 `backend/agents/performance_agent.py:181` 仍使用进程内 MemorySaver。磁盘能恢复报告和已写入的人工编辑投影，但无法重建挂起任务的可执行 checkpoint。验收场景为两个 worker 分别启动、审批，再杀掉其中一个进程重试，验证只有一个执行者且同一步不会二次消耗模型。此次保留现有自动执行模式，未引入持久 checkpoint 或分布式协调。
- **P1 · 部署认证与数据清理的历史策略**。`backend/services/auth_service.py:11` 实际使用带盐 SHA‑256，README 对 bcrypt 的描述与代码不符。首次启动的固定管理员凭据也仍存在。`backend/services/cleanup_service.py` 的跨文件与 DB 事务、预览与执行范围、后台清理与手动删除竞争需用多用户数据验证。验收分别为历史密码可登录并安全升级，以及故障注入后可重跑且仅清理符合条件的数据。
- **P2 · 实际模型 Token 和重试**。`backend/agents/performance_agent.py:325`、`:332`、`:376` 存在输入兼容回退，需真实模型和故障注入区分正常分支、响应格式回退和已计费的失败请求。验收为相同样本逐步对照真实请求次数、输入及输出 Token、超时后恢复与输出质量。此次仅去掉 SSE 的重复终态写入，实际 Token 优先于 SSE 估算值。
- **P2 · OCR 与上传传输边界**。`backend/services/file_processor.py:110` 依赖 Tesseract，当前只验证文本上传及应用保存阶段。验收为中文扫描件 OCR、并发 PDF/图片解析期间请求响应时间，以及代理层限制超大 multipart 请求的磁盘占用。代理缓冲、Docker 容器启动和浏览器 UI 交互需要对应环境复验。

## 可选改进

- **P3 · 上传解析复用**。`backend/routes/agent_routes.py:461` 的预览与 `backend/agents/performance_agent.py:611` 的正式解析可能处理同一文件两次。先在带 OCR 的真实样本记录解析次数、耗时和文本一致性，达到同等结果后再决定是否复用。
- **P3 · 依赖锁定和更细粒度的大字段查询**。当前已补齐直接依赖并减少按步骤更新，下一步可在 CI 用固定 Python/依赖组合和代表性数据量验证安装与查询计划。

## API 与历史数据

私有接口及下载要求 Bearer access token。公开问卷新增 `GET /api/agent/workflow/{session_id}/questionnaire`，现有提交地址继续公开，前提是该会话已有问卷。管理员可按已知 ID 查看无归属的历史备份，普通用户的恢复使用已有 `workflow_sessions.user_id`。缺少归属的历史数据需要依据可核实的业务记录，由管理员在隔离环境核对并补录归属后再恢复到用户名下。此次无需新增数据库字段。

旧的无归属上传路径需要重新上传后才能用于新工作流。旧导出文件可从原报告重新导出，文件名字段保持原结构，新下载按签发文件的账号隔离。同一会话 ID 只登记一次，重新运行评价应使用新会话 ID。空 JWT 密钥环境需配置密钥并重新登录。应用仍采用原有 14 步自动执行流程。

## 第二轮 · 工作流、知识库与问卷

本轮以上一轮未提交工作区为基线，沿客户端和独立工作流页、上传解析、管理端知识库、LangGraph、问卷和报告出口核查。仓库根目录没有额外的 AGENTS.md 文件，执行会话提供的仓库指令。

- **P0 · 输入与进度。** 原图状态漏掉 `file_paths` 和评价类型，已上传的样例无法在步骤中使用。原 SSE 根据完成结果补发开始事件。现在每个真实节点在开始时发布进度，长步骤保持心跳，两个页面以已完成步骤和实际当前步骤为准。验证使用包含预算 20 万元及 2025 年验收的脱敏文件，在步骤 1 阻塞时检查开始事件、心跳和 0/14 状态。实现见 `backend/agents/states/performance_state.py`、`backend/agents/performance_agent.py` 的 `_tracked_node` 和 `backend/routes/agent_routes.py` 的 `start_workflow_stream`。
- **P0 · 恢复和审批。** 页面刷新及断线后通过原状态接口恢复结果。失败步骤可重试当前进程的检查点，已完成步骤不会重新调用模型。步骤 3、6、11 可按会话选择审批，维持既有默认自动执行。新页面携带待审批步号，过期审批返回 409。验证包含步骤 2 首次失败、同一步重试、三处审批、拒绝后仍暂停和重复请求。实现见 `backend/routes/agent_routes.py` 的 `get_workflow_status`、`retry_workflow`、`approve_workflow` 和 `frontend/workflow/sse.js`。
- **P1 · 检索范围和证据。** 原向量入口使用进程单例，主链没有索引上传文件或管理端条目。主链现使用本会话解析后的材料和启用的全局知识条目进行按步骤关键词检索。管理端条目须匹配步骤或无步骤，评价类型参与匹配。项目片段与通用规则分列，合计最多五段，每段最多 480 字。索引保留 PDF 页号、标题或提取文本行号以及知识记录 ID。无匹配时使用资料不足提示，通用知识不作为项目事实。验证使用重复段落、停用条目、异步骤条目和空检索。实现见 `backend/services/workflow_evidence.py` 和 `backend/services/file_processor.py`。
- **P1 · 问卷和旧答卷。** 原生成先读旧缓存，也接受模型返回的重复题及不存在的指标。现在对当前指标体系、题目数和所选指标做版本指纹，从可调查叶指标生成统一方向的题目。已打开的历史版本可继续提交，服务端从对应题目恢复题干及指标，并校验选项范围。旧问卷格式继续接受 `id/question`。CSV 为每列转义引号、换行和电子表格公式前缀。实现见 `backend/agents/tools/questionnaire_gen.py`、`backend/routes/agent_routes.py` 的问卷路由和 `frontend/questionnaire/index.html`。
- **P1 · 评分、人工编辑和报告。** 原评分汇总把零分和缺项当作满分。当前确认 ID、数值范围、层级与合计，只有匹配本步骤项目证据和理由的模型评分才进入结构化结果。缺项标待核实。人工编辑保存版本和原内容，已完成下游标记待复核。组装草稿会用待复核提示替代受影响章节，最终导出需要先完成复核。更换指标体系保留旧评分快照，避免沿用旧分。实现见 `backend/agents/tools/score_validator.py`、`backend/agents/performance_agent.py` 的步骤 3、4、7、9、11、14，以及 `backend/routes/agent_routes.py` 的步骤编辑与组装路由。

### 步骤依赖

- 初始化解析已授权上传。步骤 1 整理材料，2 提取核心事实并建立索引，3 以核心信息和评价类型构建指标。
- 步骤 4 校验分值，5 制定评分依据，6 汇总评价表并提供问卷入口。问卷读取当前指标树，不以生成的表格正文作为数据源。
- 步骤 7 分析并校验有证据的评分，8 总结经验，9 从经校验扣分点梳理问题，10 对应问题提出建议，11 汇总评分和评价结论。
- 步骤 12、13 生成项目基本情况和评价过程。步骤 14 按章节组装，附上可用证据索引，使用程序组装而不调用模型。步骤 1–13 的正常生成使用模型，材料不足分支可输出待核实说明。

### 兼容和测试边界

API 字段及 SSE 事件保留原有含义。新增会话级审批选择、状态内容增量、失败步骤重试、步骤编辑和复核，以及可选问卷版本字段。暂无数据库结构迁移。进度、已完成结果及编辑可读自磁盘，跨进程执行检查点仍依赖当前进程的 MemorySaver，强制进程重启后的任务续跑单独待验证。固定模型验证可核查结构、数据流与边界。真实模型事实一致性、真实 Token 成本、浏览器和反向代理实跑及更大知识库的排序质量仍待复验。
