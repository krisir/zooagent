"""A mutating tool that succeeds must mark the stream for a UI refresh.

The data page only refetches when the `done` event carries `refresh: true`;
that flag is derived from `ok AND tool.refresh`. A failing Action used to leave
the record written but the flag false, so the page kept showing stale rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from zooagent.actions.schema import ActionDef
from zooagent.agents.llm.base import AssistantTurn, ChatMessage, ToolCall
from zooagent.agents.runtime import AgentRuntime
from zooagent.agents.tools import build_application_tools
from zooagent.data.query import QuerySpec
from zooagent.models.schema import FieldDef, ModelDef


class _ScriptedModel:
    """Calls `run_action` once, then answers with a final message."""

    def __init__(self, action_key: str, inputs: dict[str, Any]) -> None:
        self.action_key = action_key
        self.inputs = inputs
        self.turns = 0

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        self.turns += 1
        if self.turns == 1:
            return AssistantTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="run_action",
                        arguments={"action_key": self.action_key, "input": self.inputs},
                    )
                ],
            )
        return AssistantTurn(content="记录好了", tool_calls=[])


async def _collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]


def _log_period_action() -> ActionDef:
    return ActionDef.model_validate(
        {
            "key": "log_period",
            "name": "记录一次经期",
            "inputs": [{"key": "start_date", "name": "开始日期", "type": "date", "required": True}],
            "steps": [
                {
                    "id": "create_record",
                    "type": "create",
                    "model": "period_record",
                    "output": "record",
                    "values": {"start_date": {"ref": "input.start_date"}},
                },
                # the shape the AI actually wrote in the reported bug
                {"id": "done", "type": "return", "value": {"ref": "record"}},
            ],
        }
    )


def test_successful_action_marks_refresh(ctx, state) -> None:
    ctx.models.create_model(
        ctx.conn,
        ModelDef(
            key="period_record",
            name="经期记录",
            title_field="start_date",
            fields=[FieldDef(key="start_date", name="开始日期", type="date", required=True)],
        ),
    )
    ctx.actions.create_action(ctx.conn, _log_period_action())

    registry = build_application_tools(state.service, ctx.application.id)
    scripted = _ScriptedModel("log_period", {"start_date": "2026-10-20"})
    runtime = AgentRuntime(chat_model=scripted, registry=registry, system_prompt="test")

    events = asyncio.run(_collect(runtime.stream(history=[], user_text="今天来了")))
    final = [event for event in events if event["type"] == "assistant_message"][-1]
    assert final["refresh"] is True
    assert [(tool["name"], tool["ok"]) for tool in final["tools"]] == [("run_action", True)]

    model = ctx.models.get_model(ctx.conn, "period_record")
    assert ctx.data.query_records(ctx.conn, model, QuerySpec())["total"] == 1
