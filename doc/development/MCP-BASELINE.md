# MCP 服务与工具契约

本文记录 M2-02 的官方 MCP SDK Streamable HTTP 服务、客户端、工具 Schema 和内部身份上下文，M2-03 的真实只读主数据查询，以及 M2-04 的真实草稿保存、版本和幂等控制。相关依据为 PRD FR-01/06/07/08/10/13/19、AC-01/06/07/08/10/14/20、NFR-02/06/09/10，Spec §3.7、§5.4、§5.5、§6.1、§8.1，以及原型 06、08、15、16、24、25。

## 运行方式

Compose 服务地址为 `http://mcp:8000/mcp`；本机映射默认为 `http://127.0.0.1:8001/mcp`。MCP 服务只绑定回环地址端口，容器间通过 Compose 私有网络通信。官方 SDK 的 Streamable HTTP transport 启用 DNS rebinding 防护，并限制 `Host` 为配置端口上的 `mcp`、`localhost` 或 `127.0.0.1`；不接受浏览器跨域 Origin。

合同有效期按 Spec 的业务时区 `BUSINESS_TIMEZONE` 判断，默认 `Asia/Hong_Kong`；部署环境可在 `code/backend/.env` 覆盖为有效 IANA 时区名称。

后端依赖通过 `uv` 和 `code/backend/uv.lock` 管理。本地 Compose 设置来自 Git 忽略的 `code/backend/.env`，可由 `code/backend/.env.example` 建立。Worker 与 MCP 容器都读取 `MCP_CONTEXT_SIGNING_KEY`；调用业务工具前必须配置至少 32 字节的随机密钥。PowerShell 可生成十六进制密钥：

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

将输出写入本机 `code/backend/.env` 的 `MCP_CONTEXT_SIGNING_KEY`，不要将该密钥提交到仓库。仅 `health_check` 不需要业务身份；无密钥时受保护工具返回明确鉴权失败，服务不会把它们伪装成成功。

## 工具清单与当前实现边界

五个业务工具均通过 `tools/list` 发布输入 Schema 和结构化输出 Schema。对象禁止额外属性，字符串、整数、UUID、日期等字段使用严格类型；金额以固定两位小数的十进制字符串表达，草稿数量/单价也以十进制字符串表达。服务端对 MCP 原始参数执行 Pydantic strict 校验，避免 SDK 参数适配层忽略额外字段或进行类型转换。

| 工具 | 主要输入 | 预期输出 | 副作用与当前状态 |
|---|---|---|---|
| `query_customer` | `name` 或 `code` 至少一项；`limit` 1～10 | 当前任务部门内有效客户候选及匹配依据；无结果返回 `CUSTOMER_NOT_FOUND` | 只读；按可信部门和有效状态过滤 |
| `match_material` | `raw_name`，可选规格和单位 | 标准物料候选、别名匹配依据及待确认状态 | 只读；多候选或未提供规格时返回确认问题，不自动补直径/型号 |
| `get_contract` | 合同编号、`SALES`/`PURCHASE` | 权限内合同、签约客户、项目/地址、合同物料行、价格、数量与状态 | 只读；方向、停用或按业务时区计算的有效期问题明确返回；跨部门合同不泄露 |
| `check_contract_balance` | 合同/草稿 UUID、版本、两位小数金额字符串 | 总额、占用、剩余、本次金额、允许标记、校验时间和原因 | 只读；要求当前草稿版本、创建人、合同和自报金额一致；不预占额度 |
| `save_order_draft` | 可信任务 ID、预期版本、候选订单、证据引用 | 草稿 ID、版本、内容哈希、校验状态、总额和字段问题 | 已通过真实 MCP 保存到任务唯一草稿；任务锁、版本锁和 `operation_id` 防重，同操作不同参数返回冲突；不创建审批或正式订单 |

`health_check` 是独立的无业务探针。它只能证明 MCP 进程和传输可用，不能代表五个业务工具已接通，也不能代表模型服务可用。原型中的成功调用、业务记录和 Token 数值均为展示示例，不作为本轮验收数据。

## 身份、授权与追踪

Worker 只能使用已验证的 `AuthContext`、部门范围和实际 `TaskExecutionLease` 生成短期签名上下文。HMAC-SHA256 保护规范化 JSON 声明，字段包括 actor、department、task、execution epoch、允许工具、operation ID、trace ID、模型 tool call ID 和有效时间。有效期最多 60 秒；目标固定为 `tradepilot-mcp`。令牌仅经 `X-TradePilot-Context` 服务端请求头发送，绝不放入模型参数、输入轨迹或日志。

MCP 每次工具调用都重新读取数据库中的用户状态、角色、部门授权、任务所有者/部门及当前 execution epoch。`save_order_draft.task_id` 还须匹配签名任务。身份、角色和部门字段出现在请求参数时，严格 Schema 会拒绝；已失效 epoch、无权限或无签名也会明确拒绝。正式订单批准和创建不属于模型工具集。

本轮对经过身份验证的业务调用写入真实 `TraceSpan` 和任务事件；轨迹关联 task、actor、trace、MCP request ID、模型 tool call ID 与 operation ID，只保存规范化参数哈希、结果哈希、授权范围内的来源记录引用及错误码，不保存原始查询内容、返回的客户/合同字段、签名令牌或密钥。M2-03 四项查询从 PostgreSQL 读取权威数据；余额工具只按已保存的当前草稿版本与合同余额返回预检，不占用额度。M2-04 草稿保存将草稿版本、`tool_effects` 原响应、审计和成功轨迹放在同一数据库事务内；重放使用持久逻辑 `operation_id`，不能仅依赖 MCP/模型新生成的 call ID。草稿不完整时仍可保存，但输出 `is_validated=false` 和字段问题；模型工具不能批准草稿或创建正式订单。

`is_validated` 仅表示草稿结构、必填字段和金额规则通过 M1-04 的确定性校验，不代表主数据关系、合同状态或实时额度检查通过；后续流程仍须执行相应工具和规则节点。

这是内部服务间短期签名认证，不是面向公共 MCP 客户端的完整 OAuth。若将来开放外部 MCP 客户端，须另行设计发现、授权和凭据续期流程。

## 验证入口

M2-02 协议、签名和身份上下文入口：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_009.ps1
```

M2-03 查询服务和回归入口：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_010.ps1
```

M2-04 草稿保存、版本、操作幂等与并发入口：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_011.ps1
```

入口要求 Docker Desktop Linux 容器引擎和本机 `code/backend/.env`。脚本生成一次性签名密钥、随机端口和 Compose 项目名，启动隔离 PostgreSQL，执行 Alembic `upgrade`/`check`、示例数据初始化，再对真实 Streamable HTTP 服务运行 #4/#6/#8/#9/#10/#11 回归。#10 测试会删除自己创建的临时记录；#11 保留追加式审计和任务证据直到隔离数据库销毁。所有测试只使用随机 ID 和本次专属 Compose 数据卷，脚本最后只清理本次项目的容器、网络和卷。

Issue #9 原验证入口仍可独立执行：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_009.ps1
```

验证覆盖工具发现和 Schema、健康探针、缺少/篡改/过期签名、权限与 epoch 复核、额外身份字段、金额格式/范围、结构化错误、真实 MCP 输入/输出及调用轨迹、断连；还覆盖客户部门隔离、物料候选歧义/规格确认、合同方向/停用状态/部门范围、草稿金额和版本绑定、余额计算、草稿持久化与重载、同操作原响应重放、同操作不同参数冲突、版本冲突和并发竞争。保存成功仍不会创建审批或正式订单。在线模型评测尚未执行。
