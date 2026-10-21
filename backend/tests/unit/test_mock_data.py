"""Mock data synthesis: quality, constraints and reproducibility."""

from __future__ import annotations

import datetime as dt

import pytest

from zooagent.agents.mock_data import (
    REFERENCE_MODEL_CAP,
    dependency_order,
    generate_application_data,
    generate_records,
)
from zooagent.data.query import AggregateSpec, Aggregation, QuerySpec
from zooagent.errors import ValidationFailure
from zooagent.models.schema import FieldDef, ModelDef


def finance_models() -> list[ModelDef]:
    category = ModelDef(
        key="category",
        name="分类",
        title_field="name",
        fields=[
            FieldDef(key="name", name="分类名称", type="text", required=True, unique=True),
            FieldDef(
                key="kind",
                name="收支类型",
                type="enum",
                options=[
                    {"value": "expense", "label": "支出"},
                    {"value": "income", "label": "收入"},
                ],
                default="expense",
            ),
        ],
    )
    transaction = ModelDef(
        key="transaction",
        name="交易",
        title_field="note",
        fields=[
            FieldDef(key="date", name="日期", type="date", required=True),
            FieldDef(key="amount", name="金额", type="decimal", scale=2, required=True),
            FieldDef(
                key="direction",
                name="方向",
                type="enum",
                required=True,
                options=[
                    {"value": "expense", "label": "支出"},
                    {"value": "income", "label": "收入"},
                ],
            ),
            FieldDef(key="category", name="分类", type="relation", target_model="category"),
            FieldDef(key="note", name="备注", type="text"),
            FieldDef(key="confirmed", name="已确认", type="boolean"),
        ],
    )
    return [category, transaction]


@pytest.fixture()
def finance_ctx(ctx):
    for model in finance_models():
        ctx.models.create_model(ctx.conn, model)
    return ctx


def test_generate_records_respects_constraints(finance_ctx) -> None:
    category = finance_ctx.models.get_model(finance_ctx.conn, "category")
    report = generate_records(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, category, 12, seed=1
    )
    assert report["created"] == 12
    assert report["failed"] == 0

    # unique text field must not collide even though it comes from a small pool
    names = [
        item["data"]["name"]
        for item in finance_ctx.data.query_records(
            finance_ctx.conn, category, QuerySpec(limit=50)
        )["items"]
    ]
    assert len(set(names)) == 12


def test_generated_data_looks_real_and_is_reproducible(finance_ctx) -> None:
    category = finance_ctx.models.get_model(finance_ctx.conn, "category")
    generate_records(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, category, 6, seed=5
    )
    transaction = finance_ctx.models.get_model(finance_ctx.conn, "transaction")
    generate_records(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, transaction, 40, seed=42
    )

    items = finance_ctx.data.query_records(
        finance_ctx.conn, transaction, QuerySpec(limit=50)
    )["items"]
    assert len(items) == 40

    for item in items:
        amount = item["data"]["amount"]
        assert isinstance(amount, str) and amount.count(".") == 1
        assert item["data"]["direction"] in {"expense", "income"}
        assert item["data"]["date"] <= dt.date.today().isoformat()
        assert isinstance(item["data"]["confirmed"], bool)
        # relations resolve to real categories
        assert item["labels"].get("category")

    # income rows are salary-sized, expense rows are daily-sized
    incomes = [float(i["data"]["amount"]) for i in items if i["data"]["direction"] == "income"]
    expenses = [float(i["data"]["amount"]) for i in items if i["data"]["direction"] == "expense"]
    if incomes:
        assert min(incomes) >= 3000
    if expenses:
        assert max(expenses) <= 800

    # same seed -> same dataset
    before = sorted(i["data"]["amount"] for i in items)
    generate_records(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, transaction, 40, seed=42
    )
    all_items = finance_ctx.data.query_records(
        finance_ctx.conn, transaction, QuerySpec(limit=200)
    )["items"]
    assert len(all_items) == 80
    # both batches are identical, so the sorted union is the batch doubled
    assert sorted(i["data"]["amount"] for i in all_items) == sorted(before + before)


