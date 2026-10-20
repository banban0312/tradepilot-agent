# TradePilot

基于 LangGraph 的多模态智能商贸提单 Agent。项目包含 Vue 3 工作台、FastAPI 后端、持久 Worker、Streamable HTTP MCP 服务、PostgreSQL 数据库，以及固定集自动评测。

> 当前处于本地演示与验收交付阶段。M1–M6 前 41 个 issue 已合并，M6-07 文档与演示汇总正在进行。M6-06 的 8 小时稳定性观察按用户决定提前停止，约运行 2 小时，**没有完整 8 小时报告**。本地验收数据不代表生产 SLA 或生产就绪认证。

## 快速启动

环境要求：Windows PowerShell 7、Docker Desktop（Linux 容器）、Docker Compose v2、`uv`、Python 3.12、Node.js 22.12+。

从仓库根目录：

```powershell
if (-not (Test-Path code/backend/.env)) {
    Copy-Item code/backend/.env.example code/backend/.env
}
```

编辑 `code/backend/.env`，设置业务数据库和评测数据库各自的强口令及连接 URL。仅本机演示时，将 `DEMO_MODE=true`、`DEMO_PASSWORD` 设置为至少 12 位本机密码，并设置 `APP_ORIGIN=http://localhost:5173`、`SESSION_COOKIE_SECURE=false`。不要提交 `.env` 或使用默认凭据。

```powershell
uv python install 3.12
uv sync --project code/backend --locked
Push-Location code
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml up --build --detach --wait
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml run --rm --no-deps api uv run --frozen alembic upgrade head
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml run --rm --no-deps api uv run --frozen python -m app.seed
Pop-Location
```

页面：<http://localhost:5173/>。本机示例账号为 `demo-operator-north`、`demo-operator-south`、`demo-approver-north` 和 `demo-admin`，密码是你设置的 `DEMO_PASSWORD`。管理员不自动拥有业务审批权限。在线模型未配置时，系统会明确显示未配置；离线功能和固定评测仍可使用。

停止演示服务并保留数据：

```powershell
Push-Location code
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml down
Pop-Location
```

仅在确定要删除本机 TradePilot 演示数据库和附件时，才给该命令加 `--volumes`。不要执行全局容器、卷或镜像清理。

## 可复验场景

以下命令在随机隔离 Compose 项目和合成数据中执行，只清理本次验证资源：

```powershell
# 完整离线评测，报告为 JSON/JSONL/Markdown
pwsh -NoProfile -File code/scripts/verify_issue_037.ps1

# M6-06 API、XLSX、任务恢复、浏览器兼容与备份还原短验收
pwsh -NoProfile -File code/scripts/verify_issue_041.ps1 -SkipSoak

# M6-07 按场景演示文本提单、OCR/额度、Excel 部分成功、双模式/轨迹/评测
pwsh -NoProfile -File code/scripts/demo_issue_042.ps1 -Scenario All
```

真实模型验收需在本机 `.env` 配置模型供应商、模型名和密钥，显式运行 `pwsh -NoProfile -File code/scripts/verify_issue_014.ps1 -LiveModel`。该命令会调用外部模型并可能产生费用；模型结果与离线夹具报告分开保存。

## 主要功能

- LangGraph 持久任务、输入/审批中断和 Worker 重启恢复。
- Function Calling 与真实 MCP 工具调用，工具参数使用严格 Schema 校验。
- PDF/图片 OCR 与字段来源证据、Excel 分组和批次部分成功。
- 草稿版本、独立人工审批、合同额度校验、幂等订单提交。
- 全屏工作台与悬浮窗共享任务状态，历史查询、运行轨迹和操作审计。
- 六类固定评测数据集、离线/在线隔离运行、失败案例追踪及报告下载。

这些能力通过仓库测试和隔离验收验证。静态原型和合成数据不等同于真实服务、真实客户数据或生产部署证明。

## 文档入口

- [本地运行、演示与交付指南](doc/DELIVERY-GUIDE.md)
- [PRD、AC、NFR、原型与测试证据索引](doc/ACCEPTANCE-EVIDENCE.md)
- [PRD](doc/PRD.md)、[技术 Spec](doc/SPEC.md)
- [原型说明与 25 个画板](doc/design/README.md)
- [原型验收覆盖表](doc/design/ACCEPTANCE-COVERAGE.md)
- [后端环境与 issue 验收命令](doc/development/LOCAL-ENVIRONMENT.md)
- [自动评测报告口径](doc/development/EVALUATION-REPORTS.md)
- [持续开发与交付规范](doc/development/ISSUE-DELIVERY.md)

## 技术栈

Python 3.12、`uv`/`uv.lock`、FastAPI、LangGraph、SQLAlchemy、Alembic、PostgreSQL、Vue 3、TypeScript、Vite、Node.js 22、Docker Compose、MCP SDK、Tesseract。

## 已知限制

- 八小时长稳未完成；观察按用户决定在约两小时后停止。
- 本机性能数据和浏览器矩阵不代表生产容量或 SLA；API 性能样本只覆盖指定查询端点。
- 在线模型验收样本有限，不能据此推断普遍模型准确率。
- 尚未验证生产 HTTPS、客户数据迁移、真实 ERP 对接、生产数据规模或合规认证。
- OCR/超额规则均有各自集成测试，但当前演示没有证明同一张图片经在线 Agent 完成全链路超额提单拦截。

不得将未经验证的模型输出直接作为真实交易决策。正式提交必须由有权限的人员审批，服务端再次校验合同、权限、版本与额度。
