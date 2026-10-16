"""验证 M1-02 的迁移、约束与可重复演示主数据。"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from app.models import (
    Contract,
    ContractBalance,
    Customer,
    Department,
    Material,
    MaterialAlias,
    Order,
    User,
)
from app.seed import run_seed
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


def _engine() -> Engine:
    if os.environ.get("TRADEPILOT_TEST_MODE") != "1":
        raise RuntimeError("此集成测试只能在隔离 Compose 验证环境中运行")
    url = os.environ["DATABASE_URL"]
    parsed = urlsplit(url)
    if parsed.hostname != "db" or parsed.path != "/tradepilot":
        raise RuntimeError("测试拒绝连接非隔离的 TradePilot 验证数据库")
    return create_engine(url, pool_pre_ping=True)


@pytest.fixture(scope="module")
def database() -> Engine:
    engine = _engine()
    yield engine
    engine.dispose()


def _expect_integrity_error(engine: Engine, statement: object) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(statement)


def test_migration_created_expected_tables_and_precision(database: Engine) -> None:
    inspector = inspect(database)
    expected = {
        "alembic_version",
        "departments",
        "users",
        "user_roles",
        "user_department_scopes",
        "customers",
        "projects",
        "materials",
        "material_aliases",
        "contracts",
        "contract_lines",
        "contract_balances",
        "contract_line_balances",
        "drafts",
        "orders",
    }
    assert expected <= set(inspector.get_table_names())

    amount = next(
        column
        for column in inspector.get_columns("contracts")
        if column["name"] == "total_amount"
    )
    assert amount["type"].precision == 20
    assert amount["type"].scale == 2

    line_price = next(
        column
        for column in inspector.get_columns("contract_lines")
        if column["name"] == "unit_price"
    )
    assert line_price["type"].precision == 18
    assert line_price["type"].scale == 6

    with database.connect() as connection:
        applied = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert applied


def test_fixed_seed_is_repeatable_and_covers_required_business_cases(database: Engine) -> None:
    with database.connect() as connection:
        departments = connection.execute(
            select(Department.code).where(Department.code.like("TP-DEMO-%")).order_by(Department.code)
        ).scalars().all()
        assert departments == ["TP-DEMO-NORTH", "TP-DEMO-SOUTH"]

        contracts = connection.execute(
            select(Contract.type, Contract.status, Contract.total_amount)
            .where(Contract.number.like("TP-DEMO-%"))
            .order_by(Contract.number)
        ).all()
        assert len(contracts) == 4
        assert {row.type for row in contracts} == {"SALES", "PURCHASE"}
        assert {row.status for row in contracts} == {"ACTIVE", "INACTIVE"}

        remaining = connection.execute(
            select(Contract.total_amount - ContractBalance.occupied_amount)
            .join(ContractBalance, ContractBalance.contract_id == Contract.id)
            .where(Contract.number == "TP-DEMO-SALES-N1")
        ).scalar_one()
        assert remaining == Decimal("150000.00")

        candidates = connection.execute(
            select(Material.code, Material.specification)
            .join(MaterialAlias, MaterialAlias.material_id == Material.id)
            .where(MaterialAlias.normalized_alias == "螺纹钢")
            .order_by(Material.code)
        ).all()
        assert [(row.code, row.specification) for row in candidates] == [
            ("TP-DEMO-RB12", "HRB400 12mm"),
            ("TP-DEMO-RB16", "HRB400 16mm"),
        ]

        sample_customer_count = connection.scalar(
            select(text("count(*)"))
            .select_from(Customer.__table__)
            .where(Customer.code.like("TP-DEMO-%"))
        )
        assert sample_customer_count == 2


def test_database_rejects_duplicate_codes_bad_amounts_types_and_cross_department_links(
    database: Engine,
) -> None:
    duplicate_department = Department.__table__.insert().values(
        id=uuid4(), code="TP-DEMO-NORTH", name="重复部门"
    )
    _expect_integrity_error(database, duplicate_department)

    invalid_contract_values = {
        "id": uuid4(),
        "number": f"TP-TEST-NEGATIVE-{uuid4().hex}",
        "department_id": UUID("00000000-0000-4000-8000-000000000001"),
        "type": "SALES",
        "customer_id": UUID("00000000-0000-4000-8000-000000000010"),
        "project_id": UUID("00000000-0000-4000-8000-000000000020"),
        "currency": "CNY",
        "total_amount": Decimal("-0.01"),
        "status": "ACTIVE",
        "valid_from": date(2025, 1, 1),
        "valid_to": date(2030, 12, 31),
        "version": 1,
    }
    _expect_integrity_error(database, Contract.__table__.insert().values(**invalid_contract_values))

    wrong_type = {**invalid_contract_values, "id": uuid4(), "total_amount": Decimal("1.00"), "type": "OTHER"}
    _expect_integrity_error(database, Contract.__table__.insert().values(**wrong_type))

    cross_department = {
        **invalid_contract_values,
        "id": uuid4(),
        "number": f"TP-TEST-CROSS-DEPT-{uuid4().hex}",
        "department_id": UUID("00000000-0000-4000-8000-000000000002"),
        "total_amount": Decimal("1.00"),
    }
    _expect_integrity_error(database, Contract.__table__.insert().values(**cross_department))

    missing_department_user = User.__table__.insert().values(
        id=uuid4(),
        login=f"TP-TEST-MISSING-DEPT-{uuid4().hex}",
        password_hash="not-a-credential",
        department_id=uuid4(),
    )
    _expect_integrity_error(database, missing_department_user)

    wrong_contract_department_order = Order.__table__.insert().values(
        id=uuid4(),
        number=f"TP-TEST-ORDER-{uuid4().hex}",
        department_id=UUID("00000000-0000-4000-8000-000000000002"),
        contract_id=UUID("00000000-0000-4000-8000-000000000050"),
        type="SALES",
        currency="CNY",
        total_amount=Decimal("1.00"),
    )
    _expect_integrity_error(database, wrong_contract_department_order)


def test_seed_refuses_to_overwrite_non_demo_data_using_a_reserved_id(database: Engine) -> None:
    reserved_sample_id = UUID("00000000-0000-4000-8000-000000000001")
    with database.begin() as connection:
        connection.execute(
            Department.__table__.update()
            .where(Department.id == reserved_sample_id)
            .values(code="CUSTOM-USER-DATA", name="保留用户数据")
        )

    with pytest.raises(RuntimeError, match="非演示数据"):
        run_seed()

    with database.connect() as connection:
        row = connection.execute(
            select(Department.code, Department.name).where(Department.id == reserved_sample_id)
        ).one()
    assert (row.code, row.name) == ("CUSTOM-USER-DATA", "保留用户数据")
