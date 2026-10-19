# 自动评测运行说明

## 运行入口

在仓库根目录使用 PowerShell 执行：

```powershell
.\code\scripts\verify_issue_037.ps1
```

默认使用离线模式，运行固定数据集。传入案例或分类可缩小范围；参数可以传多个值：

```powershell
.\code\scripts\verify_issue_037.ps1 -CaseId normal-complete-sales,normal-complete-purchase
.\code\scripts\verify_issue_037.ps1 -Category normal,quota
```

在线模式会调用 `code/backend/.env` 中配置的真实模型：

```powershell
.\code\scripts\verify_issue_037.ps1 -Mode online -CaseId normal-complete-sales
```

离线夹具不会调用真实模型。两种模式都通过隔离 API、Worker、MCP 和 PostgreSQL 执行业务；在线评测不能由离线结果代替。

## 环境与清理

- 后端依赖通过 `uv` 管理；Docker 需可用，`code/backend/.env` 需包含本地数据库配置。
- 验证脚本为每次运行生成独立 Compose 项目名、镜像标签、端口、数据库卷、演示密码及 MCP 签名密钥。
- 仅启动临时 `db`、`api`、`mcp` 容器，执行迁移、演示数据写入、Issue 测试和评测，然后仅清理本次命名的容器、网络、卷和镜像。不会停止或重建其他 Compose 项目。
- 报告保存在 `code/artifacts/evals/<隔离项目名>/`；该目录用于复核，不属于源码提交。
- 评测执行器要求 `TRADEPILOT_EVAL_SANDBOX=1`，并校验 API/数据库均位于隔离 Compose 网络，拒绝对宿主机或其他业务数据库执行。

## 结果说明

