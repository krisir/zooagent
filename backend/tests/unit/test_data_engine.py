"""Model Engine + Data Engine behaviour."""

from __future__ import annotations

import pytest

from zooagent.data.query import AggregateSpec, Aggregation, Filter, QuerySpec, SortSpec
from zooagent.errors import ConflictError, NotFoundError, ValidationFailure
from zooagent.models.schema import FieldDef, ModelDef


def build_customer_model() -> ModelDef:
    return ModelDef(
        key="customer",
        name="客户",
        title_field="name",
        fields=[
            FieldDef(key="name", name="客户名称", type="text", required=True, unique=True),
            FieldDef(
                key="level",
                name="客户等级",
                type="enum",
                options=[
                    {"value": "normal", "label": "普通"},
                    {"value": "vip", "label": "VIP"},
                ],
                default="normal",
            ),
            FieldDef(key="balance", name="欠款", type="decimal", scale=2, default="0.00"),
            FieldDef(key="joined_at", name="加入日期", type="date"),
        ],
    )


def test_create_model_and_versions(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    assert model.id and model.id.startswith("mdl_")
    assert all(field.id for field in model.fields)
    assert model.version == 1

    loaded = ctx.models.get_model(ctx.conn, "customer")
    assert loaded.name == "客户"
    assert loaded.effective_title_field == "name"


def test_required_and_enum_validation(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())

    with pytest.raises(ValidationFailure) as missing:
        ctx.data.create_record(ctx.conn, model, {"level": "vip"})
    assert missing.value.details["field"] == "name"

    with pytest.raises(ValidationFailure):
        ctx.data.create_record(ctx.conn, model, {"name": "张三", "level": "gold"})

    record = ctx.data.create_record(ctx.conn, model, {"name": "张三", "balance": "12.345"})
    assert record["data"]["balance"] == "12.35"
    assert record["data"]["level"] == "normal"
    assert record["revision"] == 1


def test_unknown_field_rejected(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    with pytest.raises(ValidationFailure) as exc:
        ctx.data.create_record(ctx.conn, model, {"name": "张三", "nickname": "小张"})
    assert exc.value.details["unknown_fields"] == ["nickname"]


def test_unique_constraint(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    ctx.data.create_record(ctx.conn, model, {"name": "ACME"})
    # text unique keys are normalised (case-insensitive, trimmed)
    with pytest.raises(ConflictError):
        ctx.data.create_record(ctx.conn, model, {"name": " acme "})
    # a rejected write leaves nothing behind (per-operation savepoint)
    assert ctx.data.query_records(ctx.conn, model, QuerySpec())["total"] == 1
    # the same record may be updated in place
    updated = ctx.data.update_record(
        ctx.conn,
        model,
        ctx.data.query_records(ctx.conn, model, QuerySpec())["items"][0]["id"],
        {"name": "ACME"},
    )
    assert updated["data"]["name"] == "ACME"
    # uniqueness respects soft deletes
    record_id = updated["id"]
    ctx.data.delete_record(ctx.conn, model, record_id)
    ctx.data.create_record(ctx.conn, model, {"name": "acme"})
    assert ctx.data.query_records(ctx.conn, model, QuerySpec())["total"] == 1


def test_optimistic_locking(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    record = ctx.data.create_record(ctx.conn, model, {"name": "张三"})

    updated = ctx.data.update_record(
        ctx.conn, model, record["id"], {"level": "vip"}, expected_revision=1
    )
    assert updated["revision"] == 2
    assert updated["data"]["level"] == "vip"

    with pytest.raises(ConflictError) as exc:
        ctx.data.update_record(
            ctx.conn, model, record["id"], {"level": "normal"}, expected_revision=1
        )
    assert exc.value.details["current_revision"] == 2


def test_update_is_partial_and_validates_result(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    record = ctx.data.create_record(ctx.conn, model, {"name": "张三"})

    updated = ctx.data.update_record(ctx.conn, model, record["id"], {"balance": "100"})
    assert updated["data"]["name"] == "张三"
    assert updated["data"]["balance"] == "100.00"

    with pytest.raises(ValidationFailure):
        ctx.data.update_record(ctx.conn, model, record["id"], {"name": None})


def test_query_filters_sort_and_pagination(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    for index in range(5):
        ctx.data.create_record(
            ctx.conn,
            model,
            {
                "name": f"客户{index}",
                "level": "vip" if index % 2 == 0 else "normal",
                "balance": str(index * 10),
                "joined_at": f"2026-01-0{index + 1}",
            },
        )

    result = ctx.data.query_records(
        ctx.conn,
        model,
        QuerySpec(
            filters=[Filter(field="level", operator="eq", value="vip")],
            sort=[SortSpec(field="balance", direction="desc")],
            limit=2,
        ),
    )
    assert result["total"] == 3
    assert [item["data"]["name"] for item in result["items"]] == ["客户4", "客户2"]

    page_two = ctx.data.query_records(
        ctx.conn, model, QuerySpec(limit=2, offset=2, sort=[SortSpec(field="name")])
    )
    assert [item["data"]["name"] for item in page_two["items"]] == ["客户2", "客户3"]

    contains = ctx.data.query_records(
        ctx.conn,
        model,
        QuerySpec(filters=[Filter(field="name", operator="contains", value="客户3")]),
    )
    assert contains["total"] == 1


def test_aggregate_group_by(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    for index in range(4):
        ctx.data.create_record(
            ctx.conn,
            model,
            {
                "name": f"客户{index}",
                "level": "vip" if index < 2 else "normal",
                "balance": "100.00",
            },
        )

    result = ctx.data.aggregate(
        ctx.conn,
        model,
        AggregateSpec(
            group_by=["level"],
            aggregations=[
                Aggregation(function="count", alias="count"),
                Aggregation(function="sum", field="balance", alias="total"),
            ],
            sort=[SortSpec(field="total", direction="desc")],
        ),
    )
    assert result["items"] == [
        {"level": "vip", "count": 2, "total": 200.0},
        {"level": "normal", "count": 2, "total": 200.0},
    ]


def test_relations_are_validated_and_expanded(ctx) -> None:
    customer = ctx.models.create_model(ctx.conn, build_customer_model())
    order = ctx.models.create_model(
        ctx.conn,
        ModelDef(
            key="order",
            name="订单",
            fields=[
                FieldDef(key="code", name="编号", type="text", required=True),
                FieldDef(
                    key="customer",
                    name="客户",
                    type="relation",
                    target_model="customer",
                ),
            ],
        ),
    )
    buyer = ctx.data.create_record(ctx.conn, customer, {"name": "李四"})

    with pytest.raises(ValidationFailure):
        ctx.data.create_record(ctx.conn, order, {"code": "A1", "customer": "rec_missing"})

    created = ctx.data.create_record(ctx.conn, order, {"code": "A1", "customer": buyer["id"]})
    assert created["labels"]["customer"] == "李四"

    ctx.data.delete_record(ctx.conn, customer, buyer["id"])
    still_there = ctx.data.get_record(ctx.conn, order, created["id"])
    # the label survives a soft delete but is clearly marked
    assert still_there["labels"]["customer"] == "李四（已删除）"


def test_soft_delete_restore_and_history(ctx) -> None:
    model = ctx.models.create_model(ctx.conn, build_customer_model())
    record = ctx.data.create_record(ctx.conn, model, {"name": "王五"})
    ctx.data.update_record(ctx.conn, model, record["id"], {"level": "vip"})
    ctx.data.delete_record(ctx.conn, model, record["id"])

    with pytest.raises(NotFoundError):
        ctx.data.get_record(ctx.conn, model, record["id"])

    restored = ctx.data.restore_record(ctx.conn, model, record["id"])
    assert restored["deleted_at"] is None

    rows = ctx.conn.execute(
        "SELECT change_type FROM record_history WHERE record_id = ? ORDER BY revision",
        (record["id"],),
    ).fetchall()
    assert [row["change_type"] for row in rows] == ["created", "updated", "deleted", "restored"]


def test_model_version_keeps_data_and_field_ids(ctx) -> None:
    first = ctx.models.create_model(ctx.conn, build_customer_model())
    record = ctx.data.create_record(ctx.conn, first, {"name": "赵六"})
    name_field_id = first.field("name").id

    spec = build_customer_model()
    spec.fields.append(FieldDef(key="notes", name="备注", type="text"))
    second = ctx.models.update_model(ctx.conn, "customer", spec)

    assert second.version == 2
    assert second.field("name").id == name_field_id
    assert second.field("notes").id is not None

    reloaded = ctx.data.get_record(ctx.conn, second, record["id"])
    assert reloaded["data"]["name"] == "赵六"
