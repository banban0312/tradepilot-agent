# TradePilot

### Enterprise Agentic Trade Operations Platform

**基于 LangGraph 的企业级多模态智能商贸提单 Agent**

TradePilot 是一个面向大宗物资贸易和企业供应链业务的 AI Agent 应用平台。项目通过大语言模型、LangGraph 工作流编排、多模态文档解析和企业业务工具调用，实现从自然语言需求理解、合同信息查询、物料标准化映射，到订单草稿生成、履约额度校验、人工审批和正式提单的完整业务闭环。

TradePilot is an enterprise AI agent platform designed for intelligent trade order processing and supply chain operations. Powered by LangGraph, LLM tool calling, and multimodal document understanding, it orchestrates business workflows from natural language interpretation to contract validation, order generation, human approval, and execution.

> **Project Status: Planning & Development**
>
> 本项目目前处于规划与开发阶段。本文描述的是目标架构与计划功能，具体实现进度将随开发持续更新。

---

## 项目目录与文档

```text
tradepilot-agent/
├── code/                         # 项目实现代码、测试、评测脚本与运行配置
├── doc/
│   ├── README.md                 # 文档导航
│   ├── PRD.md                    # 产品需求与验收标准
│   ├── SPEC.md                   # 技术设计与实现任务
│   └── design/
│       ├── ui.pen                # VS Code Pen 可编辑原型
│       ├── README.md             # 原型说明与页面索引
│       ├── ACCEPTANCE-COVERAGE.md # 功能与验收覆盖表
│       ├── manifest.json         # 画板节点与导出文件清单
│       ├── exports/              # 正式 PNG 与 PDF 导出
│       └── review/               # 绘制过程中的视觉审阅图片
├── .gitignore
└── README.md
```

从 [PRD](./doc/PRD.md)、[技术 Spec](./doc/SPEC.md) 和 [原型说明](./doc/design/README.md) 开始阅读。后续前端、后端、MCP、测试、评测与部署实现统一放入 `code/`；需求、设计和原型资料统一放入 `doc/`。

## ✨ 核心特性 | Key Features

### 🤖 Agentic Workflow — 智能业务工作流

基于 LangGraph 构建具有状态管理和条件路由能力的 Agent 工作流。

- 自然语言业务意图识别与订单实体提取
- 多轮对话补全缺失的业务字段
- 基于 Function Calling 的业务工具调用
- 订单创建任务状态持久化与异常恢复
- Human-in-the-loop 人工审批与执行控制
- Agent 执行轨迹记录和业务操作审计

### 📄 Multimodal Understanding — 多模态单据理解

统一处理自然语言、合同文档、单据图片及 Excel 文件。

- 自然语言采购和销售订单解析
- 发货单、签收单和采购合同 OCR 识别
- Excel 批量订单数据解析和字段校验
- 非标准物料名称识别与编码映射
- 结构化字段提取与人工纠错

### 🛠️ Tool Calling — 企业业务工具调用

通过标准化工具接口连接企业商贸业务系统。

- 客户与供应商信息查询
- 商贸合同与工程项目查询
- 标准物料信息检索
- 合同剩余额度计算
- 销售和采购订单草稿管理
- 订单提交、状态查询及履约追踪

### 🛡️ Business Validation — 业务规则与安全校验

通过确定性规则引擎保障关键业务操作的可靠性。

- 合同金额与数量额度核验
- 必填字段和业务数据完整性检查
- 订单重复提交防护与幂等性控制
- 部门级数据权限隔离
- 超额订单预警与审批流转
- 关键操作审计与异常记录

### 🖥️ Dual-Mode Workspace — 双模式智能工作台

提供完整的企业商贸业务交互体验。

- 全屏智能提单工作台
- 商贸系统内嵌 AI 悬浮窗
- 对话内容与订单草稿双向联动
- 合同履约额度可视化
- 批量订单导入与异常数据处理
- 订单详情查询与业务操作追踪

---

## 🏗️ 系统架构 | Architecture

项目计划采用前后端分离架构，以 LangGraph 作为 Agent 编排核心，以 FastAPI 提供业务接口，通过独立的业务工具层连接数据库和合同、订单等业务能力。