- `summary.json` 汇总模式、数据版本、所选案例、状态计数与执行器元数据。
- `cases.jsonl` 逐例保存预期、实际状态、断言结果、任务和轨迹引用。
- `report.md` 提供人工可读摘要。
- 案例引用的合同若未由应用演示种子提供，执行器会仅在隔离评测数据库创建版本化合同、明细和零占用额度账，避免依赖业务演示数据库中的临时状态。
- `PASS` 才计为通过；`FAIL`、`SKIPPED` 或 `ERROR` 均令命令以非零退出。
- 离线模型响应夹具单独保存在 `fixtures/v1/model_responses.json`，不由 `expected` 断言生成。尚无对应夹具或执行动作的案例会标为跳过，不能当作通过。
- `normal-approved-order` 通过独立授权审批人登录并提交绑定当前草稿版本的审批，再由真实 Worker 恢复并提交订单；执行器不会代替用户批准。
- `normal-multiple-lines` 在隔离数据库创建两条真实合同明细，并断言各行物料、数量、单价及金额均正确。
- `abnormal-mcp-unavailable` 仅将评测 Worker 的 MCP 地址指向隔离容器内未监听端口，以真实连接失败检查错误码与无订单副作用。
- `unauthorized-wrong-approver` 在隔离数据库临时创建南区审批身份，尝试审批北区任务；验证 API 以 `404 RESOURCE_NOT_FOUND` 隐藏跨部门任务，并由原经办人回查等待状态及零订单、零审批、零占额事实。专用身份只存在于本次隔离数据库。
- `unauthorized-old-approval` 先持久化授权审批，再由经办人编辑排队中的草稿；验证旧审批记录失效、原幂等键重放返回 `409 IDEMPOTENCY_CONFLICT`，并由真实 Worker 恢复到新版本的审批等待点；不生成订单或额度占用。
- `special-recovery-waiting-approval` 让真实 Worker 将任务推进到审批等待点，再以新 Worker 实例恢复队列；确认无残留作业被误领，API 仍能回查原审批等待点，且不写订单、审批或额度占用。
- `special-recovery-waiting-input` 让真实 Worker 将任务推进到输入等待点，再以新 Worker 实例确认该等待点可回查；经办人补充规格后恢复同一任务到审批等待，不创建订单或额度占用。
- `duplicate-double-click` 由同一授权审批人并发提交两次相同版本、相同幂等键的批准请求，再由真实 Worker 处理唯一持久作业；核对只生成一笔订单和一次合同占额。
- `duplicate-draft-replay` 在 Worker 持有有效任务租约时，通过真实 MCP 客户端用同一签名上下文重放相同草稿保存请求；核对两次输出一致且草稿版本只保存一次。
- `duplicate-commit-stale-approval` 在排队审批被经办人编辑作废后，由真实 Worker 恢复新草稿，再直接重试旧持久批准的订单提交服务；断言以 `APPROVAL_INVALID` 拒绝且不产生订单或占额。
- `duplicate-key-payload-conflict` 在隔离数据库内对已提交订单重放被篡改审批绑定的持久命令；使用事务 savepoint 回滚故障注入，断言提交服务返回 `ORDER_IDEMPOTENCY_CONFLICT`，原订单和额度账保持不变。
- `duplicate-lost-response` 与 `special-unknown-commit` 在隔离服务中完成授权审批和真实订单提交，再重放同一持久审批命令；断言任务完成、重放返回同一订单，且不重复占额。由于订单提交目前是 Worker 内部事务，这两例验证的是提交结果持久化后的幂等恢复，不模拟外部 HTTP 响应丢失。
- `special-concurrency-idempotency` 对同一已完成任务并发执行 8 次持久提交结果回放；断言 8 次均指向同一订单，订单和合同占额各只有一笔。
- `special-concurrency-quota` 使用两个真实任务和 Worker 同时提交各 7000 元订单竞争 10000 元合同额度；断言恰有一笔完成，另一笔以 `CONTRACT_AMOUNT_EXCEEDED` 阻断。
- `normal-excel-row` 与 `ambiguity-excel-row` 使用固定 XLSX 夹具走真实附件上传和订单导入预览 API，断言正常组、缺项组和超额组状态及原始行号；预览不会伪装成 Agent 任务或模型调用，也不会创建订单、审批和额度占用。
- `abnormal-damaged-pdf` 将固定损坏 PDF 上传至真实附件 API，断言上传校验返回 `FILE_UNREADABLE`，附件未落库且没有业务订单副作用。
- `duplicate-same-key` 先由经办人创建草稿、授权审批人批准并由 Worker 提交，再在隔离事务中重放同一持久审批命令；核对提交函数返回原订单 ID，且数据库中订单和合同占额没有重复增加。当前服务的订单提交是 Worker 内部事务，不存在公开的订单提交 HTTP 端点；本项覆盖持久层幂等回放，不代表网络并发或响应丢失验证。
- 当前已有真实隔离服务离线端到端证据的案例：`normal-complete-sales`、`normal-complete-purchase`、`normal-price-from-contract`、`normal-multiple-lines`、`normal-excel-row`、`normal-approved-order`、`abnormal-damaged-pdf`、`abnormal-tool-type`、`abnormal-past-delivery`、`abnormal-missing-quantity`、`abnormal-model-timeout`、`abnormal-mcp-unavailable`、`ambiguity-missing-spec`、`ambiguity-material-alias`、`ambiguity-missing-date`、`ambiguity-missing-price`、`ambiguity-similar-customer`、`ambiguity-excel-row`、`unauthorized-cross-department`、`unauthorized-prompt-injection`、`unauthorized-role-claim`、`unauthorized-direct-commit`、`unauthorized-wrong-approver`、`unauthorized-old-approval`、`special-recovery-waiting-approval`、`special-recovery-waiting-input`、`duplicate-same-key`、`duplicate-double-click`、`duplicate-commit-stale-approval`、`duplicate-lost-response`、`duplicate-draft-replay`、`duplicate-key-payload-conflict`、`special-concurrency-quota`、`special-concurrency-idempotency`、`special-unknown-commit`、`quota-over`、`quota-equal`、`quota-over-by-cent`、`quota-inactive-contract`、`quota-purchase-isolated`、`quota-note-no-bypass`；当前固定数据集 41 个案例均有真实隔离服务离线端到端证据。

## 本地测试

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\code\backend);$(Resolve-Path .\code)"
uv run --frozen --project backend --directory code python -m pytest -q tests/issues/test_issue_037.py tests/issues/test_issue_036.py
```

跨服务验证入口为 `code/scripts/verify_issue_037.ps1`。在线请求可能产生模型 API 费用；要验证在线错误处理，可使用隔离环境并确认供应商配置/连通性，报告会记录真实失败，不会伪报通过。