def test_relation_requires_existing_target_records(finance_ctx) -> None:
    """A required relation with no candidates must fail loudly, not silently."""
    strict = ModelDef(
        key="strict",
        name="严格",
        fields=[
            FieldDef(key="title", name="标题", type="text", required=True),
            FieldDef(
                key="category",
                name="分类",
                type="relation",
                target_model="category",
                required=True,
            ),
        ],
    )
    finance_ctx.models.create_model(finance_ctx.conn, strict)
    stored = finance_ctx.models.get_model(finance_ctx.conn, "strict")
    report = generate_records(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, stored, 3, seed=1
    )
    assert report["created"] == 0
    assert report["failed"] == 3
    assert "没有可用值" in report["errors"][0]["error"]

    # once the target model has rows, generation succeeds
    generate_records(
        finance_ctx.conn,
        finance_ctx.models,
        finance_ctx.data,
        finance_ctx.models.get_model(finance_ctx.conn, "category"),
        4,
        seed=1,
    )
    assert (
        generate_records(
            finance_ctx.conn, finance_ctx.models, finance_ctx.data, stored, 3, seed=1
        )["created"]
        == 3
    )


def test_generate_requires_a_stored_model(finance_ctx) -> None:
    """An unsaved model spec must be rejected with a clear message."""
    spec = ModelDef(
        key="unsaved",
        name="未保存",
        fields=[FieldDef(key="title", name="标题", type="text", required=True)],
    )
    with pytest.raises(ValidationFailure) as exc:
        generate_records(finance_ctx.conn, finance_ctx.models, finance_ctx.data, spec, 3)
    assert "还没有保存" in exc.value.message


def test_generate_application_data_orders_dependencies(finance_ctx) -> None:
    report = generate_application_data(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, count=50, seed=7
    )
    assert report["created"] >= 50
    assert report["failed"] == 0

    transaction = finance_ctx.models.get_model(finance_ctx.conn, "transaction")
    categories = finance_ctx.models.get_model(finance_ctx.conn, "category")
    assert (
        finance_ctx.data.query_records(finance_ctx.conn, transaction, QuerySpec(limit=1))[
            "total"
        ]
        == 50
    )
    assert (
        finance_ctx.data.query_records(finance_ctx.conn, categories, QuerySpec(limit=1))[
            "total"
        ]
        <= REFERENCE_MODEL_CAP
    )

    # grouping by month gives a readable trend, which is the point of the data
    trend = finance_ctx.data.aggregate(
        finance_ctx.conn,
        transaction,
        AggregateSpec(
            group_by=["date"],
            granularity="month",
            aggregations=[Aggregation(function="sum", field="amount", alias="total")],
        ),
    )
    keys = [item["date"] for item in trend["items"]]
    assert keys == sorted(keys)
    assert all(len(str(key)) == 7 for key in keys)  # YYYY-MM


def test_granularity_rejects_non_date_fields(finance_ctx) -> None:
    category = finance_ctx.models.get_model(finance_ctx.conn, "category")
    with pytest.raises(ValidationFailure):
        finance_ctx.data.aggregate(
            finance_ctx.conn,
            category,
            AggregateSpec(group_by=["name"], granularity="month"),
        )


def test_dimension_models_are_not_duplicated(finance_ctx) -> None:
    """Seeded categories/accounts must be reused, not regenerated with suffixes."""
    category = finance_ctx.models.get_model(finance_ctx.conn, "category")
    for name in ("餐饮", "交通", "购物"):
        finance_ctx.data.create_record(finance_ctx.conn, category, {"name": name})

    report = generate_application_data(
        finance_ctx.conn, finance_ctx.models, finance_ctx.data, count=20, seed=3
    )
    names = [
        item["data"]["name"]
        for item in finance_ctx.data.query_records(
            finance_ctx.conn, category, QuerySpec(limit=50)
        )["items"]
    ]
    assert sorted(names) == ["交通", "购物", "餐饮"]
    assert report["created"] == 20  # only the fact model got rows
    skipped = [item for item in report["models"] if item.get("skipped")]
    assert any(item["model"] == "category" for item in skipped)


def test_count_limits() -> None:
    assert dependency_order([]) == []
    models = finance_models()
    ordered = dependency_order(models)
    assert [model.key for model in ordered] == ["category", "transaction"]


def test_generate_records_rejects_silly_counts(finance_ctx) -> None:
    model = finance_ctx.models.get_model(finance_ctx.conn, "category")
    with pytest.raises(ValidationFailure):
        generate_records(
            finance_ctx.conn, finance_ctx.models, finance_ctx.data, model, 0
        )
    with pytest.raises(ValidationFailure):
        generate_records(
            finance_ctx.conn, finance_ctx.models, finance_ctx.data, model, 99_999
        )
