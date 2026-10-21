"""End-to-end: a failing Action is diagnosed inside the chat stream.

The application assistant answers first (the user must see the failure), then
the platform runs the primary assistant's diagnosis, paints it as a second
bubble, and stores both the report and the relay.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from zooagent.actions.schema import ActionDef
from zooagent.agents.factory import PreparedAgent
from zooagent.agents.llm.base import AssistantTurn, ChatMessage, ToolCall
from zooagent.agents.runtime import AgentRuntime
from zooagent.agents.tools import build_application_tools
from zooagent.main import create_app
from zooagent.models.schema import FieldDef, ModelDef


class _FailingActionModel:
    """Calls `run_action` once, then reports the failure to the user."""

    def __init__(self) -> None:
        self.turns = 0

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> AssistantTurn:
        self.turns += 1
        if self.turns == 1:
            return AssistantTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="run_action",
                        arguments={
                            "action_key": "log_expense",
                            "input": {"amount": "38", "note": "午餐"},
                        },
                    )
                ],
            )
        return AssistantTurn(content="这笔支出没有记上，执行失败了。", tool_calls=[])


def _broken_action() -> ActionDef:
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
                        # the model declares `category`; this typo fails every run
                        "categry": "food",
                        "amount": {"ref": "input.amount"},
                        "note": {"ref": "input.note"},
                    },
                }
            ],
        }
    )


def _prepare(state):
    application = state.service.create_application("个人记账")
    with state.service.open_app(application.id) as ctx:
        ctx.models.create_model(
            ctx.conn,
            ModelDef(
                key="transaction",
                name="交易",
                title_field="note",
                fields=[
                    FieldDef(
                        key="amount", name="金额", type="decimal", scale=2, required=True
                    ),
                    FieldDef(key="note", name="备注", type="text"),
                    FieldDef(
                        key="category",
                        name="分类",
                        type="enum",
                        options=[{"value": "food", "label": "餐饮"}],
                    ),
                ],
            ),
        )
        ctx.actions.create_action(ctx.conn, _broken_action())
    return application


def test_chat_stream_diagnoses_a_failed_action(state, monkeypatch) -> None:
    application = _prepare(state)
    agent = state.service.get_agent(application.id)
    runtime = AgentRuntime(
        chat_model=_FailingActionModel(),
        registry=build_application_tools(state.service, application.id),
        system_prompt="test",
    )
    prepared = PreparedAgent(runtime=runtime, agent=agent, conversation_rows=[])
    monkeypatch.setattr(state.agents, "application", lambda _app_id: prepared)

    with TestClient(create_app(state)) as client:
        client.headers.update({"Authorization": f"Bearer {state.token}"})
        response = client.post(
            f"/api/applications/{application.id}/chat", json={"message": "午餐花了 38"}
        )

    assert response.status_code == 200
    events = [
        json.loads(line) for line in response.text.splitlines() if line.strip()
    ]
    types = [event["type"] for event in events]

    # 1. the application assistant answers, then 2. the diagnosis runs
    first_answer = events[types.index("assistant_message")]
    assert first_answer["content"] == "这笔支出没有记上，执行失败了。"
    diagnose_calls = [
        event
        for event in events
        if event["type"] == "tool_call" and event["name"] == "diagnose_action"
    ]
    assert len(diagnose_calls) == 1
    diagnosis_result = [
        event
        for event in events
        if event["type"] == "tool_result" and event["name"] == "diagnose_action"
    ][0]
    assert diagnosis_result["ok"] is True
    assert diagnosis_result["result"]["definition_problem"] is True
    assert diagnosis_result["result"]["has_proposal"] is True

    # a relay bubble is painted in the application conversation
    relays = [
        event
        for event in events
        if event["type"] == "assistant_message" and "主助手" in event["content"]
    ]
    assert len(relays) == 1
    assert "categry" in relays[0]["content"] or "不存在" in relays[0]["content"]

    # both conversations kept a record
    app_agent = state.service.get_agent(application.id)
    conversation = state.conversations.get_or_create(app_agent.id, application.id)
    stored = state.conversations.messages(conversation.id)
    kinds = [message["metadata"].get("kind") for message in stored if message["metadata"]]
    assert "action_diagnosis_relay" in kinds

    primary = state.service.get_primary_agent()
    primary_conversation = state.conversations.get_or_create(primary.id)
    primary_messages = state.conversations.messages(primary_conversation.id)
    assert primary_messages[-1]["metadata"]["kind"] == "action_diagnosis"
    assert "categry" in primary_messages[-1]["content"]
