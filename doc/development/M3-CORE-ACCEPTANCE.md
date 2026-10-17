# M3 核心验收索引（AC-01～AC-20）

本索引按 PRD 的验收标准列出可运行的既有回归和 M3-07 端到端验证入口。原型、固定夹具和离线测试只证明相应离线或服务集成范围；在线模型结果以隔离环境中的真实调用为准。

| 验收项 | 可复查覆盖 | 状态边界 |
|---|---|---|
| AC-01 | #3/#7/#9/#10/#14/#15/#16/#19/#20；`test_issue_003.py`、`test_issue_009.py`、`test_issue_014.py`、`test_issue_015.py`、`test_issue_016.py`、`test_issue_019.py`、`test_issue_020.py` | 权限和部门越权由真实 API/数据库用例验证。 |
| AC-02 | #8/#13；`test_issue_008.py`、`test_issue_013.py` | 任务、会话、草稿隔离及身份恢复由数据库事实断言。 |
| AC-03 | #13/#21；`test_issue_013.py` 与 `verify_issue_021.ps1 -LiveModel` | 本入口会在真实 `WAITING_INPUT` 和 `WAITING_APPROVAL` 各重启 Worker 容器 10 次并逐次回查原任务；附件/全量轨迹不在 M3 结论内。 |
| AC-04 | #12/#14/#21；`test_issue_012.py`、`test_issue_014_live.py` 和在线钢材流程 | 自然语言、真实模型工具选择、主数据价格及解析后的草稿字段会留在实际任务轨迹。相对日期精确解释不作为本轮已验收结果。 |
| AC-05 | #12/#13/#21；`test_issue_012.py`、`test_issue_013.py` 和在线钢材流程 | 缺字段等待、补充输入、旧检查点恢复及后续审批由任务状态和草稿版本核验。 |
| AC-06 | #9/#12；`test_issue_009.py`、`test_issue_012.py` | 工具 Schema 的类型、必填、越界及无副作用由真实 MCP 服务集成用例验证。 |
| AC-07 | #12/#14/#21；`test_issue_012.py`、`test_issue_014_live.py` 和在线钢材流程 | 在线入口核对真实模型跨度和 `query_customer`、`get_contract`、`match_material`、`save_order_draft`、`check_contract_balance` 成功调用；关闭服务错误在 #12 集成用例覆盖。 |
| AC-08 | #5/#10/#20；`test_issue_005.py`、`test_issue_010.py`、`test_issue_020.py` | 合同方向/状态/部门、客户/物料歧义及主数据错误的规则结果有服务端断言。 |
| AC-09 | #5/#15；`test_issue_005.py`、`test_issue_015.py` | 金额、数量、单位、日期和阻断批准的规则测试。 |
| AC-10 | #14/#18；`test_issue_014.py`、`test_issue_018.py` | 草稿版本、冲突、编辑重算及任务快照由 API/数据库用例验证。 |
| AC-11 | #5/#15/#16/#18；`test_issue_005.py`、`test_issue_015.py`、`test_issue_016.py`、`test_issue_018.py` | 超额、等额、批准硬阻断、合同预览与持久额度覆盖。 |
| AC-12 | #16；`test_issue_016.py` | 并发竞争通过 PostgreSQL 事务和订单/额度记录验证，不以 UI 结果代替。 |
| AC-13 | #15/#21；`test_issue_015.py` 和在线钢材流程 | 真实审批等待、拒绝/退回/批准，以及批准后提交由服务端工作流验证。 |
| AC-14 | #15/#16/#17；`test_issue_015.py`、`test_issue_016.py`、`test_issue_017.py` | 修改失效旧批准、旧版本拒绝及恢复绑定校验。 |
| AC-15 | #16/#17/#21；`test_issue_016.py`、`test_issue_017.py` 和在线钢材流程 | 幂等并发及重放通过唯一订单/额度占用断言；在线流程复核一笔真实批准订单。 |
| AC-16 | #17；`test_issue_017.py` | 失败/未知提交回查与重试只重用原订单、作业和额度占用；故障注入不会伪装成在线模型证据。 |
| AC-17 | #19/#21；`test_issue_019.py` 和在线钢材流程 | 类型/合同列表、订单详情、明细金额、批准快照和任务引用一致。 |
| AC-18 | #20；`test_issue_020.py` | 管理员别名、停用合同最终阻断及历史订单快照由数据库事实验证。 |
| AC-19 | #6/#17/#21；`test_issue_006.py`、`test_issue_017.py` 和在线钢材流程 | 状态事件、工具跨度、正式订单与审计关联按持久轨迹回查；文件阶段继续补充。 |
| AC-20 | #6/#19/#20；`test_issue_006.py`、`test_issue_019.py`、`test_issue_020.py` | **部分：**M3 覆盖字段来源、批准人、订单和操作审计，并验证订单详情不暴露原文/对象 ID；附件原文/哈希授权和下载隔离由 M4 #22 补验，不宣称本期完整通过。 |

## 验证入口

离线/服务集成回归：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_021.ps1
```

先运行默认离线命令。在线阶段重复除“模型必须未配置”之外的回归（该状态断言已由默认离线命令通过），然后执行真实在线钢材流程、两种等待态各 10 次 Worker 容器重启、人工审批和正式订单回查：

```powershell
pwsh -NoProfile -File code/scripts/verify_issue_021.ps1 -LiveModel
```

在线模式要求 `code/backend/.env` 已配置 DeepSeek 的 provider、model name 和密钥。脚本不显示密钥值，使用独立 PostgreSQL 卷、临时演示密码、随机端口和随机 MCP 签名密钥；结束时仅移除本次随机 Compose 项目。模型调用会产生供应商侧实际用量。若模型/API 返回错误，脚本失败并输出隔离服务日志，不记录为通过。

在线脚本通过时会输出本次隔离运行的 `task_id` 和 `order_id`，可将其与命令输出、订单明细及审计记录一起作为验收引用；隔离数据库及附件卷随后删除，不保留临时业务数据。

AC-01～AC-19 的结果按具体 issue 回归记录；AC-20 仍部分验收。M4 文件解析、M5 双模式/批量、M6 自动评测及性能/稳定性项目不计入 M3 已交付范围。
