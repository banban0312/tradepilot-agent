# TradePilot 项目文档

实现代码统一放在仓库根目录的 `code/`，本目录集中维护需求、设计和原型资料。

| 文档 | 内容 |
|---|---|
| [PRD](./PRD.md) | 业务目标、功能需求、权限、业务规则与验收标准 |
| [技术 Spec](./SPEC.md) | 系统架构、工作流、工具契约、数据模型、API 与实现任务 |
| [本地运行、演示与交付指南](./DELIVERY-GUIDE.md) | Compose 快速启动、账号、演示路线、评测、备份恢复、清理与限制 |
| [验收证据索引](./ACCEPTANCE-EVIDENCE.md) | 六项核心能力、FR/AC/NFR、25 个原型画板与测试入口映射 |
| [原型说明](./design/README.md) | 25 个功能及状态画板、视觉规范与阅读路径 |
| [验收覆盖表](./design/ACCEPTANCE-COVERAGE.md) | 28 项 P0/P1 功能、27 条 AC 与画板逐项对应 |
| [本地开发环境](./development/LOCAL-ENVIRONMENT.md) | uv、Node.js、Compose 启动及 issue 验证命令 |
| [工程技术基线](./development/TECHNICAL-BASELINE.md) | 依赖锁定、MCP/PDF 授权选择与兼容验证证据 |
| [数据库迁移与演示数据](./development/DATABASE-BASELINE.md) | Alembic 初始化、基础约束与可重复主数据 |
| [登录会话与授权基线](./development/AUTH-BASELINE.md) | Cookie 会话、CSRF、角色与部门范围 |
| [前端导航与通用界面状态基线](./development/FRONTEND-BASELINE.md) | 登录导航、会话保护、通用反馈与尚未接入功能边界 |
| [草稿领域与版本基线](./development/DRAFT-BASELINE.md) | 候选/有效草稿、Decimal 金额、来源证据与乐观锁 |
| [合同与字段规则预检基线](./development/CONTRACT-RULES-BASELINE.md) | 合同关联、规则校验、额度预览与只读边界 |
| [可观测性持久化基础](./development/OBSERVABILITY-BASELINE.md) | Trace/Span、顺序事件、脱敏审计与运行轨迹留存边界 |
| [持久任务队列与恢复基线](./development/TASK-QUEUE-BASELINE.md) | 任务/命令/作业、advisory lock、execution epoch、检查点 fencing 与恢复验证 |
| [MCP 服务与工具契约](./development/MCP-BASELINE.md) | Streamable HTTP、严格 Schema、签名身份上下文、追踪及验证边界 |
| [可编辑原型](./design/ui.pen) | 使用 VS Code 的 Pen 扩展打开 |
| [原型 PDF](./design/exports/TradePilot-Prototype-v0.1.pdf) | 总览、设计规范与 25 个画板，共 27 页 |
| [原型清单](./design/manifest.json) | 画板编号、节点 ID 与图片路径 |

原型中的业务数据、轨迹和评测成绩都是示例。界面覆盖与实现验收分别记录，原型不替代后端权限、恢复和并发测试。