```text
                  TradePilot
                      |
          ┌───────────┴───────────┐
          |                       |
   Fullscreen Workspace     Floating Widget
          |                       |
          └───────────┬───────────┘
                      |
                 FastAPI API
                      |
               LangGraph Runtime
                      |
          ┌───────────┼───────────┐
          |           |           |
    Intent Parser  Tool Router  State Manager
          |           |           |
          └───────────┼───────────┘
                      |
              Business Tool Layer
                      |
       ┌──────────────┼──────────────┐
       |              |              |
 Contract Service  Material API  Order Service
       |              |              |
       └──────────────┼──────────────┘
                      |
                 PostgreSQL
```

### Agent 工作流设计

```text
User Input
    |
    v
Intent Recognition
    |
    v
Entity Extraction
    |
    +---- Missing Fields?
    |          |
    |          v
    |     Clarification
    |          |
    +----------+
    |
    v
Business Tool Calling
    |
    v
Contract & Material Validation
    |
    +---- Validation Failed?
    |          |
    |          v
    |     Correction / Review
    |          |
    +----------+
    |
    v
Order Draft Generation
    |
    v
Human Approval
    |
    +---- Rejected --> Revise / Cancel
    |
    v
Idempotent Order Submission
    |
    v
Result & Audit Logging
```

关键设计原则：

1. **LLM 负责理解与决策辅助**：处理自然语言、字段提取及工具选择。
2. **确定性代码负责业务约束**：金额计算、权限校验和订单状态管理不由模型自由决定。
3. **关键操作需要人工确认**：正式订单提交必须经过授权用户审批或确认。
4. **所有执行操作可追溯**：记录 Agent、业务工具和人工审批的关键步骤。

---

## 🧰 技术栈 | Tech Stack

以下为计划采用的技术选型，开发过程中可能根据实际需要调整。

| Category | Technology |
|---|---|
| Language | Python, TypeScript |
| Agent Framework | LangGraph |
| LLM Integration | OpenAI-compatible API |
| Backend | FastAPI, Pydantic, SQLAlchemy |
| Frontend | Vue 3, TypeScript, Tailwind CSS |
| Database | PostgreSQL |
| Cache | Redis |
| OCR & Documents | OCR Adapter, PyMuPDF, openpyxl |
| Observability | LangSmith / Langfuse |
| Testing | pytest |
| Deployment | Docker, Docker Compose |

---

## 🔄 核心业务场景 | Use Cases

### 场景一：自然语言创建销售订单

用户输入：

> 给中建八局科技城项目下 200 吨 HRB400E 螺纹钢，走 2026-WZ-089 合同，后天送到一号工地。

计划执行流程：

1. Agent 识别销售订单意图。
2. 提取工程项目、合同编号、物料、数量与交期。
3. 查询合同及项目主数据。
4. 将物料信息映射到标准编码。
5. 计算本次订单金额和合同剩余额度。
6. 生成可编辑的销售订单草稿。
7. 等待用户确认后提交。
8. 返回正式订单编号和执行记录。

### 场景二：OCR 单据识别与合同超额预警

用户上传一张纸质发货签收单。

单据内容包含：

- 物料：P.O 42.5 水泥
- 数量：500 吨
- 订单金额：220,000 元
- 合同剩余额度：150,000 元

系统计划自动解析关键字段、关联物料编码，并触发合同额度不足预警。

超额订单不得直接进入普通提交流程，必须经过人工复核及符合权限要求的特批流程。

### 场景三：Excel 批量提单

用户上传包含多笔采购或销售订单的 Excel 文件。

系统逐行完成字段解析、合同关联、物料映射与业务校验，并提供：

- 正常订单批量预览
- 异常行高亮及原因说明
- 单行修改和重新校验
- 按审批规则批量提交
- 处理结果与失败原因追踪

---

## 🔐 安全与可靠性 | Safety & Reliability

TradePilot 计划从业务系统和 Agent 运行时两个层面保障执行安全。

**Agent 层面**

- 工具参数结构化校验
- 状态持久化与可恢复执行
- 工具调用超时及失败处理
- 高风险操作人工确认
- 模型输入与工具输出边界校验

**业务层面**

- 基于角色和部门的数据访问控制
- 服务端合同额度校验
- 并发提交额度一致性控制
- 数据库事务和幂等请求处理
- 订单状态流转约束
- 业务审计日志

所有涉及订单创建、审批和额度占用的核心约束均在服务端执行，不能仅依赖前端状态或大模型判断。

