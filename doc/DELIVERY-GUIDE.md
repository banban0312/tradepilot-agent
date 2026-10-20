# TradePilot 本地运行、演示与交付指南

本指南面向 Windows + PowerShell 7 的开发/验收环境。后端 Python 依赖统一使用 `uv`，Compose 和验收脚本只操作明确命名的项目。此说明不代表生产部署认证或生产 SLA。

## 环境要求

- Windows 10/11、PowerShell 7、Git、Docker Desktop（Linux 容器）和 Docker Compose v2。
- Python 3.12 由 `uv` 管理；Node.js 22.12+、npm 10/11 用于前端。
- 第一次构建需要访问 Docker Hub、GHCR、npm 和 Python 软件包索引。
- 浏览器演示支持 Chrome/Edge；M6-06 兼容矩阵覆盖 Chrome/Edge 当前版及前一主版本。

## 1. 安装与启动

从仓库根目录运行。首次复制仅创建本机配置，不覆盖已经存在的 `.env`：

```powershell
if (-not (Test-Path code/backend/.env)) {
    Copy-Item code/backend/.env.example code/backend/.env
}
```

编辑 `code/backend/.env`：为业务数据库和评测数据库分别设置强随机密码，并同步更新对应连接 URL；本机浏览器演示设置 `DEMO_MODE=true`、`DEMO_ALLOW_SELF_APPROVAL=false`、`DEMO_PASSWORD` 为长度至少 12 的本机口令，`APP_ORIGIN=http://localhost:5173`，`SESSION_COOKIE_SECURE=false`。这些值仅适用于本机 HTTP；外部部署必须使用 HTTPS、Secure Cookie 和独立凭据。

Python 依赖可以在宿主机同步；Docker 服务启动时也会按锁文件使用 `uv`：

```powershell
uv python install 3.12
uv sync --project code/backend --locked
```

启动本机演示 Compose（项目名固定为 `tradepilot`，数据卷会保留）：

```powershell
Push-Location code
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml up --build --detach --wait
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml run --rm --no-deps api uv run --frozen alembic upgrade head
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml run --rm --no-deps api uv run --frozen python -m app.seed
Pop-Location
```

访问地址：

- 前端：<http://localhost:5173/>
- 登录：<http://localhost:5173/login>
- API 就绪：<http://localhost:8010/health/ready>
- MCP Streamable HTTP：<http://localhost:8001/mcp>

本地示例账号由 `DEMO_PASSWORD` 在 `DEMO_MODE=true` 时创建：`demo-operator-north`、`demo-operator-south`、`demo-approver-north`、`demo-admin`。均使用同一个 `.env` 演示口令；管理员不能代替业务经办人或审批人。关闭演示模式后，示例账号不能登录。

本地模型与 OCR 可留空；功能状态会显示未配置。需要在线模型演示时，在本机 `.env` 配置受支持的供应商、模型名、可选 API 地址及本机密钥，然后通过 `verify_issue_014.ps1 -LiveModel` 做真实调用验收。不要把密钥放进仓库、命令行截图或评测文档。

## 2. 演示路线

按顺序可展示这些真实实现；页面操作产生的数据只在演示数据库中：

