"""End-to-end: assistants relay a request to each other inside a chat stream.

The application assistant answers first ("已经转给主助手"), then the platform runs
the primary assistant, records both sides as collapsible message cards, and
streams the sender's card into the still-open response.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from zooagent.agents.factory import PreparedAgent
from zooagent.agents.llm.base import AssistantTurn, ChatMessage, ToolCall
from zooagent.agents.messaging import (
    application_message_tool,
    primary_message_tool,
)
from zooagent.agents.runtime import AgentRuntime
from zooagent.agents.tools import ToolRegistry, build_application_tools
from zooagent.main import create_app


class _ForwardingModel:
    """Application assistant: forwards the request, then answers the user."""

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
                        name="send_message_to_primary",
                        arguments={"message": "把助手改得更温柔一点"},
                    )
                ],
            )
        return AssistantTurn(content="已经转给主助手，答复会出现在这个对话里。", tool_calls=[])


class _PlanModel:
    """Primary assistant in read-only relay mode: answers with a plan."""

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> AssistantTurn:
        names = [item["function"]["name"] for item in tools]
        assert "define_model" not in names  # 被联系时只读
        return AssistantTurn(content="方案：把应用指令改成「先共情，再给结论」。", tool_calls=[])


class _AskingModel:
    """Primary assistant: asks an application assistant for a number."""

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
                        name="send_message_to_application",
                        arguments={
                            "application_id": self.application_id,
                            "message": "最近三天一共花了多少？",
                        },
                    )
                ],
            )
        return AssistantTurn(content="已经问过记账助手，答复马上到。", tool_calls=[])


class _AnsweringModel:
    """Application assistant answering the primary's question."""

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> AssistantTurn:
        return AssistantTurn(content="最近三天一共 128 元。", tool_calls=[])


def _events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_application_assistant_forwards_to_primary(state, monkeypatch) -> None:
    application = state.service.create_application("个人记账")
    agent = state.service.get_agent(application.id)

    registry = build_application_tools(state.service, application.id)
    primary_message_tool(registry)
    prepared = PreparedAgent(
        runtime=AgentRuntime(
            chat_model=_ForwardingModel(), registry=registry, system_prompt="test"
        ),
        agent=agent,
        conversation_rows=[],
    )
    monkeypatch.setattr(state.agents, "application", lambda _app_id, **_kw: prepared)

    primary_agent = state.service.get_primary_agent()
    primary_registry = build_application_tools(state.service, application.id).subset(
        {"list_models"}
    )
    plan = PreparedAgent(
        runtime=AgentRuntime(
            chat_model=_PlanModel(), registry=primary_registry, system_prompt="test"
        ),
        agent=primary_agent,
        conversation_rows=[],
    )
    monkeypatch.setattr(state.agents, "for_messaging", lambda _sender: plan)

    with TestClient(create_app(state)) as client:
        client.headers.update({"Authorization": f"Bearer {state.token}"})
        response = client.post(
            f"/api/applications/{application.id}/chat",
            json={"message": "把助手改得更温柔一点"},
        )

    assert response.status_code == 200
    events = _events(response)
    types = [event["type"] for event in events]

    await_calls = [
        event
        for event in events
        if event["type"] == "tool_call" and event["name"] == "await_reply"
    ]
    assert len(await_calls) == 1

    cards = [event for event in events if event["type"] == "agent_message"]
    assert len(cards) == 1
    assert cards[0]["direction"] == "out"
    assert cards[0]["peer"]["name"] == "主助手"
    assert "先共情" in cards[0]["reply"]
    assert types[-1] == "done"

    # 发送方（应用助手）保留一张「回复了主助手」卡片
    app_conversation = state.conversations.get_or_create(agent.id, application.id)
    app_messages = state.conversations.messages(app_conversation.id)
    card = app_messages[-1]
    assert card["metadata"]["kind"] == "agent_message"
    assert card["metadata"]["direction"] == "out"
    assert card["metadata"]["request"] == "把助手改得更温柔一点"
    assert "先共情" in card["content"]

    # 接收方（主助手）看到「收到了个人记账的消息」，随后是它的回答
    primary_conversation = state.conversations.get_or_create(primary_agent.id)
    received = state.conversations.messages(primary_conversation.id)
    assert received[-2]["role"] == "user"
    assert received[-2]["metadata"]["kind"] == "agent_message"
    assert received[-2]["metadata"]["direction"] == "in"
    assert received[-2]["metadata"]["peer"]["name"] == "个人记账"
    assert received[-1]["role"] == "assistant"
    assert "先共情" in received[-1]["content"]


def test_primary_assistant_can_ask_an_application_assistant(state, monkeypatch) -> None:
    application = state.service.create_application("个人记账")
    agent = state.service.get_agent(application.id)

    asking = _AskingModel()
    asking.application_id = application.id
    primary_registry = ToolRegistry()
    application_message_tool(primary_registry, state.service)
    prepared = PreparedAgent(
        runtime=AgentRuntime(
            chat_model=asking,
            registry=primary_registry,
            system_prompt="test",
        ),
        agent=state.service.get_primary_agent(),
        conversation_rows=[],
    )
    monkeypatch.setattr(state.agents, "primary", lambda: prepared)

    answering = PreparedAgent(
        runtime=AgentRuntime(
            chat_model=_AnsweringModel(),
            registry=build_application_tools(state.service, application.id),
            system_prompt="test",
        ),
        agent=agent,
        conversation_rows=[],
    )
    monkeypatch.setattr(
        state.agents, "application", lambda _app_id, **_kw: answering
    )

    with TestClient(create_app(state)) as client:
        client.headers.update({"Authorization": f"Bearer {state.token}"})
        response = client.post("/api/chat/primary", json={"message": "问一下记账助手"})

    events = _events(response)
    cards = [event for event in events if event["type"] == "agent_message"]
    assert len(cards) == 1
    assert cards[0]["peer"]["name"] == "个人记账"
    assert cards[0]["reply"] == "最近三天一共 128 元。"

    # 应用助手的对话里留下了「收到了主助手的消息」以及自己的回答
    app_conversation = state.conversations.get_or_create(agent.id, application.id)
    app_messages = state.conversations.messages(app_conversation.id)
    assert app_messages[-2]["metadata"]["kind"] == "agent_message"
    assert app_messages[-2]["metadata"]["direction"] == "in"
    assert app_messages[-2]["metadata"]["peer"]["name"] == "主助手"
    assert app_messages[-1]["content"] == "最近三天一共 128 元。"
