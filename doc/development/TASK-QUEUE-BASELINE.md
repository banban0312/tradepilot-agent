# 持久任务队列与恢复基线

本基线对应 M2-01（GitHub #8），实现任务容器与任务执行身份分离、数据库命令/作业、同任务 PostgreSQL advisory lock、执行 epoch 与 LangGraph PostgreSQL 检查点写入 fencing。

## 持久状态

- `sessions` 是导航容器；销售和采购任务各有独立 `agent_tasks.id`。任务 ID 同时用作 LangGraph `thread_id`，恢复继续原任务而不生成新任务。
- API 应在一个数据库事务中调用任务创建/命令入队服务，确保命令和 `jobs` 作业一起提交，再响应客户端。HTTP 请求生命周期不负责长任务存活。
- `(task_id, request_id)` 唯一约束实现请求重放；相同请求内容返回原命令和原作业，不同内容返回幂等冲突。
- `jobs` 用 `READY/RUNNING/DONE/FAILED` 投影作业生命周期；领取事务使用 `FOR UPDATE SKIP LOCKED`，领取完成即释放行锁，不覆盖外部调用。

## 执行锁与 fencing

- Worker 以专属 PostgreSQL 连接获取 `pg_try_advisory_lock(hashtextextended(task_id, 0))`，持有到图调用结束。连接不可回到普通连接池复用。
- 获取任务锁后，在事务中递增 `agent_tasks.execution_epoch` 并写入作业。每次状态投影/业务副作用必须在同一事务中先 `SELECT ... FOR UPDATE` 校验 epoch；旧执行者不能在检查和写入之间被接管。
- `EpochFencedPostgresSaver` 绑定任务 ID、epoch 和专属连接。检查点及中间 writes 的同一个写事务先锁定任务行核对 epoch，避免“先查 epoch，再由另一连接写 checkpoint”的竞争窗口。业务 ORM 写入使用 `lease.write_session()`，它将任务行锁与业务写入放在同一连接的外层事务中；官方 LangGraph 负责管理 checkpoint 表结构。
- 恢复器将过期心跳仅视为候选。只有成功获取任务 session advisory lock 才能递增 execution_epoch 并将原 job 重新排为 READY；恢复接管事务先使旧 epoch 失效。心跳过期本身不撤销仍持锁 Worker 的写权限。
- Worker 初始最多 5 个全局活跃任务；领取事务使用全局短事务锁串行化容量判断，单个任务仍由 advisory lock 串行。没有注册图执行器的作业不会被领取或显示成功；真实图/工具执行由后续 issue 接入。

M1 可观测性表此前允许任务表建立前的历史 UUID。本次迁移以 PostgreSQL `NOT VALID` 外键保留潜在历史孤儿记录，同时立即约束新写入；待数据盘点后可单独验证历史约束。审计记录仍不可变。

## 验证

```powershell
pwsh -File code/scripts/verify_issue_008.ps1
```

该命令使用随机 Compose 项目、独立 PostgreSQL 数据卷和随机端口，先迁移/检查 ORM，再运行 `test_issue_006.py` 回归与 `test_issue_008.py`。场景包括采销任务隔离、命令幂等冲突、真实锁竞争、活 Worker 心跳陈旧时不被接管、释放锁后同一 task/job 恢复、旧 epoch 无法写任务状态或 LangGraph checkpoint。脚本退出时只删除本次随机 Compose 项目和数据卷。

本地离线静态检查不能替代上述 PostgreSQL 集成。没有 Docker/PostgreSQL 时，迁移、锁竞争、checkpointer 与恢复验收应报告“未验证”，不能仅凭 mock 通过。

## 当前边界

本 issue 不实现任务 HTTP API、模型 Function Calling 或缺字段 LangGraph 流程。队列接口可由后续 API issue 在一个事务中调用；真实图执行器在相关 workflow issue 注册。原型图中的示例任务、消息和业务单号不作为数据库验收数据。
