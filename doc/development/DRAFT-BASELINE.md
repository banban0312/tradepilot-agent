# 草稿领域与版本基线

Issue #4 提供订单候选、有效草稿、人民币金额计算、字段来源记录，以及可复用的草稿保存/载入服务。任务表和任务 API 由后续 issue 引入，因此当前服务以调用方生成的 `draft_id` 为入口；本阶段不伪造任务关联，也不提供审批或正式下单接口。

## 类型和金额约定

- `CandidateOrder` 与 `ValidatedDraft` 是不同的 Pydantic 类型。候选允许字段缺失并可暂存；`validate_candidate` 仅在订单类型、合同、客户、项目、交期、收货信息和至少一条完整有效明细齐备时生成 `ValidatedDraft`。
- `ValidatedDraft` 只表示本 issue 已实现的字段结构与金额规则通过，不代表合同方向/状态、部门关联或实时额度检查通过，更不代表已获审批。后续主数据、额度和审批流程仍须分别执行权威校验。
- 输入数量和单价使用十进制字符串，内部转为 `Decimal`。拒绝 JSON 浮点数、负数、非有限值、超过 6 位小数及超出 `numeric(18,6)` 的数值；不静默截断。
- 行金额以足够精度相乘，再按 `ROUND_HALF_UP` 舍入至人民币分；总额为已舍入行金额之和。金额与币种限制和数据库的 `numeric(20,2)`、CNY 约束一致。
- 不完整明细可以保存，若任一行金额无法计算则版本总额为空；只有有效草稿才可进入后续审批流程。

## 来源证据和身份

`FieldEvidenceInput` 支持用户文本、文件页、文件单元格、主数据、人工编辑和系统计算来源，并保留原始值、确认值及定位。客户端模型禁止额外的身份或审批字段。保存服务从可信 `AuthContext` 获取修改人，只有 `USER_EDIT` 的确认人由服务端记为当前用户。

## 版本和事务

迁移新增 `draft_revisions`、`draft_items`、`field_evidence`。修订与明细按版本只追加，SHA-256 摘要由排序键规范化的业务 JSON 计算，不包含来源证据或界面/运行状态。保存先锁定草稿头并比对 `expected_version`；版本过期返回 `DRAFT_VERSION_CONFLICT`，锁定草稿拒绝修改。相同业务摘要返回现有版本，不重复插入。调用方必须在同一外层数据库事务内完成其他关联写入并负责 commit/rollback。

`drafts.current_version = 0` 表示尚无修订；首个修订版本为 1。此表当前没有 `task_id` 外键，因为对应任务表尚未交付；后续任务 issue 负责增加关联并在集成边界补齐任务锁与访问范围。当前领域服务要求 `business_operator` 角色，不接受调用方提供 `changed_by`、`confirmed_by` 或批准身份。

## 验证

在仓库根目录执行：

```powershell
Copy-Item code/backend/.env.example code/backend/.env
pwsh -File code/scripts/verify_issue_004.ps1
```

验证入口使用唯一 Compose 项目名和独立 PostgreSQL 卷，显式启用本地演示账号，仅用于隔离集成测试。脚本通过 `uv run --frozen` 运行迁移、检查和 pytest；退出时只清理本次生成的 Compose 项目与卷，并恢复调用 shell 的演示环境变量。需要 Docker Compose、项目依赖可用及本机数据库端口可绑定。此验证不调用在线模型或外部 ERP。

测试覆盖 200×3500、180×3500、半分 ROUND_HALF_UP、精度/浮点拒绝、不完整候选、来源追溯、同内容幂等、版本变化、陈旧版本冲突和角色限制。迁移与测试使用本地隔离 PostgreSQL，结果不代表真实服务或在线模型验收。
