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

## 评测指标与报告解释（M6-03）

每次运行生成 `summary.json`、`cases.jsonl` 和 `report.md`。三份报告记录相同的运行模式、数据集版本、分类计数和质量指标；JSONL 为逐例机器可读明细，Markdown 展示可读摘要和失败定位。

- 字段精确匹配 = 精确匹配字段数 / 已执行案例中预期标注字段数；遗漏字段计入分母并同时降低覆盖率。跳过或执行错误的字段数单独列出，不假作提取失败或成功。
- 字段分组包含字段路径、字段来源类型（来自持久化字段证据）和文件 MIME 类型。没有来源记录时计为 `unavailable`，不猜测来源。
- 工具选择按案例所需工具及允许工具集合检查；工具执行率按实际工具 span 的成功状态计算；参数校验报告接受/拒绝次数，不把预期拒绝的非法输入判为成功调用。
- 规则阻断仅统计预期状态为 `BLOCKED_RULE` 的案例。越权、额度和重复提交安全案例单列分类与不变量通过率，不能由其他正常案例平均掩盖。
- 幂等和恢复指标按 `duplicate_submission` 分类及 `recovery` 标签计算，分母只含实际执行且可判定的案例；跳过/错误案例在总状态和未执行计数中可见。
- Token 仅累计模型 span 实际上报的用量；无用量时显示 `unavailable` 并报告覆盖率，绝不写成 0。
- 耗时分别记录模型 span、工具 span 和评测单例耗时。CLI 无法观测的人工等待明确标为 `unavailable_not_measured_by_cli`。
- 运行元数据记录运行模式、数据/主数据/输入夹具版本、清单摘要、随机种子、代码版本、Python/平台、模型名/供应商、提示词与工具 Schema 摘要、非敏感模型配置摘要。报告写入前递归移除凭据字段并遮蔽常见 API key 格式。

在线结果和离线夹具结果必须分别运行、分别看 `mode`，不得汇总成一个模型质量分数。每例 JSONL 保存 expected、actual、失败断言、错误码、task/trace/span 引用和输入证据。
- 参数校验统计以已进入实际 TOOL span 的请求计为通过，以 `function_calling.tool_argument_validation` RULE 错误 span 计为拒绝；没有相应 span 时不推测。
- 字段评分只纳入固定数据集中标注的业务字段（或 `expected.field_labels` 显式指定项），如审批状态、重放次数等执行事实不混入字段提取准确率。
- 预期值为 `null` 的抽取字段表示该等待态下字段本就未确认，故不纳入必评字段分母；预期非空但实际缺失仍计为遗漏并降低覆盖率。

## 评测中心（M6-04）

系统管理员可从“评测中心”启动 v1 固定数据集的离线或在线运行。API 在独立的 eval-db PostgreSQL 服务中为每次运行创建 tp_eval_<run UUID> 数据库，完成迁移和固定示例 seed 后，启动隔离 API 与 MCP；评测执行器按案例控制一次性 Worker，避免常驻 Worker 抢占并发案例。执行器验证隔离 API/数据库服务标识。评测结束时停止子进程并删除该运行数据库，保留报告供页面查询和下载。评测子进程只接收专用评测数据库凭据，不接收主业务数据库凭据。

运行进度逐案例写入状态文件；运行中页面只显示进度，不显示最终成绩。结束后页面从同一份 summary.json、cases.jsonl 显示指标、分类和失败证据。失败项展开的是该隔离运行保存的任务/轨迹快照，不跳转到主业务数据库的任务轨迹。在线模式会使用后端进程配置的真实模型并可能产生费用；离线和在线运行始终分开标记。

评测中心运行要求：

- 后端在仓库 Compose 网络内运行，业务 PostgreSQL 与独立评测 PostgreSQL 服务名分别为 db、eval-db。
- 主库使用 DATABASE_URL；评测服务使用独立的 EVALUATION_POSTGRES_DB、EVALUATION_POSTGRES_USER、EVALUATION_POSTGRES_PASSWORD 和 EVALUATION_DATABASE_ADMIN_URL。评测凭据不得复用主库密码。
- API 服务挂载 evaluation_reports 持久卷。报告仅允许系统管理员查询和下载。
- 页面只支持固定数据集版本 v1；不接受客户端路径、任意执行命令或自定义执行器。

Issue #39 的本地验证入口：

```powershell
.\code\scripts\verify_issue_039.ps1
```

该入口运行后端授权/报告/进度测试、评测服务进度测试和前端类型/生产构建。实际隔离服务运行则从已启动的本机应用页面选择“运行评测”，默认离线；在线运行单独验证模型连通性与实际结果。

## 专项回归（M6-05）

在仓库根目录用 PowerShell 执行 `./code/scripts/verify_issue_040.ps1`。脚本创建唯一名称的隔离 Compose 项目和数据库卷，运行 #8、#9、#10、#13、#14、#15、#16、#17、#20、#22、#23、#24、#25、#29、#37、#38 回归，并从真实隔离 API、MCP 和 PostgreSQL 重新执行完整 v1 离线数据集；固定案例必须为 41/41 PASS。案例报告保存在被 Git 忽略的 `code/artifacts/evals/<隔离项目名>/`，保留状态、断言、任务和轨迹引用。此结果是离线模型夹具加真实后端服务的集成验证，不代表在线模型验证。

恢复压力用例重复 10 轮：旧 Worker 在 `WAITING_INPUT` 检查点失联后由新 Worker 进程恢复；任务到达 `WAITING_APPROVAL` 后再启动独立进程回读数据库投影、草稿版本、图版本和 checkpoint 引用。批次回归通过真实导入 API 创建两个共享合同的子任务，为子任务建立固定版本审批测试前置状态后，并发提交各 10 万元订单，核对订单、占额和审计事实。独立故障注入进程分别在订单事务提交前和已提交、成功响应发出前强制退出；验证前者回滚、后者可回查原订单且不重复占额。20 次相同订单提交、模型/MCP 超时和提示注入由数据库集成测试及 41 个固定案例验证。

脚本只删除本次唯一 Compose 项目、镜像和卷，不重启或清理开发用 Compose 服务。Docker Desktop 必须运行；在线模型不在本项脚本中调用。若失败，按断言与隔离容器日志定位，清理只针对脚本输出的项目名。
