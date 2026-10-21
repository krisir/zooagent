"""When an Action fails, the platform diagnoses it instead of only reporting.

Covers the three layers of that feature:
* the engine snapshots the failure (definition, step, trace, rolled-back writes);
* the deterministic inspection finds the mechanical causes - including the
  forward reference that define-time validation lets through;
* the diagnoser records a report in the primary conversation and relays a short
  version to the application conversation.
"""

from __future__ import annotations

import asyncio

import pytest

from zooagent.actions.diagnostics import inspect_failure
from zooagent.actions.schema import ActionDef
from zooagent.agents.tools import build_application_tools
from zooagent.data.query import QuerySpec
from zooagent.errors import ActionExecutionError
from zooagent.models.schema import FieldDef, ModelDef


def _transaction_model() -> ModelDef:
    return ModelDef(
        key="transaction",
        name="交易",
        title_field="note",
        fields=[
            FieldDef(key="amount", name="金额", type="decimal", scale=2, required=True),
            FieldDef(key="note", name="备注", type="text"),
            FieldDef(
                key="category",
                name="分类",
                type="enum",
                options=[
                    {"value": "food", "label": "餐饮"},
                    {"value": "travel", "label": "交通"},
                ],
            ),
        ],
    )


def _typo_action() -> ActionDef:
    return ActionDef.model_validate(
        {
            "key": "log_expense",
            "name": "记一笔支出",
            "inputs": [
                {"key": "amount", "name": "金额", "type": "decimal", "required": True},
                {"key": "note", "name": "备注", "type": "text"},
            ],
            "steps": [
                {
                    "id": "create_transaction",
                    "type": "create",
                    "model": "transaction",
                    "output": "transaction",
                    "values": {
                        # typo: the model declares `category`
                        "categry": "food",
                        "amount": {"ref": "input.amount"},
                        "note": {"ref": "input.note"},
                    },
                }
            ],
        }
    )


def _setup(state):
    application = state.service.create_application("个人记账")
    with state.service.open_app(application.id) as ctx:
        ctx.models.create_model(ctx.conn, _transaction_model())
        action = ctx.actions.create_action(ctx.conn, _typo_action())
    return application, action


@pytest.fixture()
def application(state):
    application, _ = _setup(state)
    return application


def _failure(state, application, action, *, inputs=None) -> dict:
    with state.service.open_app(application.id) as ctx:
        with pytest.raises(ActionExecutionError) as excinfo:
            with ctx.conn:
                ctx.actions.run(
                    ctx.conn,
                    action,
                    inputs if inputs is not None else {"amount": "38", "note": "午餐"},
                    actor="test",
                    app_name=application.name,
                    application_id=application.id,
                )
    return excinfo.value.details


def test_failure_snapshot_is_complete(state, application) -> None:
    with state.service.open_app(application.id) as ctx:
        action = ctx.actions.get_action(ctx.conn, "log_expense")
    failure = _failure(state, application, action)

    assert failure["application"] == {
        "id": application.id,
        "name": "个人记账",
    }
    assert failure["action"]["key"] == "log_expense"
    assert failure["step"] == {
        "id": "create_transaction",
        "type": "create",
        "index": 0,
    }
    assert failure["inputs"]["amount"] == "38"
    assert failure["error"]["code"] == "validation_error"
    assert failure["error"]["message"]
    # the failed step is marked in the trace, and nothing was written
    assert failure["trace"][-1]["status"] == "error"
    assert failure["rolled_back"] is True

    with state.service.open_app(application.id) as ctx:
        model = ctx.models.get_model(ctx.conn, "transaction")
        assert ctx.data.query_records(ctx.conn, model, QuerySpec())["total"] == 0


def test_inspection_finds_the_typo_and_proposes_a_patch(state, application) -> None:
    with state.service.open_app(application.id) as ctx:
        action = ctx.actions.get_action(ctx.conn, "log_expense")
        models = ctx.models.list_models(ctx.conn)
    failure = _failure(state, application, action)

    inspection = inspect_failure(failure, models)

    assert inspection.category == "unknown_field"
    assert inspection.is_definition_problem
    assert any("categry" in item.title for item in inspection.findings)
    assert any("category" in suggestion for suggestion in inspection.suggestions)
    assert inspection.proposal is not None
    # the proposal renames exactly the mistyped key, keeping everything else
    values = inspection.proposal["steps"][0]["values"]
    assert values["category"] == "food"
    assert "categry" not in values
    ActionDef.model_validate(inspection.proposal)


