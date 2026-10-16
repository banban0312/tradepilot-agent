# 数据库迁移与演示主数据

本文说明 M1-02 建立的 PostgreSQL 业务基础表、Alembic 迁移和可重复演示主数据。表结构依据 `doc/SPEC.md` 第 5 章；这里只提供持久化结构，不代表登录、订单审批或下单功能已经实现。

## 表结构范围

- 身份基础：`departments`、`users`、`user_roles`、`user_department_scopes`。密码字段只保存哈希；此 issue 不创建默认用户，也不启用登录。
- 部门主数据：`customers`、`projects`，客户与项目编码在各自部门内唯一。
- 物料主数据：`materials`、`material_aliases`，允许同一规范别名映射到多个规格，后续匹配必须显示多个候选。
- 合同与余额：`contracts`、`contract_lines`、`contract_balances`、`contract_line_balances`。采销方向、状态、金额、日期范围、外键和数量/金额非负由 PostgreSQL 约束；合同、客户、项目和订单部门关系使用复合外键约束。
- 基础业务头：`drafts`、`orders`。任务、审批、版本明细和额度占用关系由其依赖 issue 通过后续迁移添加，不在本 issue 提前实现运行服务。

UUID、时间戳、`numeric(20,2)` 金额、`numeric(18,6)` 数量和单价与 Spec 一致。外键删除使用 `RESTRICT`；外键查询列有显式索引。Alembic 只负责业务表，LangGraph 检查点表仍由官方 checkpointer 管理。

## Alembic 操作

首次在 Compose 示例环境初始化并更新结构：

```powershell
docker compose --project-name tradepilot --env-file code/backend/.env --file code/compose.yaml up --build --detach --wait api
docker compose --project-name tradepilot --env-file code/backend/.env --file code/compose.yaml exec api uv run --frozen alembic upgrade head
```

数据库 URL 在容器内通过环境变量传入；不要把数据库密码写入迁移文件。运行迁移前应备份需要保留的数据库。`downgrade` 只用于明确可丢弃的开发数据库；初始版本的回退会删除本 issue 创建的表及其中数据。

后端依赖使用 `uv` 和 `uv.lock`。迁移文件由当前 SQLAlchemy 元数据通过 Alembic autogenerate 创建，并需与模型一并审查；修改模型时先生成迁移，再检查迁移是否完整，禁止在应用启动时静默建表。

## 可重复演示数据

```powershell
docker compose --project-name tradepilot --env-file code/backend/.env --file code/compose.yaml exec api uv run --frozen python -m app.seed
```

seed 使用固定 UUID、固定编码和固定日期。重复执行会更新同一批 `TP-DEMO-` 样本，不删除其他行，也不重置业务表。若用户数据已占用这些固定 UUID 或样本编码，seed 会因唯一约束冲突而失败，不会覆盖另一条记录。

示例包含：

- 华北和华南两个部门，各自客户、项目和合同，支持后续部门范围测试。
- 销售、采购、停用合同；华北销售合同金额 150000 元、余额占用为零，剩余额度为 150000 元。
- 同一“螺纹钢”别名对应 HRB400 12mm 与 16mm 两种物料，必须保留多候选；另有钢板物料和不同价格/数量配置。
- 固定的人民币口径、有效期及单位，测试不依赖当前日期或在线服务。

## 隔离验证

从仓库根目录运行：

```powershell
pwsh -File code/scripts/verify_issue_002.ps1
```

脚本使用随机 Compose 项目名和 PostgreSQL 卷，运行两次 `alembic upgrade head`、两次 seed，再检查主数据、剩余额度、别名歧义、金额精度、唯一键、检查约束、外键及跨部门引用拒绝。结束时只删除该随机项目及其数据卷，不接触其他 Compose 项目。容器构建需要 Docker Hub、GHCR 和 Python 套件索引；不需要生产凭据、模型或 OCR 服务。
