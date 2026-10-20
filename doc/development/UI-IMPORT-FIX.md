# 批量导入入口与前端显示修复

## 问题与处理

- 侧栏“批量导入”仍为早期占位按钮。新增 `/imports` 入口，复用现有 Excel 上传、预览与批次处理流程；在批次详情保持侧栏高亮。
- Excel 面板不再显示文本任务的必填描述及禁用解析按钮，并接入已有模板下载 API。
- 将页面零散字号收敛为统一变量：辅助信息 12px、正文/输入 14px、次级标题 16/18px、面板标题 22px、数字强调 24px、页面标题 28px。品牌主视觉保留独立层级。
- 统一表单字体、下拉框、展开选项、勾选及单选控件。支持 `appearance: base-select` 的浏览器使用统一展开面板；其他浏览器继续使用原生选择行为和基础样式。
- 等待点的 `missing_fields` 实际可能包含 `{field, message}`，原页面按字符串调用 `.match()`，导致任务页渲染失败。现兼容历史字符串与结构化问题，并展示中文字段和原因。
- 本机 `.env` 缺少 MCP 内部签名密钥，真实任务在读取合同时失败。已为本机配置随机共享密钥并重启 API/MCP/Worker，凭据不进入 Git。

## 验证

- `npm --prefix code/frontend run build`：类型检查与构建通过。
- `npm --prefix code/frontend run test:unit`：11 项通过；结构化字段回归测试在修复前复现 2 项 `match is not a function` 失败。
- 浏览器实测上传正常/异常两组 Excel：预览准确定位 `订单明细!J3` 数量错误。
- 使用与当前示例合同一致的螺纹钢、规格、单价文件，正常批次子任务 `b7b5877e-16d0-5d3b-8a9c-00e4dfb9172a` 进入 `WAITING_APPROVAL`；未提交正式订单。
- 规则阻断任务页可正常打开，显示“所选物料未配置在该合同中”，不再停留在读取状态。
- 当前浏览器下拉框实际为 `base-select`，控件字体为 14px；已检查展开面板截图。

可直接上传的合成示例见 [`code/examples/README.md`](../../code/examples/README.md)。最终界面证据保存在 Git 忽略的 `code/artifacts/performance/ui-review-20261020/`。
