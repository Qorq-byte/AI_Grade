# 验证记录

## 2026-11-13 · 客户端步骤同步修复

- 应用 brainstorming / investigate / planning-with-files；沿用用户已经确认的需求，不另加交互审批。
- 已追踪并修复 SSE、步骤选择、恢复和滚动路径，完成回归及 gstack browse 真实布局验证。
- 已完成客户端修复：按步骤缓存、查看/执行分离、开始/完成事件条件刷新、进度高亮、阅读位置记忆、状态轮询同步和旧会话事件过滤。
- 首批 7 条回归在旧实现全部失败；最终 Node 测试 71/71 通过（新增 10 条）；前端契约 19/19 通过。
- gstack browse 使用实际客户端标记/脚本的离线夹具回放碎片 SSE，在 1440×1000 与最终版本的 1024×768 下分别 16/16 通过。覆盖 20 次步骤往返、生成中/未开始提示、后续步骤、最终正文替换、工具栏输入保留、长文跟随/上翻/恢复和滚动区域隔离，无 JS 异常。截图在 .gstack/client-workflow-fixed*.png。
- Python 全量回归：原 .venv 启动器目标不存在，改用可用的 Conda CPython 3.13.13 + 项目原 .venv/Lib/site-packages；沙箱外、MYSQL_HOST=127.0.0.1 / MYSQL_PORT=1 下运行 `pytest tests -q --ignore=tests/rag_eval`：906 passed，68.48s。已有 Pydantic/datetime 弃用警告，未出现失败。
- git diff --check 通过。保持既有问卷变更；仅修改客户端行为、回归与本轮记录；gstack 自动增加 .gitignore 中的 .gstack/ 忽略项。浏览器使用回放的 SSE 数据，未调用真实模型。

## 基线和分批验收

- 基线使用 CPython 3.11 编译 Git 跟踪及可见未跟踪的全部 Python 文件。47 个文件中仅比较模块第 258 行失败。移除孤立引号后为 47/47。加入两个验证文件后为 49/49。
- 仓库虚拟环境的原始解释器路径失效。以临时 CPython 3.13.14 加原有 `.venv/Lib/site-packages` 验证真实应用导入。依赖安装下载使用独立临时目录。工作区原有虚拟环境配置予以保留。
- 从 Git 基线导出副本，仅叠加语法修复和第一批隔离测试。结果为 2 通过、11 失败。权限批修改后同组 13 通过。额外的清理基线用例复现预期删除 1 个、实际删除 0 个。
- Windows 沙箱内的 TestClient 停在 asyncio 本地套接字创建。经批准在沙箱外运行同一套测试，数据库和文件均指向独立测试目录。最终 28 项通过。另有 3 项 Node SSE 用例、8 个 HTML 内联脚本语法检查及 Docker 入口 Bash 语法检查通过。

## 可重跑的命令

```powershell
python -m pytest -q tests/test_engineering_regressions.py
node --test tests/test_workflow_sse.cjs
python tests/run_isolated_smoke.py
python tests/run_isolated_smoke.py --mysql-binary <mysqld.exe 的本机路径>
bash -n docker-entrypoint.sh
git diff --check
```

Windows 本轮使用临时 Python 3.13，`PYTHONPATH` 指向仓库原有 site-packages，并通过 `pytest --basetemp` 与隔离回放的 `--work-dir` 指定不同临时目录。独立回放默认使用 SQLite。提供 MySQL 可执行文件时，脚本以 `--no-defaults` 创建全新数据目录，只绑定随机回环端口，并在退出时停止测试实例。验证脚本里的原地址会在回放时限制到这个隔离进程。

- `test_all_apis.py` 通过 46/46，实际执行普通用户分组。
- `frontend_e2e_check.py` 通过 63/63，使用合成头像。
- `wf_e2e.py` 通过 35/35。执行真实 14 步 LangGraph 编排，步骤模型输出为固定数据。
- `wf_mutation_check.py` 退出 0，验证章节、模板、评分和模块写入。
- `sse_stream_probe.py` 退出 0。MySQL 回放中前三步约在 0.5、1.0、1.6 秒分别到达，停止读取后数据库最终状态为 `completed`。
- 审批独立用例使用真实 LangGraph `interrupt()` 和 MemorySaver，验证所有者和管理员均可恢复，另一普通用户拒绝，重复调用返回 409。

## 量化对照

- 上传微基准以真实临时文件模拟 multipart 完成后的输入，比较原来的整文件读取和 `_store_upload`。每个场景 5 次，取 tracemalloc 峰值与 perf_counter 耗时中位数，范围仅含应用保存函数。
  - 64 MiB 超限且已知大小，64.009 MiB → 0.001 MiB，20.775 ms → 0.033 ms。
  - 64 MiB 超限且无大小元数据，64.009 MiB → 2.009 MiB，22.293 ms → 34.081 ms。新路径在超限前分块写入临时目标，随后清理。
  - 49 MiB 合法文件，49.009 MiB → 2.009 MiB，49.195 ms → 30.581 ms。
- 旧 SSE 路径每发送一个步骤就查一次工作流记录，14 步最多 14 次进度 SELECT。新路径的进度发射不查数据库，单次终态更新在后台执行。回归监听实际 SQL，成功和失败各确认只有 1 次终态 UPDATE。鉴权、登记、模型用量和独立状态查询不计入这一项。
- 真实模型 Token、代理缓冲和 OCR 响应时间留待对应环境验证。当前数字来自隔离样本，峰值不是 HTTP 请求全链路或进程 RSS。

## 第二轮 · 工作流质量验收

- `python -m pytest -q tests/test_engineering_regressions.py` 为 34 项通过，包含真实 LangGraph 长步骤、失败重试、审批和恢复，以及评分、证据、问卷版本和报告复核的固定样例。长步骤测试缩短测试心跳周期，确认等待时仍收到心跳。
- `node --test tests/test_workflow_sse.cjs` 为 5 项通过，覆盖跨字节 UTF‑8 分片、多个事件、重复事件、心跳、连接中断和 CSV 转义。另有 50 个 Python 文件、8 个 HTML 内联脚本及 `git diff --check` 通过。
- `python tests/run_isolated_smoke.py --work-dir .venv` 在 SQLite 临时库运行五个原脚本。API 46/46，前端联通 63/63，工作流 35/35，写入检查和探针退出 0。探针在约 0.6、0.8、1.3 秒收到前三步，断开后状态最终为 completed。
- 固定模型的完整 SSE 从请求发起到终止事件为 1.612 秒，前端估算输出 Token 为 1,255。旧版记录估算 1,213，旧计时在收到响应头后开始并舍入到秒，因此两轮耗时不能直接对比。估算随提示和报告正文变化，真实供应商 Token 另行验证。
- 同一脱敏指标样例中，旧生成路径接受固定模型输出的 4 道题，1 道重复、1 道对应未知 ID，并调用模型 1 次。新路径生成 1 道符合条件的独立题，0 道重复、0 道未知 ID，模型调用为 0。其余同义项、复合项和仅凭台账核实的指标按规则排除。
- 同一脱敏资料检索样例中，项目预算 PDF 第 2 页的重复段落只引用一次，同时命中 1 条当前步骤规则，排除异步骤及停用条目。空检索返回资料不足提示。
- 同一评分样例满分分配为 50+50，第一项人工评分 0，第二项缺资料。旧汇总按两个满分得到 100，新汇总保留 0 和未评分，综合等级为待核实。回归另确认越界分值拒绝、通用规则不能代替项目评分证据。
