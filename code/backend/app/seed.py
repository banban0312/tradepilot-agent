"""写入固定 UUID 的演示主数据；只 upsert 本文件列出的样本，不清空其他数据。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.models import (
    Contract,
    ContractBalance,
    ContractLine,
    ContractLineBalance,
    Customer,
    Department,
    Material,
    MaterialAlias,
    Project,
)

SEED_TIME = datetime(2025, 1, 1, tzinfo=UTC)


def _id(suffix: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{suffix:012d}")


def seed_rows() -> list[tuple[object, list[dict[str, object]]]]:
    """保持样本 ID 和业务值固定，便于不同环境复现同一组主数据。"""
    return [
        (
            Department.__table__,
            [
                {"id": _id(1), "code": "TP-DEMO-NORTH", "name": "华北演示部门", "active": True},
                {"id": _id(2), "code": "TP-DEMO-SOUTH", "name": "华南演示部门", "active": True},
            ],
        ),
        (
            Customer.__table__,
            [
                {
                    "id": _id(10), "code": "TP-DEMO-CUST-N1", "name": "华北示例客户",
                    "department_id": _id(1), "contact": "张先生", "address": "北京市朝阳区示例路 1 号",
                    "active": True,
                },
                {
                    "id": _id(11), "code": "TP-DEMO-CUST-S1", "name": "华南示例客户",
                    "department_id": _id(2), "contact": "李女士", "address": "深圳市南山区示例路 2 号",
                    "active": True,
                },
            ],
        ),
        (
            Project.__table__,
            [
                {
                    "id": _id(20), "code": "TP-DEMO-PROJ-N1", "name": "华北示例工程",
                    "department_id": _id(1), "delivery_address": "北京市朝阳区示例工地",
                },
                {
                    "id": _id(21), "code": "TP-DEMO-PROJ-S1", "name": "华南示例工程",
                    "department_id": _id(2), "delivery_address": "深圳市南山区示例工地",
                },
            ],
        ),
        (
            Material.__table__,
            [
                {
                    "id": _id(30), "code": "TP-DEMO-RB12", "name": "螺纹钢",
                    "specification": "HRB400 12mm", "unit": "吨", "active": True,
                },
                {
                    "id": _id(31), "code": "TP-DEMO-RB16", "name": "螺纹钢",
                    "specification": "HRB400 16mm", "unit": "吨", "active": True,
                },
                {
                    "id": _id(32), "code": "TP-DEMO-PLATE08", "name": "热轧钢板",
                    "specification": "Q235 8mm", "unit": "吨", "active": True,
                },
            ],
        ),
        (
            MaterialAlias.__table__,
            [
                {"id": _id(40), "material_id": _id(30), "alias": "螺纹钢", "normalized_alias": "螺纹钢"},
                {"id": _id(41), "material_id": _id(31), "alias": "螺纹钢", "normalized_alias": "螺纹钢"},
                {"id": _id(42), "material_id": _id(30), "alias": "HRB400 12", "normalized_alias": "hrb40012"},
                {"id": _id(43), "material_id": _id(32), "alias": "钢板", "normalized_alias": "钢板"},
            ],
        ),
        (
            Contract.__table__,
            [
                {
                    "id": _id(50), "number": "TP-DEMO-SALES-N1", "department_id": _id(1),
                    "type": "SALES", "customer_id": _id(10), "project_id": _id(20),
                    "currency": "CNY", "total_amount": Decimal("150000.00"), "status": "ACTIVE",
                    "valid_from": date(2025, 1, 1), "valid_to": date(2030, 12, 31), "version": 1,
                },
                {
                    "id": _id(51), "number": "TP-DEMO-PURCHASE-N1", "department_id": _id(1),
                    "type": "PURCHASE", "customer_id": _id(10), "project_id": _id(20),
                    "currency": "CNY", "total_amount": Decimal("800000.00"), "status": "ACTIVE",
                    "valid_from": date(2025, 1, 1), "valid_to": date(2030, 12, 31), "version": 1,
                },
                {
                    "id": _id(52), "number": "TP-DEMO-INACTIVE-N1", "department_id": _id(1),
                    "type": "SALES", "customer_id": _id(10), "project_id": _id(20),
                    "currency": "CNY", "total_amount": Decimal("500000.00"), "status": "INACTIVE",
                    "valid_from": date(2025, 1, 1), "valid_to": date(2030, 12, 31), "version": 1,
                },
                {
                    "id": _id(53), "number": "TP-DEMO-SALES-S1", "department_id": _id(2),
                    "type": "SALES", "customer_id": _id(11), "project_id": _id(21),
                    "currency": "CNY", "total_amount": Decimal("300000.00"), "status": "ACTIVE",
                    "valid_from": date(2025, 1, 1), "valid_to": date(2030, 12, 31), "version": 1,
                },
            ],
        ),
        (
            ContractLine.__table__,
            [
                {
                    "id": _id(60), "contract_id": _id(50), "material_id": _id(30), "unit": "吨",
                    "unit_price": Decimal("3500.000000"), "max_quantity": Decimal("100.000000"),
                },
                {
                    "id": _id(61), "contract_id": _id(51), "material_id": _id(31), "unit": "吨",
                    "unit_price": Decimal("3550.000000"), "max_quantity": None,
                },
                {
                    "id": _id(62), "contract_id": _id(53), "material_id": _id(32), "unit": "吨",
                    "unit_price": None, "max_quantity": None,
                },
            ],
        ),
        (
            ContractBalance.__table__,
            [
                {"contract_id": _id(50), "occupied_amount": Decimal("0.00"), "occupied_order_count": 0, "version": 0},
                {"contract_id": _id(51), "occupied_amount": Decimal("0.00"), "occupied_order_count": 0, "version": 0},
                {"contract_id": _id(52), "occupied_amount": Decimal("0.00"), "occupied_order_count": 0, "version": 0},
                {"contract_id": _id(53), "occupied_amount": Decimal("0.00"), "occupied_order_count": 0, "version": 0},
            ],
        ),
        (
            ContractLineBalance.__table__,
            [
                {"contract_line_id": _id(60), "occupied_quantity": Decimal("0.000000")},
                {"contract_line_id": _id(61), "occupied_quantity": Decimal("0.000000")},
                {"contract_line_id": _id(62), "occupied_quantity": Decimal("0.000000")},
            ],
        ),
    ]


def run_seed() -> None:
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            for table, rows in seed_rows():
                if "created_at" in table.c:
                    for row in rows:
                        row["created_at"] = SEED_TIME
                        row["updated_at"] = SEED_TIME
                statement = insert(table).values(rows)
                primary_keys = [column.name for column in table.primary_key]
                updates = {
                    column.name: getattr(statement.excluded, column.name)
                    for column in table.columns
                    if column.name not in primary_keys and column.name not in {"created_at"}
                }
                if "code" in table.c:
                    sample_scope = table.c.code.like("TP-DEMO-%")
                elif "number" in table.c:
                    sample_scope = table.c.number.like("TP-DEMO-%")
                elif table.name == "material_aliases":
                    sample_scope = table.c.material_id.in_(
                        select(Material.id).where(Material.code.like("TP-DEMO-%"))
                    )
                elif table.name == "contract_lines" or table.name == "contract_balances":
                    sample_scope = table.c.contract_id.in_(
                        select(Contract.id).where(Contract.number.like("TP-DEMO-%"))
                    )
                elif table.name == "contract_line_balances":
                    sample_scope = table.c.contract_line_id.in_(
                        select(ContractLine.id)
                        .join(Contract, Contract.id == ContractLine.contract_id)
                        .where(Contract.number.like("TP-DEMO-%"))
                    )
                else:
                    raise RuntimeError(f"未定义演示数据隔离条件：{table.name}")

                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=primary_keys,
                        set_=updates,
                        where=sample_scope,
                    )
                )
                seeded_ids = [row[primary_keys[0]] for row in rows]
                persisted_count = connection.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c[primary_keys[0]].in_(seeded_ids), sample_scope)
                )
                if persisted_count != len(rows):
                    raise RuntimeError(f"{table.name} 存在与固定演示 ID 冲突的非演示数据，已回滚本次 seed")
    finally:
        engine.dispose()


if __name__ == "__main__":
    run_seed()
    print("演示主数据已写入；重复运行会更新同一组固定样本，不删除其他数据。")
