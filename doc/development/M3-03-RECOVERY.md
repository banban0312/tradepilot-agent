# M3-03 未知提交结果回查与安全恢复

## 已实现行为

- Worker 失败时只从本次执行租约期间的 span 中定位错误；没有可定位 span 时生成内部失败 span。失联作业的遗留 `RUNNING` span 会转为可重试的 `INTERRUPTED`。任务投影记录安全错误码和可重试标记；模型、MCP 和订单提交轨迹不保存异常原文。
- `POST /api/v1/tasks/{task_id}/retry` 要求经办人身份、CSRF、幂等键、失败 span 和预期任务版本。服务端拒绝未知请求字段，因此客户端不能选择 LangGraph 节点或 `goto`。
- 重试只接受当前失败任务、最新失败 span 和状态为 `FAILED` 的原作业；服务端引用原始 `START`、`INPUT` 或 `REVIEW` 命令，并沿同一任务的持久 PostgreSQL checkpoint 恢复。
- 重试复用原 `job_id`，保持由 job 派生的 MCP operation identity，从而在工具重放时沿用已有幂等账本。同一幂等键即使之后发生另一轮恢复，仍可回放原命令和作业。
- 订单事务前后记录 `order.commit` span。数据库执行错误被标记为结果未知；任务保持失败，草稿仍锁定。安全重试会重新走原审批 checkpoint，订单提交服务先检查已有订单/占额事实；已提交时返回原订单，不重复占额。确定的业务规则阻断则由订单服务释放草稿锁并使旧审批失效。
- 等待中的人工输入/审批继续使用原任务、检查点、等待 ID 和已持久化命令；恢复 API 不接受客户端 checkpoint 或节点覆盖值。

## 验证

在仓库根目录的 PowerShell 执行：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_017.ps1
```

脚本要求 Docker Desktop Linux 容器引擎可用，并要求已配置 `code/backend/.env`。它为本轮建立随机 Compose 项目、PostgreSQL 数据卷和端口，启动隔离数据库、API 与 Web，执行 Alembic 迁移与一致性检查、Ruff 检查/格式检查、Python 编译检查、演示数据初始化，以及 #3、#5、#8、#13、#14、#15、#16、#17 的服务集成回归。回归验证失败 span、不可重试规则阻断、旧版本冲突、相同幂等键回放、拒绝客户端指定恢复节点，以及模拟“订单事务已提交、调用方随后重试”时复用原订单且不重复占额。

脚本只清理随机命名的 Compose 项目和对应数据卷，并恢复其设置的进程环境变量；不会清理用户现有的 `code` Compose 项目。预期所有测试通过且脚本返回 0；失败时脚本输出本轮服务日志并返回非零。在线模型调用不在本验收中，未声称完成在线模型评测。

## 当前验证记录

- 隔离 Compose 集成：51 项通过。
- 数据库迁移与 ORM 一致性：通过。
- Ruff 检查及格式检查、Python 编译检查：通过。
- 在线模型评测：未执行；本 issue 的安全恢复验收使用隔离数据库和确定性测试夹具。
