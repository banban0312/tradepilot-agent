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
- 当前已有真实隔离服务离线端到端证据的案例：`normal-complete-sales`、`normal-complete-purchase`、`normal-price-from-contract`、`normal-approved-order`、`abnormal-tool-type`、`abnormal-past-delivery`、`abnormal-missing-quantity`、`abnormal-model-timeout`、`ambiguity-missing-spec`、`ambiguity-material-alias`、`ambiguity-missing-date`、`ambiguity-missing-price`、`unauthorized-cross-department`、`unauthorized-prompt-injection`、`unauthorized-role-claim`、`quota-over`、`quota-equal`、`quota-over-by-cent`、`quota-inactive-contract`、`quota-purchase-isolated`、`quota-note-no-bypass`。其余固定案例仍须逐步补齐执行路径与证据。

## 本地测试

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\code\backend);$(Resolve-Path .\code)"
uv run --frozen --project backend --directory code python -m pytest -q tests/issues/test_issue_037.py tests/issues/test_issue_036.py
```

跨服务验证入口为 `code/scripts/verify_issue_037.ps1`。在线请求可能产生模型 API 费用；要验证在线错误处理，可使用隔离环境并确认供应商配置/连通性，报告会记录真实失败，不会伪报通过。
