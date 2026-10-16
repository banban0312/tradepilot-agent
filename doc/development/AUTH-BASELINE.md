# 登录会话与授权基线

本文说明 M1-03 已交付的后端登录与授权基础。当前没有草稿、审批或订单业务 API；这些接口接入时必须复用 `app.auth` 中的可信上下文、角色和部门范围策略。

## 初始化

1. 复制 `code/backend/.env.example` 为 `code/backend/.env`，设置本机 PostgreSQL 密码。
2. 如需使用演示账号，显式设置 `DEMO_MODE=true` 和长度至少 12 个字符的 `DEMO_PASSWORD`。项目不内置通用默认账号密码。
3. 本机 HTTP 演示默认使用 `APP_ORIGIN=http://localhost:5173` 和 `SESSION_COOKIE_SECURE=false`；HTTPS 部署必须将 Secure 设为 `true`，并把 `APP_ORIGIN` 改为浏览器访问的同源地址。
4. 在 `code/` 运行 `docker compose --env-file backend/.env up --build --detach --wait api`，然后执行：

   ```powershell
   docker compose --env-file backend/.env exec api uv run --frozen alembic upgrade head
   docker compose --env-file backend/.env exec api uv run --frozen python -m app.seed
   ```

每次执行迁移前应确认目标数据库正确。演示账号仅在 `DEMO_MODE=true` 时创建：

| 登录名 | 角色 | 授权部门 |
|---|---|---|
| `demo-operator-north` | 业务经办人 | 华北 |
| `demo-operator-south` | 业务经办人 | 华南 |
| `demo-approver-north` | 业务审批人 | 华北 |
| `demo-admin` | 系统管理员 | 华北、华南 |

四个演示账号使用环境变量 `DEMO_PASSWORD` 的值。系统管理员只拥有独立的系统管理员角色；部门范围不会自动赋予经办或审批权。

## 接口

- `POST /api/v1/auth/login`：JSON `{ "login": "...", "password": "..." }`。响应包含用户、角色、部门范围和 CSRF 令牌，并设置 HttpOnly、SameSite=Lax 会话 Cookie。
- `GET /api/v1/auth/me`：根据数据库中的有效会话返回当前用户、角色、部门范围和当前 CSRF 令牌。
- `POST /api/v1/auth/logout`：必须同时携带会话 Cookie、同源 `Origin` 和 `X-CSRF-Token`，成功后撤销服务端会话。

会话令牌使用密码学安全随机数，数据库仅存 SHA-256 摘要；默认 12 小时过期，可通过 `SESSION_TTL_HOURS` 调整。密码使用 Argon2id 哈希。所有 `/api/v1/` 写请求先校验同源 `Origin`，带会话的写入口还必须使用 `require_csrf` 依赖。反向代理部署应配置外部 `APP_ORIGIN`。

身份上下文只由有效会话、用户角色和授权部门表生成。业务 handler 和 MCP 工具不得从请求参数或模型输出构造身份，也不得接受客户端部门字段覆盖可信范围。`require_role` 和 `require_department_scope` 分别检查角色与部门，`require_business_access` 同时要求经办/审批角色和部门范围；管理员角色不自动获得业务读取或提交权限。MCP 当前只有无业务副作用的健康探针；后续业务工具需在各自 issue 中加入内部凭据验签与当前权限复核。

## 验证

执行 `pwsh -File code/scripts/verify_issue_003.ps1`。脚本为本次运行生成临时演示密码，只注入隔离 Compose 项目，不打印或写入仓库；验证迁移重复执行、迁移与模型一致、重复 seed、前端代理保留版本化认证路径、会话令牌哈希、账号错误/停用/过期、登出撤销、CSRF、跨源拒绝及角色/部门隔离。脚本结束时只删除随机命名的本次 Compose 项目容器、卷和网络。

生产环境启用 HTTPS、Cookie Secure 和固定 `APP_ORIGIN` 是部署前提。本 issue 未实现用户自助注册、密码修改/找回、MFA、登录限流或完整 MCP 服务凭据；这些能力没有在当前交付中宣称完成。
