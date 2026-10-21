"""Action Engine: deterministic orchestration, transactions and expressions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from zooagent.actions.expression import ExpressionError, evaluate, resolve_path
from zooagent.actions.schema import ActionDef
from zooagent.data.query import QuerySpec
from zooagent.errors import ActionExecutionError, ValidationFailure
from zooagent.models.schema import FieldDef, ModelDef


def _account_model() -> ModelDef:
    return ModelDef(
        key="account",
        name="账户",
        title_field="name",
        fields=[
            FieldDef(key="name", name="账户", type="text", required=True),
            FieldDef(key="balance", name="余额", type="decimal", scale=2, default="0.00"),
        ],
    )


def _transaction_model() -> ModelDef:
    return ModelDef(
        key="transaction",
        name="交易",
        title_field="note",
        fields=[
            FieldDef(key="amount", name="金额", type="decimal", scale=2, required=True),
            FieldDef(key="note", name="备注", type="text"),
            FieldDef(
                key="account",
                name="账户",
                type="relation",
                target_model="account",
            ),
        ],
    )


def _spend_action() -> ActionDef:
    return ActionDef.model_validate(
        {
            "key": "spend",
            "name": "支出",
            "inputs": [
                {"key": "account", "name": "账户", "type": "relation", "target_model": "account", "required": True},
                {"key": "amount", "name": "金额", "type": "decimal", "scale": 2, "required": True},
                {"key": "note", "name": "备注", "type": "text"},
            ],
            "steps": [
                {
                    "id": "load_account",
                    "type": "query",
                    "model": "account",
                    "output": "account",
                    "filters": [
                        {"field": "id", "operator": "eq", "value": {"ref": "input.account"}}
                    ],
                },
                {
                    "id": "new_balance",
                    "type": "compute",
                    "output": "balance",
                    "expression": "$steps.account.items[0].data.balance - $input.amount",
                },
                {
                    "id": "update_account",
                    "type": "update",
                    "model": "account",
                    "record_id": {"ref": "input.account"},
                    "values": {"balance": {"ref": "steps.balance"}},
                    "output": "account_after",
                },
                {
                    "id": "create_transaction",
                    "type": "create",
                    "model": "transaction",
                    "output": "transaction",
                    "values": {
                        "amount": {"ref": "input.amount"},
                        "note": {"ref": "input.note"},
                        "account": {"ref": "input.account"},
                    },
                },
            ],
        }
    )


def test_expression_language() -> None:
    context = {"input": {"amount": "10.00", "name": "午餐"}}
    assert evaluate("$input.amount * 2", context) == Decimal("20.00")
    assert evaluate("$input.amount > 5", context) is True
    assert evaluate("str($input.amount) + ' 元'", context) == "10.00 元"

    with pytest.raises(ExpressionError):
        evaluate("__import__('os').system('ls')", context)
    with pytest.raises(ExpressionError):
        evaluate("$input.unknown + 1", context)


def test_action_updates_and_creates(ctx) -> None:
    account_model = ctx.models.create_model(ctx.conn, _account_model())
    ctx.models.create_model(ctx.conn, _transaction_model())
    account = ctx.data.create_record(ctx.conn, account_model, {"name": "现金", "balance": "100"})

    action = ctx.actions.create_action(ctx.conn, _spend_action())
    result = ctx.actions.run(
        ctx.conn,
        action,
        {"account": account["id"], "amount": "38", "note": "午餐"},
        actor="test",
        app_name="测试",
    )

    assert result["output"]["data"]["amount"] == "38.00"
    assert [effect["type"] for effect in result["effects"]] == ["updated", "created"]

    reloaded = ctx.data.get_record(ctx.conn, account_model, account["id"])
    assert reloaded["data"]["balance"] == "62.00"
    assert reloaded["revision"] == 2


def test_action_rolls_back_on_failure(ctx) -> None:
    ctx.models.create_model(ctx.conn, _account_model())
    transaction_model = ctx.models.create_model(ctx.conn, _transaction_model())

    broken = ActionDef.model_validate(
        {
            "key": "broken",
            "name": "会失败的操作",
            "inputs": [{"key": "amount", "name": "金额", "type": "decimal", "required": True}],
            "steps": [
                {
                    "id": "create_transaction",
                    "type": "create",
                    "model": "transaction",
                    "output": "transaction",
                    "values": {
                        "amount": {"ref": "input.amount"},
                        "account": "rec_does_not_exist",
                    },
                }
            ],
        }
    )
    action = ctx.actions.create_action(ctx.conn, broken)

    with pytest.raises(ActionExecutionError) as excinfo:
        with ctx.conn:
            ctx.actions.run(ctx.conn, action, {"amount": "10"}, actor="test")
    assert excinfo.value.details["rolled_back"] is True

    result = ctx.data.query_records(ctx.conn, transaction_model, QuerySpec())
    assert result["total"] == 0


def test_action_rolls_back_all_steps_when_a_later_step_fails(ctx) -> None:
    """A step failure must undo writes from earlier steps of the same Action.

    Step-level savepoints alone made this pass only when the *first* step was
    the one that failed; a `return` step referencing a missing path used to
    leave the created record behind.
    """
    ctx.models.create_model(ctx.conn, _account_model())
    transaction_model = ctx.models.create_model(ctx.conn, _transaction_model())

    action = ctx.actions.create_action(
        ctx.conn,
        ActionDef.model_validate(
            {
                "key": "log_then_break",
                "name": "写入后失败",
                "inputs": [{"key": "amount", "name": "金额", "type": "decimal", "required": True}],
                "steps": [
                    {
                        "id": "create_transaction",
                        "type": "create",
                        "model": "transaction",
                        "output": "record",
                        "values": {"amount": {"ref": "input.amount"}},
                    },
                    {
                        # valid definition, but the relation target does not exist
                        "id": "second_write",
                        "type": "create",
                        "model": "transaction",
                        "output": "second",
                        "values": {"amount": "1", "account": "rec_does_not_exist"},
                    },
                ],
            }
        ),
    )

    with pytest.raises(ActionExecutionError):
        ctx.actions.run(ctx.conn, action, {"amount": "10"}, actor="test")

    result = ctx.data.query_records(ctx.conn, transaction_model, QuerySpec())
    assert result["total"] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM record_history").fetchone()[0] == 0


def test_return_step_accepts_bare_step_output_name(ctx) -> None:
    """`{"ref": "record"}` must resolve to the `output: record` step output."""
    ctx.models.create_model(ctx.conn, _account_model())
    ctx.models.create_model(ctx.conn, _transaction_model())
    action = ctx.actions.create_action(
        ctx.conn,
        ActionDef.model_validate(
            {
                "key": "log_only",
                "name": "只记录",
                "inputs": [{"key": "amount", "name": "金额", "type": "decimal", "required": True}],
                "steps": [
                    {
                        "id": "create_transaction",
                        "type": "create",
                        "model": "transaction",
                        "output": "record",
                        "values": {"amount": {"ref": "input.amount"}},
                    },
                    {"id": "done", "type": "return", "value": {"ref": "record"}},
                ],
            }
        ),
    )

    result = ctx.actions.run(ctx.conn, action, {"amount": "10"}, actor="test")
    assert result["output"]["data"]["amount"] == "10.00"
    # the canonical `steps.<output>` form keeps working too
    assert resolve_path("steps.record", {"steps": {"record": {"id": 1}}}) == {"id": 1}


def test_unresolvable_ref_is_rejected_at_define_time(ctx) -> None:
    """The bug the user hit: a stored Action whose return ref cannot resolve."""
    ctx.models.create_model(ctx.conn, _account_model())
    ctx.models.create_model(ctx.conn, _transaction_model())
    spec = ActionDef.model_validate(
        {
            "key": "broken_ref",
            "name": "引用不存在的输出",
            "inputs": [{"key": "amount", "name": "金额", "type": "decimal", "required": True}],
            "steps": [
                {
                    "id": "create_transaction",
                    "type": "create",
                    "model": "transaction",
                    "output": "record",
                    "values": {"amount": {"ref": "input.amount"}},
                },
                {"id": "done", "type": "return", "value": {"ref": "missing_output"}},
            ],
        }
    )
    with pytest.raises(ValidationFailure) as excinfo:
        ctx.actions.create_action(ctx.conn, spec)
    assert any("missing_output" in problem for problem in excinfo.value.details)
    assert ctx.actions.list_actions(ctx.conn) == []


def test_unsupported_expression_is_rejected_at_define_time(ctx) -> None:
    ctx.models.create_model(ctx.conn, _account_model())
    ctx.models.create_model(ctx.conn, _transaction_model())
    spec = ActionDef.model_validate(
        {
            "key": "bad_expression",
            "name": "表达式不支持",
            "inputs": [{"key": "amount", "name": "金额", "type": "decimal", "required": True}],
            "steps": [
                {
                    "id": "recent",
                    "type": "query",
                    "model": "transaction",
                    "output": "recent",
                },
                {
                    "id": "avg",
                    "type": "compute",
                    "output": "avg",
                    "expression": "size($recent.items) < 2 ? null : date_diff($recent, 1)",
                },
                {"id": "done", "type": "return", "value": {"ref": "avg"}},
            ],
        }
    )
    with pytest.raises(ValidationFailure) as excinfo:
        ctx.actions.create_action(ctx.conn, spec)
    assert any("表达式语法错误" in problem for problem in excinfo.value.details)


def test_action_validation_errors(ctx) -> None:
    action_model = ctx.models.create_model(ctx.conn, _account_model())
    ctx.models.create_model(ctx.conn, _transaction_model())
    action = ctx.actions.create_action(ctx.conn, _spend_action())
    account = ctx.data.create_record(ctx.conn, action_model, {"name": "现金", "balance": "10"})

    with pytest.raises(ActionExecutionError):
        ctx.actions.run(ctx.conn, action, {"account": account["id"]})

    with pytest.raises(ActionExecutionError):
        ctx.actions.run(
            ctx.conn, action, {"account": account["id"], "amount": "10", "unknown": 1}
        )