def test_inspection_detects_a_forward_reference(state, application) -> None:
    """Define-time validation accepts any declared output; runtime order does not."""
    with state.service.open_app(application.id) as ctx:
        action = ctx.actions.create_action(
            ctx.conn,
            ActionDef.model_validate(
                {
                    "key": "borrow_from_the_future",
                    "name": "引用后续步骤",
                    "steps": [
                        {
                            "id": "first",
                            "type": "create",
                            "model": "transaction",
                            "output": "first",
                            "values": {
                                "amount": {"ref": "steps.second.amount"},
                            },
                        },
                        {
                            "id": "second",
                            "type": "create",
                            "model": "transaction",
                            "output": "second",
                            "values": {"amount": "1"},
                        },
                    ],
                }
            ),
        )
        models = ctx.models.list_models(ctx.conn)

    failure = _failure(state, application, action, inputs={})
    inspection = inspect_failure(failure, models)

    assert inspection.category == "forward_reference"
    assert any("steps.second" in item.title for item in inspection.findings)
    # a forward reference cannot be repaired mechanically
    assert inspection.proposal is None


def test_input_validation_failure_is_categorised_as_input(state, application) -> None:
    with state.service.open_app(application.id) as ctx:
        action = ctx.actions.get_action(ctx.conn, "log_expense")
        models = ctx.models.list_models(ctx.conn)
        with pytest.raises(ActionExecutionError) as excinfo:
            ctx.actions.run(ctx.conn, action, {"amount": 38, "extra": 1})

    inspection = inspect_failure(excinfo.value.details, models)
    assert inspection.category == "invalid_input"
    assert inspection.is_definition_problem
    assert excinfo.value.details["rolled_back"] is True


def test_diagnosis_records_primary_report_and_application_relay(
    state, application
) -> None:
    with state.service.open_app(application.id) as ctx:
        action = ctx.actions.get_action(ctx.conn, "log_expense")
    failure = _failure(state, application, action)

    result = asyncio.run(
        state.diagnosis.diagnose(failure, relay_application_id=application.id)
    )

    assert result["ok"] is True
    assert result["definition_problem"] is True
    assert result["has_proposal"] is True

    primary = state.service.get_primary_agent()
    primary_conversation = state.conversations.get_or_create(primary.id)
    primary_messages = state.conversations.messages(primary_conversation.id)
    report = primary_messages[-1]
    assert report["role"] == "assistant"
    assert report["metadata"]["kind"] == "action_diagnosis"
    assert report["metadata"]["action_key"] == "log_expense"
    assert "Action 执行失败排查" in report["content"]
    assert "categry" in report["content"]
    # the proposal is spelled out, so a later "按建议修" turn can apply it
    assert "建议的 Action 定义" in report["content"]
    assert "```json" in report["content"]

    app_agent = state.service.get_agent(application.id)
    app_conversation = state.conversations.get_or_create(app_agent.id, application.id)
    relay = state.conversations.messages(app_conversation.id)[-1]
    assert relay["metadata"]["kind"] == "action_diagnosis_relay"
    assert "主助手" in relay["content"]
    assert result["relay"] == relay["content"]


def test_registry_reports_only_the_latest_run_action_failure(state, application) -> None:
    registry = build_application_tools(state.service, application.id)
    failed = registry.call(
        "run_action", {"action_key": "log_expense", "input": {"amount": "38"}}
    )
    assert failed["ok"] is False
    assert failed["error"]["code"] == "action_failed"
    assert len(registry.failed_action_runs()) == 1
    assert registry.failed_action_runs()[0]["action"]["key"] == "log_expense"

    # a later success clears the failure: the user already recovered
    with state.service.open_app(application.id) as ctx:
        action = ctx.actions.get_action(ctx.conn, "log_expense")
        fixed = action.model_copy(deep=True)
        values = fixed.steps[0].values
        values["category"] = values.pop("categry")
        with ctx.conn:
            ctx.actions.update_action(ctx.conn, action.key, fixed)

    registry.call("run_action", {"action_key": "log_expense", "input": {"amount": "1"}})
    assert registry.failed_action_runs() == []
