# Function Calling 与真实 MCP 执行基线

## 本轮交付边界

`code/backend/app/agent/` 提供 Responses API 模型适配器和受控工具循环。调用方传入已认证用户、部门、任务、持久作业 ID、执行租约、原始需求和带时区的输入时间；运行器通过现有 MCP Streamable HTTP 客户端完成工具发现、输入校验、身份签发、工具调用和输出校验。它不会批准订单或创建正式订单。

本模块是后端执行组件；任务状态图与 Worker 作业处理器由后续 issue 接入。本 issue 的联调验证直接调用该组件，不把页面原型或离线模型替身称为线上模型验收。

## 调用链与安全约束

1. 查询实际 MCP 工具列表，只向模型发布服务端工具白名单内的五种工具及真实输入 JSON Schema。
2. 将任务方向、用户原文、输入时刻和 `Asia/Hong_Kong` 业务时区放入用户数据消息；可信身份不进入模型参数。
3. 通过 OpenAI Responses API 请求模型自动选择工具；默认不在供应商保存 response 状态。
4. 解析 JSON 参数并使用现有严格 Pydantic 契约复核；未知工具、非对象参数、NaN/Infinity 和越界值均在 MCP 副作用前拒绝。
5. 每次 MCP 调用签发短时、单任务、单工具上下文。操作 ID 由持久 job ID、模型轮次和工具序号确定；临时故障重试时重新签名但复用操作 ID。
6. MCP 返回值通过现有客户端输出 Schema 复核，再作为 JSON 数据回填 Responses API；模型指令明确合同/工具返回内容都只是数据。
7. 工具查询、模型调用、MCP 请求与响应、调用耗时、真实模型标识和可用的 Token usage 写入任务轨迹。没有供应商 usage 时存为 unavailable，不补零。

模型工具结果可保存草稿并检查余额；余额检查使用已保存草稿的 ID、版本和金额。没有价格/规格等事实时，模型必须保留缺失，不能编造。模型结果不构成人工批准；超额仍由服务端硬阻断。

## 配置

复制 `code/backend/.env.example` 到本地 `code/backend/.env` 后配置：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `MODEL_PROVIDER` | 空 | 本轮支持 `openai`；未设置时拒绝真实模型执行 |
| `MODEL_NAME` | 空 | 账号可访问且支持 Responses API 工具调用的模型标识 |
| `MODEL_API_KEY` | 空 | 只由服务端环境注入，不返回前端或写入轨迹 |
| `MODEL_BASE_URL` | `https://api.openai.com/v1` | Responses API 根地址，可用于受控兼容网关 |
| `MODEL_TIMEOUT_SECONDS` | `60` | 每次模型/MCP 外部请求超时，范围 0～300 秒（不含 0） |
| `MODEL_MAX_RETRIES` | `2` | 最多额外重试次数，只重试暂时性故障；草稿重试沿用逻辑操作 ID |
| `MODEL_MAX_ROUNDS` | `8` | 单次循环的最大模型轮数 |
| `MODEL_MAX_TOOL_CALLS` | `20` | 单次循环的最大 MCP 工具调用数 |

模型和 MCP 都使用有限重试与同一外部调用超时。参数错误、权限错误和业务校验失败不重试。`/health/config` 只能反映配置是否齐全，不能证明供应商可连接或在线工具选择成功。

## 验证

离线单元验证（不请求真实模型、不调用真实业务数据）：

```powershell
cd code/backend
uv run --frozen python -m pytest -q ../tests/issues/test_issue_012.py -k "not real_mcp and not online_model"
```

隔离 PostgreSQL、示例主数据和 MCP 服务联调：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_012.ps1
```

脚本使用随机 Compose 项目名和临时回环端口，运行迁移、迁移一致性检查、示例数据初始化、真实 MCP 联调及相关 issue 回归；最终只删除本次随机项目及其数据卷，不停止其他容器。没有有效模型凭据时，脚本会明确报告在线模型验收未执行。

当 `MODEL_PROVIDER=openai`、`MODEL_NAME` 和 `MODEL_API_KEY` 均配置后，同一脚本会额外运行在线模型场景。在线测试以隔离数据核验真实模型是否依次选择客户、合同、物料、草稿和余额工具，轨迹是否可回查，以及只产生草稿而不产生批准或订单。该测试会产生供应商 API 用量；当前仓库环境是否有可用凭据应以执行时状态为准。

## 限制与后续衔接

- 本轮实现真实 Responses API 调用组件、预算、输入/输出 Schema 检查、重试和可持久轨迹；Worker 还没有注册此函数循环为任务 job handler。由后续工作流 issue 在持久化状态图中调用，不能把直接组件测试误报为端到端 UI 任务验证。
- 当前 `code/backend/.env` 中 `MODEL_PROVIDER`、`MODEL_NAME`、`MODEL_API_KEY` 为空；因此目前只能验证离线回路和真实 MCP 联调，真实在线模型工具选择仍未验证。
- 模型参数是非严格 JSON Schema 模式以兼容复杂 MCP Schema；所有参数仍会在本地按严格 Pydantic 类型、额外字段禁用及业务规则复核。