---

## 📊 Agent Evaluation — 智能体评测

项目计划建立覆盖典型业务与异常场景的自动化评测集。

| 评测维度 | 关注指标 |
|---|---|
| Intent Recognition | 订单意图识别准确率 |
| Entity Extraction | 关键字段提取正确率 |
| Tool Calling | 工具选择与参数正确率 |
| Task Execution | 端到端任务成功率 |
| Business Validation | 超额及非法订单拦截率 |
| Reliability | 重试恢复与幂等测试通过率 |
| Performance | 平均响应时间、Token 消耗 |

评测数据将区分模型调用结果、实际业务执行结果与人工审核结论，不使用未经验证的模拟结果作为真实性能指标。

---

## 📁 项目目录 | Project Structure

实现目录统一以 `code/` 为根，以下子目录在编码阶段逐步建立；`doc/` 中的需求、设计与原型资料已经形成。

```text
tradepilot-agent/
├── code/
│   ├── frontend/             # Vue 3 前端
│   │   └── src/              # 组件、页面、状态与接口
│   ├── backend/              # FastAPI、LangGraph 与 MCP
│   │   └── app/              # api、agent、tools、domain 等
│   ├── tests/                # 单元、集成、恢复与安全验证
│   ├── evals/                # Agent 评测集与运行脚本
│   ├── data/                 # 示例业务数据
│   ├── docker-compose.yml
│   └── .env.example
├── doc/
│   ├── PRD.md
│   ├── SPEC.md
│   └── design/               # ui.pen、覆盖表与导出图片/PDF
├── AGENTS.md                 # 协作与提交约定
└── README.md
```

---

## 🗺️ 开发路线 | Roadmap

### Phase 1 — Core Agent MVP

- [ ] 搭建 FastAPI 与 Vue 3 基础工程
- [ ] 设计商贸合同、物料和订单数据模型
- [ ] 实现自然语言意图识别和实体提取
- [ ] 实现 LangGraph Agent 工作流
- [ ] 集成合同、物料及订单业务工具
- [ ] 实现订单草稿生成和业务规则校验
- [ ] 完成人工确认及正式订单提交

### Phase 2 — Multimodal & Workflow

- [ ] 集成 OCR 单据解析
- [ ] 实现非标准物料名称纠错映射
- [ ] 实现 Excel 批量订单导入
- [ ] 支持悬浮窗和全屏工作台双模式
- [ ] 实现 Agent Checkpoint 与中断恢复
- [ ] 完善审批、幂等与审计能力

### Phase 3 — Engineering & Evaluation

- [ ] 建立自动化 Agent 评测集
- [ ] 增加执行轨迹与模型成本监控
- [ ] 完善异常恢复及并发测试
- [ ] 实现 Docker Compose 一键部署
- [ ] 完善 API 文档及项目架构说明
- [ ] 制作端到端业务演示视频

---

## 🚀 Quick Start

项目基础工程完成后，将在此补充可直接执行的安装及启动步骤，包括：

1. 克隆仓库并安装前后端依赖。
2. 使用 `code/.env.example` 创建本地环境配置。
3. 按技术 Spec 启动 PostgreSQL 与应用服务；Redis 为后续可选组件。
4. 初始化示例业务数据。
5. 启动前端并访问 TradePilot 工作台。
6. 运行后端测试及 Agent 评测。

当前尚未提供经过验证的启动命令，避免产生与实际项目结构不一致的使用说明。

---

## 📚 Documentation

计划维护以下文档：

- Architecture — 系统架构和 Agent 状态图
- API Reference — 后端业务接口说明
- Agent Design — 工作流与工具定义
- Database Design — 数据库和业务实体设计
- Evaluation — 自动化评测方案与结果
- Deployment — 环境配置与部署说明

---

## ⚠️ Disclaimer

本项目主要用于 AI Agent 应用开发、软件工程实践及技术研究。

演示数据、合同与业务主体均可使用虚构或脱敏信息。模型生成的内容需要经过业务规则校验及必要的人工审核，不应将本项目未经验证的输出直接用于真实企业交易决策。

---

## 📄 License

License to be determined.

---

**TradePilot — From Natural Language to Reliable Business Execution.**

*让 AI 不止理解业务，更能安全、可靠地执行业务。*
