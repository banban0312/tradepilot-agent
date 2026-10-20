# TradePilot 验收证据索引

本文把 PRD、Spec、25 个原型画板和仓库中可运行的实现/测试入口对应起来。原型是交互设计证据，不能单独证明业务功能通过；测试文件是验证入口，真实通过结果须以对应 PR、报告和运行环境为准。

## 范围与状态

- 覆盖 PRD 的 FR-01～FR-28、AC-01～AC-27、NFR-01～NFR-14，以及 `doc/design/manifest.json` 中 25 个功能画板。
- M1～M6 前 41 个 issue 已合并；M6-07（#42）正在汇总运行交付资料。
- M6-06 的页面反馈、性能、浏览器和备份恢复短验收已通过；8 小时观察按用户决定约运行 2 小时后停止，明确为“豁免、未完成”。因此 NFR-11 的 8 小时条款没有通过证据。
- 所有运行报告位于 Git 忽略的 `code/artifacts/`；生产环境 SLA、生产部署、安全认证与真实 ERP 对接不在已验证结论内。

## 六项核心能力

| 能力 | 实际实现 | 可复验入口 | 代表性证据与限制 |
|---|---|---|---|
| LangGraph 状态化工作流 | [`workflow.py`](../code/backend/app/agent/workflow.py)、[`checkpointer.py`](../code/backend/app/tasks/checkpointer.py)、任务队列与 Worker | [#8](../code/tests/issues/test_issue_008.py)、[#13](../code/tests/issues/test_issue_013.py)、[#40](../code/tests/issues/test_issue_040.py) | 两个等待点持久化及恢复；#40 的隔离 Compose Worker 重启各 10 次。 |
| Function Calling + MCP | [`function_calling.py`](../code/backend/app/agent/function_calling.py)、[`mcp_server/`](../code/backend/app/mcp_server/) | [#12](../code/tests/issues/test_issue_012.py)、[#35](../code/tests/issues/test_issue_035.py)、[`在线验收入口`](../code/scripts/verify_issue_014.ps1) | #35/#41 记录真实模型与 MCP 调用；离线夹具结果单独标记，不替代在线模型质量。 |
| 多模态提取 | [`parsing/`](../code/backend/app/parsing/)、附件与导入服务 | [#27](../code/tests/issues/test_issue_027.py)、[#28](../code/tests/issues/test_issue_028.py)、[#29](../code/tests/issues/test_issue_029.py)、[#33](../code/tests/issues/test_issue_033.py) | PDF 文本/扫描页、Tesseract 图片 OCR、字段证据、Excel 分组均有可执行断言；OCR 结果需人工确认。 |
| Human-in-the-loop | 草稿版本、审批等待、订单事务 | [#15](../code/tests/issues/test_issue_015.py)、[#19](../code/tests/issues/test_issue_019.py)、[#20](../code/tests/issues/test_issue_020.py)、[#21](../code/tests/issues/test_issue_021.py) | 审批绑定版本；未批准或额度不足不产生正式订单与占额。 |
| 可观测性 | [`observability/service.py`](../code/backend/app/observability/service.py)、历史任务与轨迹 API | [#6](../code/tests/issues/test_issue_006.py)、[#33](../code/tests/issues/test_issue_033.py)、[#34](../code/tests/issues/test_issue_034.py) | 时间线、工具、状态、人工等待、脱敏与 Token unavailable 语义有回归覆盖。 |
| 自动评测 | [`code/evals/`](../code/evals/)、评测中心 API/页面 | [#36](../code/tests/issues/test_issue_036.py)、[#37](../code/tests/issues/test_issue_037.py)、[#38](../code/tests/issues/test_issue_038.py)、[#39](../code/tests/issues/test_issue_039.py) | 固定 v1 共 41 例、六类分组、离线/在线分开、JSON/JSONL/Markdown 报告。在线结果不可与离线夹具合并计算。 |

## 功能需求 FR-01～FR-28

测试入口按需求逐项列出；相邻需求可能共享服务与回归。Issue 编号对应 GitHub 上已经合并的实现成果。

| 需求 | 实现/测试证据 | 画板 |
|---|---|---|
| FR-01 登录、角色与部门权限 | [#3](../code/tests/issues/test_issue_003.py)、[#39](../code/tests/issues/test_issue_039.py) | 10、16、18、24 |
| FR-02 创建及继续任务 | [#13](../code/tests/issues/test_issue_013.py)、[#14](../code/tests/issues/test_issue_014.py) | 11、22 |
| FR-03 状态持久化与恢复 | [#8](../code/tests/issues/test_issue_008.py)、[#13](../code/tests/issues/test_issue_013.py)、[#40](../code/tests/issues/test_issue_040.py) | 02、06、11、17 |
| FR-04 文本意图与抽取 | [#12](../code/tests/issues/test_issue_012.py)、[#14](../code/tests/issues/test_issue_014.py) | 01、22 |
| FR-05 缺失/歧义澄清 | [#13](../code/tests/issues/test_issue_013.py)、[#15](../code/tests/issues/test_issue_015.py) | 02、12 |
| FR-06 严格工具类型契约 | [#11](../code/tests/issues/test_issue_011.py)、[#12](../code/tests/issues/test_issue_012.py) | 06、16、24 |
| FR-07 Function Calling 与 MCP | [#12](../code/tests/issues/test_issue_012.py)、[#35](../code/tests/issues/test_issue_035.py) | 06、16、17 |
| FR-08 客户/合同/物料关联 | [#5](../code/tests/issues/test_issue_005.py)、[#10](../code/tests/issues/test_issue_010.py) | 01、08、12、15 |
| FR-09 字段校验 | [#5](../code/tests/issues/test_issue_005.py)、[#12](../code/tests/issues/test_issue_012.py) | 02、12、13、24 |
| FR-10 双向编辑与版本冲突 | [#18](../code/tests/issues/test_issue_018.py)、[#25](../code/tests/issues/test_issue_025.py) | 01、04、11、25 |
| FR-11 额度与并发保护 | [#5](../code/tests/issues/test_issue_005.py)、[#20](../code/tests/issues/test_issue_020.py)、[#40](../code/tests/issues/test_issue_040.py) | 01、03、08、21、24 |
| FR-12 人工批准/退回/拒绝 | [#15](../code/tests/issues/test_issue_015.py)、[#19](../code/tests/issues/test_issue_019.py) | 09、19、20、24 |
| FR-13 草稿变更使审批失效 | [#15](../code/tests/issues/test_issue_015.py)、[#20](../code/tests/issues/test_issue_020.py) | 04、19、25 |
| FR-14 幂等提单 | [#19](../code/tests/issues/test_issue_019.py)、[#20](../code/tests/issues/test_issue_020.py)、[#21](../code/tests/issues/test_issue_021.py) | 09、14、17、21、24 |
| FR-15 超时重试与恢复 | [#17](../code/tests/issues/test_issue_017.py)、[#23](../code/tests/issues/test_issue_023.py)、[#40](../code/tests/issues/test_issue_040.py) | 11、17、21 |
| FR-16 订单列表与详情 | [#19](../code/tests/issues/test_issue_019.py)、[#24](../code/tests/issues/test_issue_024.py) | 04、14 |
| FR-17 主数据与运行参数 | [#2](../code/tests/issues/test_issue_002.py)、[#26](../code/tests/issues/test_issue_026.py)、[#34](../code/tests/issues/test_issue_034.py) | 08、15、16 |
| FR-18 运行轨迹 | [#6](../code/tests/issues/test_issue_006.py)、[#33](../code/tests/issues/test_issue_033.py) | 06、17、23 |
| FR-19 来源证据与审计 | [#6](../code/tests/issues/test_issue_006.py)、[#27](../code/tests/issues/test_issue_027.py)、[#33](../code/tests/issues/test_issue_033.py) | 12、14、23 |
| FR-20 PDF 解析 | [#27](../code/tests/issues/test_issue_027.py)、[#28](../code/tests/issues/test_issue_028.py) | 12、18、22 |
| FR-21 图片 OCR | [#22](../code/tests/issues/test_issue_022.py)、[#28](../code/tests/issues/test_issue_028.py) | 03、12、22、23 |
| FR-22 Excel 模板与解析 | [#22](../code/tests/issues/test_issue_022.py)、[#29](../code/tests/issues/test_issue_029.py)、[#30](../code/tests/issues/test_issue_030.py) | 05、13、22 |
| FR-23 物料纠错与证据确认 | [#28](../code/tests/issues/test_issue_028.py)、[#33](../code/tests/issues/test_issue_033.py) | 03、12、15 |
| FR-24 批次子任务与审批 | [#29](../code/tests/issues/test_issue_029.py)、[#30](../code/tests/issues/test_issue_030.py)、[#40](../code/tests/issues/test_issue_040.py) | 05、13、20、21 |
| FR-25 悬浮窗与全屏共享状态 | [#32](../code/frontend/tests/issues/issue_032.test.ts) | 01、04 |
| FR-26 轨迹统计与历史查询 | [#6](../code/tests/issues/test_issue_006.py)、[#33](../code/tests/issues/test_issue_033.py)、[#37](../code/tests/issues/test_issue_037.py) | 06、11、17 |
| FR-27 固定集与自动评测 | [#36](../code/tests/issues/test_issue_036.py)、[#37](../code/tests/issues/test_issue_037.py)、[#39](../code/tests/issues/test_issue_039.py) | 07、24 |
| FR-28 指标及失败报告 | [#37](../code/tests/issues/test_issue_037.py)、[#38](../code/tests/issues/test_issue_038.py)、[#39](../code/tests/issues/test_issue_039.py) | 07、24 |

## 验收标准 AC-01～AC-27

下表与原型覆盖表的 AC 口径一致，提供主要可重复测试入口。#35/#40/#41 的隔离综合验收会另外重跑多项回归。

| 验收项 | 可复验测试入口 |
|---|---|
| AC-01 部门越权拒绝 | [#3](../code/tests/issues/test_issue_003.py)、[#39](../code/tests/issues/test_issue_039.py) |
| AC-02 新建/恢复任务及隔离 | [#8](../code/tests/issues/test_issue_008.py)、[#14](../code/tests/issues/test_issue_014.py) |
| AC-03 两个等待点重启后恢复 | [#13](../code/tests/issues/test_issue_013.py)、[#40](../code/tests/issues/test_issue_040.py) |
| AC-04 文本字段与固定时钟解析 | [#10](../code/tests/issues/test_issue_010.py)、[#12](../code/tests/issues/test_issue_012.py) |
| AC-05 多规格进入澄清 | [#12](../code/tests/issues/test_issue_012.py)、[#28](../code/tests/issues/test_issue_028.py) |
| AC-06 工具参数 Schema 拒绝 | [#11](../code/tests/issues/test_issue_011.py)、[#12](../code/tests/issues/test_issue_012.py) |
| AC-07 真实模型选择 MCP 工具 | [#12](../code/tests/issues/test_issue_012.py)、[#35](../code/tests/issues/test_issue_035.py) |
| AC-08 合同方向、状态、权限与候选 | [#5](../code/tests/issues/test_issue_005.py)、[#10](../code/tests/issues/test_issue_010.py) |
| AC-09 缺失/非法字段与日期拦截 | [#5](../code/tests/issues/test_issue_005.py)、[#12](../code/tests/issues/test_issue_012.py) |
| AC-10 草稿同步、保存及版本冲突 | [#18](../code/tests/issues/test_issue_018.py)、[#25](../code/tests/issues/test_issue_025.py) |
| AC-11 超额硬阻断与等额允许 | [#5](../code/tests/issues/test_issue_005.py)、[#20](../code/tests/issues/test_issue_020.py) |
| AC-12 并发竞争只允许余额内订单 | [#20](../code/tests/issues/test_issue_020.py)、[#40](../code/tests/issues/test_issue_040.py) |
| AC-13 人工批准、退回、拒绝 | [#15](../code/tests/issues/test_issue_015.py)、[#19](../code/tests/issues/test_issue_019.py) |
| AC-14 编辑使旧审批失效 | [#15](../code/tests/issues/test_issue_015.py)、[#20](../code/tests/issues/test_issue_020.py) |
| AC-15 幂等请求只生成一单 | [#19](../code/tests/issues/test_issue_019.py)、[#20](../code/tests/issues/test_issue_020.py) |
| AC-16 故障恢复及未知提交结果 | [#17](../code/tests/issues/test_issue_017.py)、[#40](../code/tests/issues/test_issue_040.py) |
| AC-17 订单列表与详情一致 | [#19](../code/tests/issues/test_issue_019.py)、[#24](../code/tests/issues/test_issue_024.py) |
| AC-18 主数据/配置即时生效与追溯 | [#26](../code/tests/issues/test_issue_026.py)、[#34](../code/tests/issues/test_issue_034.py) |
| AC-19 成功/等待/失败轨迹 | [#6](../code/tests/issues/test_issue_006.py)、[#33](../code/tests/issues/test_issue_033.py) |
| AC-20 文件来源、哈希及审批审计 | [#27](../code/tests/issues/test_issue_027.py)、[#33](../code/tests/issues/test_issue_033.py)、[#41](../code/tests/issues/test_issue_041.py) |
| AC-21 文本/扫描 PDF 与失败输入 | [#27](../code/tests/issues/test_issue_027.py)、[#28](../code/tests/issues/test_issue_028.py) |
| AC-22 图片识别、歧义和确认 | [#28](../code/tests/issues/test_issue_028.py)、[#33](../code/tests/issues/test_issue_033.py) |
| AC-23 Excel 分组、源行和异常定位 | [#29](../code/tests/issues/test_issue_029.py)、[#30](../code/tests/issues/test_issue_030.py) |
| AC-24 批量部分成功及安全重试 | [#29](../code/tests/issues/test_issue_029.py)、[#30](../code/tests/issues/test_issue_030.py)、[#40](../code/tests/issues/test_issue_040.py) |
| AC-25 悬浮窗/全屏共享任务状态 | [前端共享状态测试](../code/frontend/tests/issues/issue_032.test.ts) |
| AC-26 模型/工具/等待和 Token 指标 | [#6](../code/tests/issues/test_issue_006.py)、[#33](../code/tests/issues/test_issue_033.py)、[#38](../code/tests/issues/test_issue_038.py) |
| AC-27 六类评测与失败报告 | [#36](../code/tests/issues/test_issue_036.py)、[#37](../code/tests/issues/test_issue_037.py)、[#38](../code/tests/issues/test_issue_038.py)、[#39](../code/tests/issues/test_issue_039.py) |

## 非功能需求 NFR-01～NFR-14

“部分”表示有针对性样本或隔离环境证据，但测量口径没有覆盖 PRD 的全部生产级环境/负载，不得扩展为生产 SLA。

| NFR | 证据与结论 | 状态 |
|---|---|---|
| NFR-01 页面响应 | #41 浏览器端测 8 个 Chrome/Edge 版本×视口样本；反馈 P95 9.7 ms。恢复 20 个过期任务/批次的 P95 122.994 ms，但不是 20 个持续活跃业务任务负载。 | 部分通过 |
| NFR-02 业务性能 | #41 对 `GET /api/v1/admin/contracts?limit=100` 测 100 次，P95 9.368 ms；未对 PRD 列出的每种查询、草稿写入和规则检查端点逐一采样。 | 部分通过 |
| NFR-03 外部调用 | 模型超时/不可用有明确失败测试；#41 真实 DeepSeek 草稿链路已走通。没有足够样本证明文本、OCR 外部耗时 P95 建议值。 | 部分通过 |
| NFR-04 文件容量 | #41 断言图片 10 MB、PDF 20 MB/20 页、XLSX 10 MB/500 行配置；文件验证见 #22/#27/#28。 | 通过配置与边界检查 |
| NFR-05 批量性能 | #41 100 行 XLSX HTTP 201、0.1872 秒；#29 批次测试验证 5 路并发上限与混合组。 | 通过隔离样本 |
| NFR-06 权限与规则 | #3/#5/#15/#20/#37/#40 覆盖越权、未审批、超额和额度竞争；41 例离线评测独立核对安全断言。 | 通过隔离验收 |
| NFR-07 并发一致性 | #20/#40 覆盖相同幂等请求和不同订单竞争额度，核对订单、审批和占额事实。 | 通过隔离验收 |
| NFR-08 持久化恢复 | #40 两个等待点的 Worker 重启各 10/10；#41 备份还原后同一任务从 `WAITING_INPUT` 续至 `WAITING_APPROVAL`。 | 通过隔离验收 |
| NFR-09 安全处理 | #3/#6/#22/#27/#34/#36 覆盖授权、脱敏、附件鉴权、哈希和凭据扫描；生产 HTTPS/部署安全未验收。 | 部分通过 |
| NFR-10 可观测性 | #6/#33/#38 覆盖模型/工具/审批/状态关联、错误码、毫秒耗时和 Token unavailable。 | 通过功能验收 |
| NFR-11 故障与可用性 | 故障显式失败有验证；8 小时长稳于 2026-10-20 约运行 2 小时后按用户决定提前停止，没有 8 小时报告。 | **未完成（用户豁免）** |
| NFR-12 兼容性 | #41 Chrome 154/155、Edge 154/155 在 1366×768 与 1920×1080 通过登录、订单中心、工作台、横向溢出和交互反馈检查。 | 通过本机矩阵 |
| NFR-13 识别质量 | #28/#33 测固定标注 PDF/图片、候选歧义、字段覆盖/精确匹配；报告按样本区分，不将夹具成绩称为在线模型准确率。 | 隔离样本通过；在线质量分开披露 |
| NFR-14 数据保留 | #6/#34 验证 30 天默认值、可配置留存和活动任务/订单证据保护。生产留存期及数据地域未确认。 | 部分通过 |

## 25 个原型画板

图片文件和画板元数据以 [`manifest.json`](./design/manifest.json) 为准。设计到需求的完整行级映射见 [`ACCEPTANCE-COVERAGE.md`](./design/ACCEPTANCE-COVERAGE.md)；对应实现及测试见上方 FR/AC 索引。

| 画板 | 画板主题 | 实现验收入口 |
|---:|---|---|
| 01 | 智能提单 / 待人工批准 | #12、#15、#18 |
| 02 | 缺字段 / 原任务恢复 | #13、#15、#40 |
| 03 | OCR 识别 / 超额阻断 | #5、#20、#28 |
| 04 | 订单中心 / 悬浮窗 | #19、#24、#32 |
| 05 | Excel 批量导入 / 异常定位 | #22、#29、#30 |
| 06 | Agent 执行追踪 / MCP IO | #6、#12、#33、#35 |
| 07 | 评测中心 / 六类案例 | #36～#39 |
| 08 | 合同台账 / 主数据 | #2、#5、#26 |
| 09 | 人工审批 / 版本绑定 | #15、#19、#20 |
| 10 | 身份登录 / 角色部门范围 | #3、#39 |
| 11 | 历史任务 / 草稿载入恢复 | #13、#14、#33、#40 |
| 12 | PDF/图片证据确认 | #27、#28、#33 |
| 13 | Excel 行内修正 | #29、#30 |
| 14 | 订单详情 / 审批审计 | #19、#24、#33 |
| 15 | 标准物料 / 别名编码 | #2、#10、#26、#28 |
| 16 | 模型/MCP/规则/操作审计 | #12、#34、#35 |
| 17 | 失败定位 / 安全恢复 | #17、#23、#40 |
| 18 | 加载/空白/权限/错误状态 | #3、#18、#39 |
| 19 | 批准/退回/拒绝与失效 | #15、#19、#20 |
| 20 | 批量逐笔批准 | #29、#30、#35 |
| 21 | 批次结果 / 部分成功 | #29、#30、#35、#40 |
| 22 | 新建任务 / 帮助入口 | #13、#22、#37 |
| 23 | 操作审计 / 输入哈希 | #6、#33、#34 |
| 24 | 规则核验 / 并发与工具约束 | #5、#11、#20、#37、#40 |
| 25 | 草稿预览编辑 / 重新审批 | #18、#20、#25 |

## 验收命令与运行证据

| 范围 | 命令 | 报告/证据 |
|---|---|---|
| 基础环境与 MCP/检查点 | `pwsh -NoProfile -File code/scripts/verify_issue_001.ps1` | 脚本输出、隔离服务日志；需 Docker，不需模型密钥。 |
| 完整离线评测 | `pwsh -NoProfile -File code/scripts/verify_issue_037.ps1` | `code/artifacts/evals/<本次项目>/summary.json`、`cases.jsonl`、`report.md`；M6-05 验收记录 41/41 PASS。 |
| 真实模型验收 | `pwsh -NoProfile -File code/scripts/verify_issue_014.ps1 -LiveModel` | 本机忽略的性能/评测报告目录；需用户配置模型服务，可能产生用量。 |
| M6-06 短验收 | `pwsh -NoProfile -File code/scripts/verify_issue_041.ps1 -SkipSoak` | `code/artifacts/performance/`；浏览器、API、恢复和附件 JSON/截图。 |
| #40 并发和双等待点恢复 | `pwsh -NoProfile -File code/scripts/verify_issue_040.ps1` | 隔离评测报告及两个等待态各 10 次 Worker 服务重启记录。 |

所有报告目录均由 `.gitignore` 排除。提交文档与脚本，不提交运行产生的报告、截图、密钥或用户数据。
