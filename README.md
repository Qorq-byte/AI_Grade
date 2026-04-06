# 云海睿评 - AI绩效评价报告生成系统

> 基于大模型 + 提示词 + 知识库的智能绩效评价报告生成系统，通过 14 步标准化流程自动化完成从资料整理到最终报告导出，将传统耗时数周的绩效评价工作压缩至分钟级。

---

## 目录

- [项目介绍](#项目介绍)
- [核心功能](#核心功能)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [环境配置](#环境配置)
- [使用指南](#使用指南)
- [14步工作流详解](#14步工作流详解)
- [管理端功能](#管理端功能)
- [数据库设计](#数据库设计)
- [API接口概览](#api接口概览)
- [安全与权限](#安全与权限)
- [数据保留策略](#数据保留策略)
- [测试与验证](#测试与验证)
- [常见问题排查](#常见问题排查)
- [许可证](#许可证)

---

## 项目介绍

**云海睿评** 面向财政、审计、人大等部门绩效评价业务场景，覆盖项目支出、部门整体、下级政府运行、专项债、社保基金等评价类型。系统自动完成资料清单整理、核心信息提取、三级指标体系构建、分值分配、评分标准制定、指标分析、问题诊断、改进建议、综合评价、报告组装，并支持一键导出符合规范的 Word / PDF 报告。

### 核心定位
- **项目名称**: 云海睿评
- **版本**: v1.0.0
- **开发语言**: Python 3.9+
- **架构模式**: FastAPI + LangGraph + MySQL + ChromaDB

---

## 核心功能

1. **智能文件解析**: 支持 PDF / Word / Excel / TXT / 图片等多格式文件上传、批量上传与内容提取
2. **14步标准化流程**: 自动完成资料清单 → 核心信息提取 → 指标体系 → 分值 → 评分标准 → 指标体系表 → 指标分析 → 经验做法 → 问题分析 → 改进建议 → 综合评价 → 基本情况 → 评价工作简介 → 报告组装
3. **三级指标体系**: 自动构建三级绩效评价指标体系，支持 6 种指标类型（项目支出 / 部门整体 / 下级政府运行 / 专项债 / 社保基金 / 自定义），所选类型真实影响指标体系生成
4. **多视角分析**: 支持财政、人大、审计等多视角分析
5. **智能问卷生成**: 基于指标体系自动生成里克特 5 级量表问卷，支持在线填写、提交与 CSV 导出
6. **一键导出报告**: 支持 **真实 Word (.docx)** 与 **真实 PDF (.pdf)** 双格式导出（PDF 由 reportlab 原生生成，含中文排版），支持经典排版与自定义排版（字体/字号/行距/页边距）
7. **多模型支持**: 通义千问、DeepSeek、OpenAI、Anthropic，单模型 / 多模型对比两种模式
8. **SSE 实时流式进度**: 工作流步骤实时推送（step_start / step_complete / token_update / questionnaire_ready / complete / error）
9. **AI辅助对话**: 每个步骤支持多轮 AI 对话优化，可人工修改生成内容
10. **报告编辑与组装**: 报告编辑器支持在线修改、保存、章节拖拽排序、格式校对、逻辑校对
11. **管理端**: 用户、模型、提示词、知识库、模板、工作流监控、审计日志、文件管理、数据清理、系统设置
12. **Token追踪**: 实时记录各步骤 Token 消耗，支持用量统计与成本分析
13. **数据持久化与重启恢复**: 工作流会话落库，报告保存至数据库与磁盘备份，服务器重启后仍可查看、导出、编辑历史报告

---

## 技术栈

### 后端框架
- **Web框架**: FastAPI >= 0.109.0
- **ASGI服务器**: Uvicorn >= 0.27.0
- **数据验证**: Pydantic >= 2.5.0, Pydantic Settings >= 2.1.0

### AI与大模型
- **LangChain**: langchain >= 0.1.0, langchain-core >= 0.1.0, langchain-community >= 0.0.20
- **LangGraph**: langgraph >= 0.0.20（状态图引擎，MemorySaver 断点续跑）
- **LangSmith**: langsmith >= 0.0.80（可选追踪）
- **支持的LLM提供商**:
  - 通义千问 (DashScope): qwen-turbo, qwen-plus, qwen-max, qwen-long
  - DeepSeek: deepseek-chat, deepseek-coder, deepseek-v4-flash
  - OpenAI: gpt-3.5-turbo, gpt-4, gpt-4-turbo, gpt-4o
  - Anthropic: claude-3-opus, claude-3-sonnet, claude-3-haiku, claude-2.1

### 向量数据库与RAG
- **ChromaDB**: chromadb >= 0.4.22（向量存储）
- **FAISS**: faiss-cpu >= 1.7.4（相似度搜索）
- **Sentence Transformers**: sentence-transformers >= 2.3.1（文本嵌入）

### 数据库
- **关系型数据库**: MySQL（SQLAlchemy >= 2.0.0, PyMySQL >= 1.1.0）
- **迁移工具**: Alembic >= 1.13.0（可选）
- **缓存**: Redis >= 5.0.1（可选）

### 文件处理与导出
- **Word文档**: python-docx >= 1.1.0
- **Excel表格**: openpyxl >= 3.1.2
- **PDF生成**: reportlab >= 4.0.0
- **PDF解析**: PyPDF2 >= 3.0.1
- **图片OCR**: Pillow >= 10.2.0, pytesseract >= 0.3.10
- **通用解析**: docx2txt >= 0.8, unstructured >= 0.12.0

### 认证与安全
- **JWT认证**: python-jose[cryptography] >= 3.3.0
- **密码加密**: passlib[bcrypt] >= 1.7.4

### 前端技术
- **纯HTML/CSS/JavaScript**: 无框架依赖，轻量级实现
- **图标库**: Lucide Icons
- **字体**: Inter, Helvetica Now Display Bold

---

## 项目结构

```
AI_Grade/
├── backend/                          # 后端代码
│   ├── agents/                       # Agent核心模块
│   │   ├── base_agent.py             # Agent基类（多模型支持、记忆管理）
│   │   ├── performance_agent.py      # 绩效评价Agent（14步工作流，LangGraph编排）
│   │   ├── states/                   # 状态定义
│   │   │   └── performance_state.py  # 绩效评价状态模型（TypedDict）
│   │   ├── tools/                    # 工具模块
│   │   │   ├── content_indexer.py    # 内容索引器
│   │   │   ├── score_validator.py    # 分数校验器（四维校验）
│   │   │   ├── indicator_manager.py  # 指标管理器（内置模板）
│   │   │   ├── multi_model_compare.py # 多模型对比
│   │   │   ├── questionnaire_gen.py  # 问卷生成器
│   │   │   └── export_service.py     # 导出服务（Word / 真实PDF）
│   │   └── prompts/                  # Prompt模板管理
│   │       ├── prompt_manager.py     # Prompt管理器（DB优先，默认兜底）
│   │       └── default_prompts.py    # 默认Prompt模板
│   ├── routes/                       # API路由
│   │   ├── agent_routes.py           # Agent工作流接口（SSE流式、问卷、导出、组装等）
│   │   ├── auth_routes.py            # 认证接口（登录/注册/刷新）
│   │   ├── admin_routes.py           # 管理端接口
│   │   └── client_routes.py          # 客户端接口（报告/对话/通知/AI聊天）
│   ├── services/                     # 业务服务
│   │   ├── auth_service.py           # 认证服务
│   │   ├── file_processor.py         # 文件处理服务
│   │   ├── memory_service.py         # 记忆服务
│   │   ├── rag_service.py            # RAG检索增强服务
│   │   ├── token_service.py          # Token消耗记录服务
│   │   └── cleanup_service.py        # 数据清理服务（90天过期策略）
│   ├── models/                       # 数据模型
│   │   ├── user_models.py            # 用户/项目/工作流/报告/Token/提示词/通知等
│   │   └── memory_models.py          # 记忆模型
│   ├── middleware/                   # 中间件
│   │   ├── auth_middleware.py        # JWT认证中间件（依赖注入）
│   │   └── rate_limit.py             # 限流中间件
│   ├── utils/                        # 工具函数
│   │   ├── llm_helper.py             # LLM辅助函数（模型/凭证管理）
│   │   └── log_utils.py              # 日志工具
│   ├── config.py                     # 配置管理（pydantic-settings）
│   ├── database.py                   # 数据库初始化
│   └── main.py                       # FastAPI应用入口（前端页面托管 + 路由注册）
├── frontend/                         # 前端页面（后端静态托管）
│   ├── index.html                    # 首页（落地页）
│   ├── login/index.html              # 登录注册页面
│   ├── client/index.html             # 客户端（14步工作流/AI聊天/报告/问卷/设置）
│   ├── admin/index.html              # 管理端
│   ├── workflow/index.html           # 独立工作流页面
│   ├── report/index.html             # 报告预览编辑页面
│   ├── questionnaire/index.html      # 问卷填写页面
│   └── avatars/                      # 用户头像存储
├── uploads/                          # 用户上传文件（90天保留）
├── templates/                        # 指标体系模板（indicator_templates.json）
├── state_backups/                    # 工作流状态磁盘备份（重启恢复）
├── backend/exports/                  # 导出报告存储目录
├── backend/data/chroma_db/           # ChromaDB向量数据库
├── backend/knowledge_base/           # 知识库文件
├── .env                              # 环境变量配置（不入库）
├── .env.example                      # 环境变量示例
├── requirements.txt                  # Python依赖
├── start_backend.bat                 # Windows 一键启动脚本（cmd）
├── start_backend.ps1                 # Windows 一键启动脚本（PowerShell）
├── test_all_apis.py                  # API 全功能测试
├── frontend_e2e_check.py             # 前端联通性检查（65项）
├── wf_e2e.py                         # 工作流端到端测试（需真实LLM密钥）
└── README.md                         # 项目说明文档
```

---

## 快速开始

### 1. 环境要求
- Python 3.9+
- MySQL 5.7+ / 8.0（或 MariaDB）

### 2. 创建虚拟环境并安装依赖

**Windows（cmd）:**
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**Windows（PowerShell）:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env` 并修改配置：

```bash
cp .env.example .env
```

编辑 `.env` 文件，至少配置以下项：

```env
# LLM API 密钥（至少配置一个）
DASHSCOPE_API_KEY=sk-your-dashscope-key
DEEPSEEK_API_KEY=your-deepseek-key
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key

# 数据库配置
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your-mysql-password
MYSQL_DATABASE=ai_grade_db

# JWT 配置（生产环境请使用随机生成的强密钥）
JWT_SECRET_KEY=your-random-secret-key

# 默认模型（建议配置为已具备有效密钥的模型）
DEFAULT_MODEL=deepseek-chat

# 模型参数
TEMPERATURE=0.7
TOP_P=0.9
MAX_TOKENS=4000
```

> **注意**：`DEFAULT_MODEL` 决定了未显式选择模型时的工作流/AI聊天默认模型，请务必设置为密钥有效的模型。

### 4. 创建数据库

在 MySQL 中执行：

```sql
CREATE DATABASE IF NOT EXISTS ai_grade_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

> 数据表会在首次启动时自动创建（`init_db`），默认管理员账号与系统提示词模板也会自动初始化。

### 5. 启动服务器

**方式一：一键脚本（Windows）**
```bash
start_backend.bat        # cmd
# 或
.\start_backend.ps1      # PowerShell
```

**方式二：手动启动**
```bash
python -m backend.main
# 或
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

服务器启动后访问 **http://localhost:8000**。

### 6. 快速验证

```bash
# 健康检查
curl http://localhost:8000/health
# → {"status":"healthy","service":"云海睿评 - AI绩效评价报告生成系统","version":"1.0.0"}
```

---

## 使用指南

### 页面入口

| 页面 | 路径 | 说明 |
|------|------|------|
| 首页 | `/` | 平台介绍与入口 |
| 登录/注册 | `/login` | 登录、注册、记住登录状态 |
| 客户端 | `/client` | 主工作台：绩效评价、AI助手、我的报告、问卷填写、个人设置 |
| 管理端 | `/admin` | 系统管理（仅管理员） |
| 独立工作流 | `/workflow` | 精简版工作流页面（需登录） |
| 报告编辑 | `/report?session=<会话ID>` | 报告在线预览与编辑 |
| 问卷填写 | `/questionnaire?session=<会话ID>` | 独立问卷填写页 |

