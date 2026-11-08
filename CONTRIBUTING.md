# 贡献指南

感谢你愿意参与云海睿评。本文说明如何搭建环境、提交改动，以及这个项目对「什么算改好了」的判定标准。

- [行为准则](#行为准则)
- [开发环境](#开发环境)
- [分支与提交](#分支与提交)
- [可以做什么](#可以做什么)
- [改动的验收标准](#改动的验收标准)
- [提交 PR](#提交-pr)
- [报告问题](#报告问题)

---

## 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)。
技术分歧可以有，对人不对事不行。

---

## 开发环境

```bash
git clone https://github.com/Qorq-byte/AI_Grade.git
cd AI_Grade

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt
cp .env.example .env              # 至少填一个 LLM API Key 与 MySQL 连接
python -m backend.main
```

- Python 3.9+（Docker 镜像为 3.11），MySQL 5.7+/8.0；
- 前端是原生 HTML/CSS/JS，**没有构建步骤**，改完刷新即可；
- 需要 node 18+ 才能跑前端协议测试（`node --test tests/*.cjs`）。

---

## 分支与提交

从 `main` 切分支，命名建议：

| 前缀 | 用途 | 示例 |
|------|------|------|
| `feat/` | 新功能 | `feat/section-anchor` |
| `fix/` | 缺陷修复 | `fix/repair-shrink` |
| `test/` | 只动测试 | `test/mutation-step7` |
| `docs/` | 只动文档 | `docs/readme-refresh` |
| `chore/` | 构建、依赖、脚本 | `chore/gitignore` |

提交信息用 [Conventional Commits](https://www.conventionalcommits.org/)，**正文写清「为什么改」**：

```
fix(scoring): 弱定位不再计入总分

原实现把「行号存在」当成「已落地」，但实测 b20e9f85 那轮 4 条「第1段」
里有 3 条 reason 自写「无法评价、暂按 1.00 分基础分」。
放行会让总分从 30.0 抬到 33.0——数字更好看，但是假的。
```

一个提交只做一件事。**不要**把格式化、重命名和功能改动混在一起。

---

## 可以做什么

欢迎的方向（按价值排序）：

1. **检索质量**：`tests/rag_eval` 有量化基线，能在 Recall@3 / MRR 上做出可复现提升的改动最有价值；
2. **结构化输出的鲁棒性**：更多失败形态的识别与恢复，但必须保持「不静默降级」；
3. **超长文档**：分块 map-reduce，替换当前的「头尾预算 + 摘要」策略；
4. **可观测性**：`/metrics`、结构化日志、成本与耗时归因；
5. **部署形态**：Redis 版 `progress_bus` 与可插拔 checkpointer，支持多 worker；
6. **文档与示例**：真实使用场景、截图、常见报错排查。

---

## 改动的验收标准

这个项目对「改好了」有三条硬标准，PR 会被按此检查：

### 1. 不削弱断言

不允许为了「让测试变绿」而放宽断言、跳过用例或删除边界场景。
如果原断言不合理，请在 PR 里说明**为什么原来那条是错的**，并同时补一条更强的新断言。

### 2. 向后兼容

- 接口路径、SSE 事件名与语义、数据库字段**不得修改或删除**，只允许向后兼容地新增；
- 新增状态字段时，要能处理「老会话没有该字段」的情况（不能抛异常）；
- 改判定口径时，请给出「同一份输入，改前/改后的对比」。

### 3. 用数字说话

涉及效果的改动，请附可复现的验证，而不是「感觉更准了」：

```bash
# 单测与回归
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval
node --test tests/*.cjs

# 检索效果（有固定语料与阈值）
.venv/Scripts/python -m tests.rag_eval.run --check

# 真实 14 步端到端（需要 LLM 密钥，约 4~5 分钟）
.venv/Scripts/python wf_e2e.py
```

如果改动无法在本地验证（需要特定材料、特定模型、生产环境），**请在 PR 里明说**，
不要用「应该没问题」代替结论。

---

## 提交 PR

1. 确认 `pytest` 与 `node --test` 全绿；
2. 更新受影响的文档（`README.md` 或 `docs/API.md`）；
3. PR 描述里包含：**问题 → 做法 → 验证方式 → 剩余风险**；
4. 涉及界面改动请附截图；涉及判定口径改动请附改前/改后对比；
5. 一个 PR 只解决一个主题，便于 review 与回滚。

CI 目前尚未配置（见 Roadmap），因此**本地验证结果就是唯一的证据**，请如实填写。

---

## 报告问题

- 缺陷、功能建议：走 [Issues](https://github.com/Qorq-byte/AI_Grade/issues)，请附复现步骤与日志中的 `request_id`；
- 安全漏洞：**不要**公开提 Issue，见 [SECURITY.md](./SECURITY.md)。

提问前请先看 README 的「常见问题」与「能力边界」两节——很多「结果不对」属于已知的模型能力边界，
而不是程序缺陷。