1. **文本提单与人工审批**：以 `demo-operator-north` 输入销售需求，检查任务轨迹、合同/物料工具调用和 `WAITING_APPROVAL`；用独立的 `demo-approver-north` 批准当前草稿版本后再提交，回到订单中心核对正式订单。也可运行离线真实服务案例 `ambiguity-missing-spec`、`normal-approved-order`。验收案例各自在隔离评测范围执行，不共享同一 task。
2. **澄清与恢复**：输入缺规格的物料请求，观察 `WAITING_INPUT` 和澄清问题；提供规格后在原任务继续。测试入口：[`test_issue_013.py`](../code/tests/issues/test_issue_013.py)、[`test_issue_040.py`](../code/tests/issues/test_issue_040.py)。
3. **OCR 证据与超额阻断**：使用图片/PDF 解析入口观察原文、字段坐标和歧义候选；再用超额示例确认任务被规则阻断且正式订单、审批和占额均为零。当前固定验收把 OCR 证据与额度规则作为独立案例验证，不声称已有“同一张图片由在线模型自动识别并完成全链路超额拦截”的端到端结果。
4. **Excel 部分成功**：上传示例模板，正常分组可进入任务，缺字段组进入修正，超额组保持阻断；核对原始行号和数据库副作用。测试入口：[`test_issue_029.py`](../code/tests/issues/test_issue_029.py)、[`test_issue_030.py`](../code/tests/issues/test_issue_030.py)。
5. **双模式、轨迹与评测**：在全屏工作台与悬浮窗切换同一任务；打开任务历史与轨迹核对状态事件、模型/MCP 调用和人工等待；再以管理员打开评测中心，先跑离线固定集。在线评测须单独明确选择，并可能产生模型费用。

运行封装脚本可复现相应隔离验证：

```powershell
pwsh -NoProfile -File code/scripts/demo_issue_042.ps1 -Scenario TextToOrder
pwsh -NoProfile -File code/scripts/demo_issue_042.ps1 -Scenario OcrAndQuota
pwsh -NoProfile -File code/scripts/demo_issue_042.ps1 -Scenario ExcelPartial
pwsh -NoProfile -File code/scripts/demo_issue_042.ps1 -Scenario DualModeTraceEvaluation
```

一次运行全部场景：

```powershell
pwsh -NoProfile -File code/scripts/demo_issue_042.ps1 -Scenario All
```

脚本会调用现有 issue 验收入口；各入口使用随机 Compose 项目、端口和数据卷，成功/失败后只清理自己创建的资源。`All` 会重建多组隔离服务，耗时比单项长。评测报告留在 `code/artifacts/evals/`，浏览器/性能证据留在 `code/artifacts/performance/`；这些目录不提交。

### 关键验证命令

```powershell
# 完整 41 例离线评测（真实隔离 API/数据库/Worker/MCP，模型输出使用明确标记的离线夹具）
pwsh -NoProfile -File code/scripts/verify_issue_037.ps1

# 单独选择澄清、人工批准和额度边界案例
pwsh -NoProfile -File code/scripts/verify_issue_037.ps1 -CaseId ambiguity-missing-spec,normal-approved-order,quota-over

# 评测中心 API、权限、进度、报告下载、前端构建
pwsh -NoProfile -File code/scripts/verify_issue_039.ps1

# DeepSeek 或其他已配置供应商的真实模型 + MCP 流程；会产生服务调用用量
pwsh -NoProfile -File code/scripts/verify_issue_014.ps1 -LiveModel
```

