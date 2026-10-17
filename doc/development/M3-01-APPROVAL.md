# M3-01 人工审批中断

## 已实现行为

- 草稿通过当前字段、合同、物料、额度预检后，LangGraph 保存带草稿版本与 SHA-256 内容摘要的审批中断；任务投影中的 `active_wait` 来自该中断检查点。
- `POST /api/v1/tasks/{task_id}/review` 接收 `APPROVE`、`RETURN`、`REJECT`。请求需带 `draft_version`、`content_hash`、`checkpoint_id`、`interrupt_id`；`reason` 可选，最多 1000 字。
- API 校验登录会话、审批角色、部门范围、独立审批人、当前等待点、草稿版本/摘要及当前规则。决定、执行命令、作业和任务事件同事务持久化。
- Worker 只接受与当前等待、批准记录和草稿快照匹配的持久命令。批准进入 Spec 定义的 `SUBMITTING` 边界；正式订单事务由后续 M3 issue 实现。退回恢复到输入中断并把审批意见作为等待原因返回，拒绝进入 `REJECTED` 并保留审批意见供任务审计读取。
- 审批尚未被 Worker 消费且作业仍为 `READY` 时，经办人可通过草稿编辑接口改版。旧审批标记失效，旧命令置为 `SUPERSEDED`，新版本编辑命令替代待执行审批。Worker 已领取作业后，编辑返回冲突。
- 自我审批默认禁止；仅当 `DEMO_MODE=true` 且 `DEMO_ALLOW_SELF_APPROVAL=true` 时演示环境允许。该开关不授予审批角色，也不跳过版本或业务规则校验。

## 验证

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_015.ps1
```

脚本使用随机 Compose 项目、独立 PostgreSQL 卷和本轮随机演示密码，执行数据库迁移、`alembic check`，并运行 #8、#13、#14、#15 回归及审批验收。脚本只清理自己创建的 Compose 项目；不改写 `code/backend/.env`，不启动在线模型或正式订单服务。

直接运行 `code/tests/issues/test_issue_015.py` 的数据库 API 用例要求隔离 Compose 环境、演示模式、`TRADEPILOT_TEST_MODE=1` 与本轮演示密码。图行为单测使用 LangGraph `InMemorySaver`，不把它当作 PostgreSQL 持久化证据。
