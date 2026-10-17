# PDF 解析与页码证据

附件上传后可由具备原附件读取权限的经办人或审批人调用 `POST /api/v1/artifacts/{artifact_id}/parse`。接口只接受已保存的 `contract_pdf`，重新校验对象 SHA-256 后逐页解析，并写入 `artifact.parsed` 审计事件。未经授权的调用按附件读取规则返回资源不存在。

每页先使用 pypdf 提取文本；缺少至少 12 个非空白字符时才渲染并运行一次本地 Tesseract OCR，混合文档不会对已有文本页重复 OCR。结果包含原文件 SHA-256、页码、路径、原文和文本字符范围或 OCR 像素框；识别不到的页记为 `UNREADABLE`，字段候选缺失保留在 `missing_fields`。Tesseract TSV 的启发式分值未经校准，所以置信度为 `null`，所有候选都显式要求人工确认。合同号、客户、物料、规格、数量、金额、交期使用保守的标签规则提取候选，不做业务主数据映射或草稿写入。

文档文本始终是数据：解析路由不启动 Agent/MCP，不执行文档内的命令或审批请求；结果仅从已鉴权接口返回，并以 `Cache-Control: private, no-store` 禁止缓存，原始附件继续由原内容接口权限控制。OCR 最大处理时间 60 秒，页面渲染上限 4000 万像素；超时或无法读取的页面明确报告异常，整体标为部分结果。

## 运行依赖与授权记录

- 后端 Python 依赖由 `uv` 管理。`pypdfium2` 用于逐页渲染，项目本身为 Apache-2.0/BSD-3-Clause，随发行 wheel 的 PDFium 二进制和第三方组件须一并遵循其附带授权文件。许可证选择依据：[pypdfium2 官方项目授权说明](https://github.com/pypdfium2-team/pypdfium2#licensing)。
- 容器安装 Tesseract OCR、简体中文 `chi_sim` 与英文 `eng` 模型。Tesseract TSV 会输出文本框和启发式分值；来源：[Tesseract 官方文档](https://tesseract-ocr.github.io/tessdoc/)。分值未经校准，本实现不将它作为置信度返回。
- Compose 默认将 `OCR_PROVIDER` 设为 `local_tesseract`；健康配置显示“已配置，尚未验证”且不要求 OCR API 密钥。它表示镜像选择了本地引擎，不替代真实文档评测结果。
- OCR 在容器本地运行，不需要 `OCR_API_KEY` 或外部 OCR 服务；当前不代表已经选定外部供应商，也不承诺识别准确率。生产发布仍需按镜像分发方式保留系统包及随 PDFium wheel 附带的授权材料。

## 隔离验收

从仓库根目录运行 `pwsh -File code/scripts/verify_issue_023.ps1`。脚本使用随机 Compose 项目、独立数据库和附件卷，不连接正在运行的开发服务；清理范围只限本次随机项目。测试覆盖真实容器 Tesseract 的扫描 PDF、文本和混合 PDF、页码/字符范围/bbox 证据、扫描页不可读和字段缺失、提示注入作为原文数据、跨部门读取拒绝、解析审计及未创建任务/审批/订单。需 Docker、`code/backend/.env` 中的本地隔离数据库配置及镜像/套件网络。
