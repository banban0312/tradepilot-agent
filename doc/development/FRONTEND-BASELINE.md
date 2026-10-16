# 前端导航与通用界面状态基线

本文对应 M1-07（Issue #7），记录工作台登录入口、导航、界面状态反馈及其与服务端会话的边界。页面以 `doc/design/exports/00-overview.png`、`10-sign-in.png`、`18-interface-states.png`、`22-new-task-help.png` 和 `00-design-system.png` 为视觉依据；需求与接口以 `doc/PRD.md`、`doc/SPEC.md` 为准。

## 登录与路由

- `/login` 是公开入口；工作台和界面状态示例路由每次进入时都会请求同源 `GET /api/v1/auth/me`，避免只依赖客户端缓存身份。服务端未返回有效身份时转到登录页，并保留站内目标路径。
- 登录通过同源 `POST /api/v1/auth/login` 建立服务端 Cookie 会话。浏览器不保存密码或会话令牌；身份、角色、部门授权范围以服务端响应为准。
- 登出使用 `POST /api/v1/auth/logout` 并携带当前身份提供的 CSRF token。失败时保留当前会话状态并显示服务端错误。
- 登录页示例身份只提供账号切换。只有在本机显式启用演示模式并完成演示数据初始化后才可登录；示例密码由本机环境配置，不写入代码、页面或文档。
- 登录帮助、过期提示、身份与授权范围、主导航均使用简体中文。

## 尚未接入的模块

当前工作台只提供受保护的基础入口。订单中心、批量导入、任务追踪、评测中心、主数据与配置以及任务提交流程尚未由本 issue 接入；点击未接入入口只显示说明，不读取或生成虚构业务数据，也不提交用户输入或模拟上传成功。界面状态示例页是组件展示，不代表真实任务、订单或服务状态。

后续 issue 会按依赖逐步接入真实业务流程。原型中的能力说明、状态样例和数据不能替代服务端授权、数据库事实、人工审批或真实服务验证。

## 验证

在仓库根目录准备 Docker Desktop、Compose v2，并按 `doc/development/LOCAL-ENVIRONMENT.md` 配置 `code/backend/.env`。后端依赖由 `uv` 管理。运行跨前后端集成验证：

```powershell
pwsh -File code/scripts/verify_issue_007.ps1
```

脚本创建随机命名的独立 Compose 项目和专用数据卷，使用随机演示密码，执行迁移与 Alembic 漂移检查，再通过前端同源代理验证简体中文入口、历史路由、登录失败、登录/身份恢复、CSRF 登出、跨源拒绝和非法字段拒绝。完成后仅清理由脚本创建的 Compose 项目及卷，并恢复当前 PowerShell 进程中的端口和演示环境变量。需要 Docker、镜像源和 npm 网络；不调用在线模型或 OCR 服务。

前端静态检查可在 `code/frontend/` 执行：

```powershell
npm run type-check
npm run build
```

1366×768、1920×1080、Chrome/Edge 多版本及完整业务流程的 NFR-12 验收由相应后续流程记录；本基线只说明响应式布局已实现，不把单一浏览器截图描述为完整兼容性验收。线上模型评测不适用于本 issue，未执行。
