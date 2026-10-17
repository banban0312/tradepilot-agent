# 本地开发环境

本文说明如何启动 TradePilot M1 工程基线。此阶段只有服务骨架与健康检查，不包含可提单的业务流程。API 就绪只代表 PostgreSQL 可连接；MCP 的健康工具只验证协议与传输，不代表业务工具已交付。

## 环境需求

- Docker Desktop 及 Docker Compose v2；Compose 会使用文件中已锁定摘要的容器映像。
- 后端 Python 3.12，由 `uv` 管理；版本范围、依赖约束与精确解析结果分别记在 `code/backend/.python-version`、`pyproject.toml` 和 `uv.lock`。
- 前端 Node.js 22、npm 10 或 11；`code/frontend/.node-version` 与 `package.json` 会校验版本，`package-lock.json` 锁定前端依赖。

## 建立本机设置

从仓库根目录用 PowerShell 执行：

```powershell
Copy-Item code/backend/.env.example code/backend/.env
```

`backend/.env` 已排除在 Git 外。可为本机数据库修改密码，但不要在此文件放入生产凭据。模型与 OCR 字段留白时，API 明确回报“未配置”；填入凭据也只标示“已填设置，尚未验证”，不会被描述成已连通。

## Compose 启动

```powershell
Set-Location code
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml up --build --detach --wait
```

- 工作台：`http://127.0.0.1:5173`
- API 健康：`http://127.0.0.1:8010/health/ready`
- MCP Streamable HTTP：`http://127.0.0.1:8001/mcp`
- PostgreSQL：`127.0.0.1:55432`，Compose 内部服务使用隔离网络。

若本机端口已被使用，可在 `backend/.env` 改 `WEB_PORT`、`API_PORT`、`MCP_HOST_PORT` 或 `POSTGRES_PORT`。API `/health/ready` 以只读 SQL 查询检查数据库；MCP 健康探针由官方 SDK client 发现并调用 `health_check` 工具；前端健康检查实际载入页面。这些检查不代表订单或模型功能已实现。

停止服务并保留本机数据：

```powershell
docker compose --project-name tradepilot --env-file backend/.env --file compose.yaml down
```

只有确认可删除这个 TradePilot 本机示例数据时，才加 `--volumes` 移除此项目的 PostgreSQL 和附件卷。不要对其他 Compose 项目执行清理命令。

## 后端使用 uv

本机直接执行后端工具时，先安装符合范围的 Python，再由 `uv` 建立与同步隔离环境：

```powershell
uv python install 3.12
uv sync --project code/backend --locked
uv run --project code/backend --locked pytest code/tests/issues/test_issue_001.py
```

最后一行的集成测试会要求隔离 Compose 网络中的服务地址和 `TRADEPILOT_TEST_MODE=1`，一般情况请使用下方专用验证脚本。

## Issue #8 持久任务队列与恢复验证

```powershell
pwsh -File code/scripts/verify_issue_008.ps1
```

该脚本需 Docker Desktop Linux 容器引擎可用，并使用随机 Compose 项目、端口和 PostgreSQL 数据卷。脚本迁移到最新版本、运行 `alembic check`、初始化隔离示例账号，然后运行 #6 轨迹回归和 #8 PostgreSQL 集成测试。验证命令重放、采销任务隔离、同任务 advisory lock、旧 epoch 对任务状态及 LangGraph checkpoint 的写入 fencing，以及 Worker 进程释放连接后的原 job/task 恢复。进程心跳过期但锁仍被持有的场景必须保持不接管。

验证包含持久数据库与 LangGraph 官方 PostgreSQL checkpointer 的真实集成，不需要模型凭据；该 issue 不接入在线模型或业务 MCP。随机 Compose 清理仅作用于本次项目。若 Docker 不可用，静态检查结果不能替代数据库集成结果。

## Issue #14 任务 API 与 SSE 验证

```powershell
pwsh -File code/scripts/verify_issue_014.ps1
```

脚本要求 Docker Desktop Linux 容器引擎可用，以及已按上文建立的 `code/backend/.env`。脚本为 PostgreSQL、API 和 MCP 分配随机端口，创建随机 Compose 项目及数据卷；不会修改 `.env` 或触碰其他 Compose 项目。它执行数据库迁移和 `alembic check`，载入隔离演示账号后运行 #8、#13、#14 测试，覆盖任务创建/幂等/部门权限、等待点恢复、草稿版本冲突和编辑、事件序号与 SSE 重放/重同步。无需在线模型凭据；模型调用不属于本验收。完成或失败后只清理本轮随机 Compose 项目和数据卷，并恢复脚本设置的进程环境变量。

Docker 首次构建需访问 Docker Hub 与 GHCR；依赖镜像已缓存时可离线复用。预期所有测试通过且脚本以 0 退出；失败时脚本输出本轮 API/PostgreSQL 日志并以非零退出。实际验证结果应记录在 PR，不把未运行或失败的在线模型评测描述为通过。

如需额外验证从任务 API、持久 Worker、DeepSeek Function Calling 到真实 MCP 的完整在线流程，并已在 `code/backend/.env` 配置 DeepSeek 模型和密钥，可显式运行：

```powershell
pwsh -File code/scripts/verify_issue_014.ps1 -LiveModel
```

该模式会产生少量真实模型调用用量。它使用隔离 Compose 数据库、随机端口和临时演示密码，验证任务到达 `WAITING_APPROVAL`、模型与业务工具轨迹成功、写入草稿且正式订单数未增加；结束后只清理本轮隔离项目。脚本会在进程环境中生成一次性 MCP 上下文签名密钥并在退出时恢复，不修改 `.env`。模型密钥不会写入 Git 或输出到日志。在线模型结果与默认离线/真实服务集成测试分别记录。

## Issue #17 未知提交结果回查与安全恢复

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_017.ps1
```

该脚本会启动独立的数据库、API 和前端代理服务，覆盖任务失败定位、不可重试规则阻断、版本冲突、幂等重试，以及已提交订单但响应未知时回放原订单且不重复占额。脚本依赖 Docker 和本地 `code/backend/.env`，只清理自己创建的随机 Compose 项目。实现说明与验证细节见 [`M3-03-RECOVERY.md`](M3-03-RECOVERY.md)。

## Issue #1 完整验证

```powershell
pwsh -File code/scripts/verify_issue_001.ps1
```

脚本以随机且专用的 Compose 项目名称建立服务和数据卷，检查前端、API 健康及前端代理，再执行以下真实集成断言：

- API 通过 PostgreSQL 查询后回报就绪；模型/OCR 未设凭据时回报未配置，且响应不含密钥。
- MCP SDK client 通过 Streamable HTTP 列出并调用探针工具，核对结构化输出。
- LangGraph 在专用 PostgreSQL schema 写入一个中断点，关闭连接后以新连接读回并继续同一 thread。
- 前端使用锁定的 Node 22 映像执行 npm 安装与建置。

预期结果为 Compose 所有服务健康、前端及 API 就绪、三项整合测试通过，否则脚本以非零退出码停止。若失败，脚本会列出本次随机项目的服务日志。要保留容器供检查，可加 `-KeepStack`；输出会列出唯一项目名称。默认情况下脚本只删除本次随机项目建立的容器、网络和数据卷，不会清理其他 Docker 项目。

Docker 建置需要存取 Docker Hub、GHCR 和 npm；`uv lock` 需要存取 Python 套件索引。MCP 与检查点验收不依赖外部模型/OCR 服务；线上模型评测尚未执行。
