# 图片 OCR 与字段证据

## 使用入口

图片先通过 `POST /api/v1/artifacts` 上传，`parse_intent=receipt_image` 仅接受已验证的 JPG/JPEG 或 PNG。获得附件读取权限的业务经办人或审批人可调用 `POST /api/v1/artifacts/{artifact_id}/parse`。接口沿用 CSRF、部门/任务附件授权和存储哈希检查；原图仍只能经鉴权内容接口访问。

解析在 `OCR_PROVIDER=local_tesseract` 时使用本机 Tesseract `chi_sim+eng`；当前选择其他尚无适配器的供应商时明确返回 `OCR_UNAVAILABLE`，不会静默切换。结果包含原图 SHA-256、OCR 引擎/实际版本、耗时、原始逐行识别文本、像素框、字段候选和缺失字段。没有校准可信分值时，`confidence` 保持 `null`；候选一律要求人工确认。多个不同值标记 `CONFLICT`。支持合同号、客户、物料、规格、数量/单位、金额、交期、收货人和送货地址的保守标签提取；OCR 只提供候选，不查询主数据、不保存草稿、不审批或建订单。

无文本图片标记 `PARTIAL` 并返回 `IMAGE_UNREADABLE`。引擎缺失、超时、运行错误、哈希不符和权限错误使用稳定错误码；具有附件读取权限时，解析失败审计记录输入哈希、错误码和耗时，不包含图片内容。接口响应为 `private, no-store`。

## 验证

在仓库根目录运行：

```powershell
./code/scripts/verify_issue_024.ps1
```

脚本使用随机 Compose 项目、独立数据库和附件卷、随机演示口令和专用镜像；结束时仅清理自身容器、卷和镜像，并恢复当前 PowerShell 进程环境变量。它不复用或重启主开发容器，也不会继承模型/OCR API 凭据。需要 Docker、`code/backend/.env` 中可用的数据库构建参数以及镜像源网络。

稳定测试入口为 `code/tests/issues/test_issue_024.py`，验证真实本地 Tesseract PNG 输入、哈希/框/版本/耗时、字段候选/冲突、失败路径、鉴权、审计和订单/审批/任务无副作用；同一脚本回归 #001、#005、#010、#019、#022、#023。#005/#010 验证 15 万元可用额度对 22 万元候选的硬阻断。它不调用在线模型或外部 OCR 供应商，也不构成生产图像识别准确率基线。
