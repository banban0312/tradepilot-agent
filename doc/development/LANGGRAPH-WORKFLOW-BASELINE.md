# LangGraph 缺字段等待与恢复基线

本基线对应 M2-06（GitHub #13）。任务以数据库中的 `graph_version` 固定路由；`v1` 使用显式 `StateGraph` 和官方 PostgreSQL checkpointer。任务 ID 同时作为 LangGraph `thread_id`，不会为补充输入创建新任务。

## 执行与等待

- Worker 领取 `RUN_TASK` 后，从数据库重建任务所有者角色与部门范围，再调用真实模型 Function Calling 与 MCP；模型不提供身份或权限字段。
- 图按 `ingest → parse → plan_tools → execute_tools → validate` 显式执行。模型/MCP 保存的草稿由数据库当前版本和字段校验结果决定是否可继续；工作流不靠模型的自然语言回答判断字段完整。
- 缺字段进入 `prepare_input_wait → await_input`。等待节点仅调用一次 `interrupt`，不执行草稿或订单写入。中断后 Worker 从官方 checkpoint 快照投影 `task_waits` 与 `agent_tasks` 状态；检查点和业务投影分别保存。
- 恢复命令必须绑定活动 task、checkpoint、interrupt 和草稿版本。Worker 实际恢复时使用当前 `thread_id` 与 `Command(resume=...)`；成功后再标记该等待命令已消费。MCP 草稿写入以持久 job 和工具调用位置生成的 operation ID 幂等，图节点恢复不能重复创建草稿版本。
- `persist_draft` 只核实 MCP 已保存的草稿事实，不重复写入。当前图在可审草稿后投影 `WAITING_APPROVAL`；真实审批中断与批准/提交节点属于 M3，不在本基线提前开放。

## 版本兼容

新任务的图版本沿用数据库默认 `v1`。恢复时要求任务版本与当前构建一致；未知或旧版本返回稳定恢复冲突，不会尝试用新图解释旧 checkpoint。节点 ID 与中断顺序作为活动版本契约，变更需新增版本并保留旧版本执行器，或先将不兼容活动任务安全终止，不能静默覆盖。

`task_waits` 是 UI/命令校验投影，不是 checkpoint 的替代品。Worker 重领同一 RUN_TASK 作业时先读取 PostgreSQL checkpoint：已有中断则重建等待投影；有待执行节点则从该节点继续；完成状态只重建最终投影，不重新提交初始输入。恢复输入使用独立 job 和命令引用，仍需匹配原等待点。

## 验证

离线图契约：

```powershell
uv run --frozen python -m pytest tests/issues/test_issue_013.py -q -k "not postgres_checkpoint"
```

真实 PostgreSQL 与官方 saver、epoch fencing、隔离 schema、任务等待投影及独立新 Worker 进程恢复：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_013.ps1
```

脚本使用随机 Compose 项目、随机回环端口和独立 PostgreSQL 卷；会迁移并初始化固定演示账号，运行 #8 回归与 #13 PostgreSQL 恢复测试。#13 集成测试先在父进程写入真实 PostgreSQL 中断，再由全新 Python Worker 进程领取恢复命令、重建 saver 并继续同一 thread/checkpoint。结束时只删除本次 Compose 项目及其卷。模型与 MCP 节点使用固定服务夹具，因此该测试验证状态图和重启恢复，不声称再次验证在线模型；#12 已单独提供 DeepSeek 在线 Function Calling + 真实 MCP 证据。
