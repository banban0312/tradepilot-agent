# 工程基线与兼容性决策

此基线依 M1-01 的实际解析和容器检查建立，日期为 2026-10-16。Python 与 Node 的直接/间接依赖以 `uv.lock` 和 `package-lock.json` 为精确版本权威；容器映像使用 OCI 多架构清单摘要固定内容。更新任一依赖或映像时应重新执行兼容性验证并更新本文件。

## 执行环境

| 元件 | 锁定方式 | 实际用途 |
|---|---|---|
| Python | `>=3.12,<3.13`；uv 项目版本为 `3.12`；Compose 基础映像 `python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` | 本机验证采用 uv 管理 Python；容器映像摘要含 Python 3.12.15 |
| uv | 本机已验证 0.12.7；构建器映像 `ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945` | 后端依赖安装、锁定及执行 |
| Node.js | 主版本 22；`code/frontend/.node-version`、`package.json` engines 和 `node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392` | Vite、Vue、TypeScript、Pinia、Vue Router、Tailwind |
| PostgreSQL | `postgres:18.6-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c` | 业务数据库与隔离的 LangGraph 检查点 schema |

摘要是多架构映像清单摘要；不得以可变标签取代摘要来宣称完全复现。映像可用 `docker buildx imagetools inspect` 检查。

## 协议与数据依赖

- 官方 `mcp` Python SDK 由 `uv.lock` 精确锁定。MCP Server 使用 SDK 的 `MCPServer.run(transport="streamable-http")`；客户端以服务 URL 建立 `Client`，以真实 HTTP 传输发现及调用健康探针。探针只回报服务本身状态，不模拟客户、合同、物料或订单工具。
- LangGraph 与 `langgraph-checkpoint-postgres` 由 `uv.lock` 一起锁定；检查点集成验证对 PostgreSQL 执行实际中断、关闭连接、重新连接和继续，未自行复制 checkpoint 表。
- PostgreSQL 正常连接由 SQLAlchemy 2 async engine 与 Psycopg 3 执行只读 `SELECT 1`。API、Worker、MCP 是独立进程。
- Python 项目由 `uv.lock` 固定直接与传递依赖；前端由 `package-lock.json` 固定 npm 依赖。README 和验证脚本使用锁定模式，未锁定时快速失败。

## PDF 与外部服务授权边界

- 初始后端 PDF 文字依赖使用 `pypdf`（BSD-3-Clause）。散布时须保留其版权、条件和免责声明；项目没有直接嵌入该套件源代码。
- PyMuPDF/MuPDF 采用 AGPL 或 Artifex 商业授权，商业产品或服务需确认相应条件。因此本基线不加入 PyMuPDF，也不把它带入后端容器。扫描 PDF 页面渲染尚未实现，将在 M4-02 按实际功能与授权条件选择依赖。
- 官方 MCP Python SDK 采用 MIT 授权。其他直接依赖由各自套件发布的授权文件约束；更新版本时须在合并前重新检查。产品发布时需随产品提供依赖声明。
- 模型与 OCR 供应商未选定也未配置。空白凭据明确显示“未配置”；设值后显示“已填设置，尚未验证”。本次未做在线模型或 OCR 验证。

## 已取得的兼容性证据

`code/tests/issues/test_issue_001.py` 验证 Compose 服务、API 至 PostgreSQL 就绪、MCP Streamable HTTP client/server 往返，以及 LangGraph 中断状态跨新 PostgreSQL 连接恢复。该入口只使用专用 Compose 项目数据库与临时 schema，测试结束移除所建 schema。

## 官方资料

- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 及其 [Streamable HTTP 客户端说明](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)。
- [uv 项目与锁定说明](https://docs.astral.sh/uv/guides/projects/)；锁定模式会确保命令使用 `uv.lock` 中的版本。
- [pypdf BSD-3-Clause 授权文件](https://github.com/py-pdf/pdf/blob/main/LICENSE)。
- [PyMuPDF 授权说明](https://pymupdf.io/licensing)。