后端单文件测试可使用：

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\code\backend);$(Resolve-Path .\code)"
uv run --project code/backend pytest code/tests/issues/test_issue_041.py -q
```

需要数据库/API/MCP 的用例必须经对应 `verify_issue_NNN.ps1`，不能在宿主机直接运行后把跳过的集成测试记作通过。

## 3. 迁移、种子、备份与恢复

- 新环境按启动段运行 `alembic upgrade head`，再运行 `python -m app.seed`。重复 seed 更新同一组保留 ID 的示例数据，不会清空业务表。
- 修改模型后由开发者审查并生成 Alembic migration；发布前先备份数据库，迁移不由应用启动时静默执行。
- PostgreSQL 和附件的协调备份、克隆库还原、记录计数、字段证据链接、文件 SHA-256 及 LangGraph 检查点续跑，使用 [`verify_issue_041.ps1 -SkipSoak`](../code/scripts/verify_issue_041.ps1) 在随机隔离项目执行。它不接触默认 `tradepilot` 项目。
- 不要把隔离验收当成生产备份演练；生产恢复应在单独目标数据库和附件目录验证，再切换业务流量。

## 4. 停止服务与清理

停止本机演示服务并保留 PostgreSQL/附件数据：

```powershell
Push-Location code
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml down
Pop-Location
```

只有确定要删除**本机 TradePilot 演示数据**时，才对该项目加 `--volumes`。不要用无项目名的 `docker compose down`、`docker system prune` 或全局卷清理命令。issue 验收脚本只删除随机命名的临时项目；若中断后留下资源，按脚本输出的唯一项目名检查并清理，不要猜测项目名。

## 5. 配置项与安全边界

模板及变量注释见 [`code/backend/.env.example`](../code/backend/.env.example)。重要配置包括：业务及评测数据库 URL、`DEMO_MODE`、`DEMO_PASSWORD`、`APP_ORIGIN`、Cookie 安全设置、`MODEL_PROVIDER`/`MODEL_NAME`/`MODEL_API_KEY`/`MODEL_BASE_URL`、`OCR_PROVIDER`、`MCP_CONTEXT_SIGNING_KEY`、`RUNTIME_CONFIG_ENCRYPTION_KEY`、端口及时区。

- 业务数据库与评测数据库必须用不同账号/口令；评测子进程不得继承主业务数据库凭据。
- 管理界面保存的模型/OCR 凭据用 Fernet 加密；API 与 Worker 副本必须共享 `RUNTIME_CONFIG_ENCRYPTION_KEY`。丢失密钥会导致历史凭据无法解密。
- 不要向真实用户数据运行样例脚本；所有 `verify_issue_*.ps1` 应创建独立隔离 Compose 项目。
- 本仓库目前没有生产部署/HTTPS/外部 ERP 上线说明，不要把演示模式部署到公网。

## 6. 常见故障

| 现象 | 处理方法 |
|---|---|
| `.env` 不存在或连接 URL 密码不匹配 | 依照 `.env.example` 复制并更新各自数据库 URL；不要复用两套数据库密码。 |
| Docker 镜像构建失败或依赖下载超时 | 确认 Docker Desktop 使用 Linux 容器且网络可访问 Docker Hub/GHCR；重试失败的单项脚本，不删除其他镜像/卷。 |
| 5173/8010/8001/55432 端口冲突 | 仅本机持久演示环境修改 `.env` 的对应 `*_PORT`；随机验收脚本自行分配空闲端口。 |
| API 健康但无法登录 | 检查已执行迁移和 seed、`DEMO_MODE=true`、经办账号名称、`DEMO_PASSWORD` 和 `APP_ORIGIN`；切勿使用 `root/root` 作为凭据。 |
| 模型显示未配置或失败 | 核对供应商、模型名、服务根 URL 与密钥；先查看 `/health/ready` 或任务错误状态。密钥不回显是预期行为。 |
| 评测中心无法创建运行 | 检查 `eval-db` 健康状态、评测数据库独立连接参数和管理员身份；查看本次运行报告与 `api/eval-db` 日志。 |
| 验收脚本非零退出 | 保存完整日志和随机 Compose 项目名；先读失败断言，不删除用户的 `tradepilot` 服务或数据。 |

## 7. 已知限制

- #41 的八小时运行时长按用户要求豁免，实测约两小时且没有最终八小时报告；不声称 NFR-11 长稳通过。
- 性能和浏览器数据来自本机隔离环境。#41 的 API 100 样本是一个订单查询端点；不能外推为全部 API 或生产容量。
- 在线模型验收是少量样本，证明该路径曾成功执行，不足以表示普遍模型准确率。离线 41/41 是夹具驱动的真实后端服务验收，与在线质量分开。
- OCR、字段候选及超额规则各有真实实现/测试；固定演示还没有同一张图像经在线 Agent 自动完成识别、纠错、审批和超额拦截的全链路结论。
- PDF/图片解析结果需要人工确认；评测/演示数据均为合成示例。
- 未验证生产 HTTPS、生产数据规模、法规/等保、真实 ERP 对接、真实客户数据迁移或生产 SLA。

完整需求与验收入口见 [`ACCEPTANCE-EVIDENCE.md`](./ACCEPTANCE-EVIDENCE.md)；原型文件、画板预览和设计约束见 [`design/README.md`](./design/README.md)。
