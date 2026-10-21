"""Provider-neutral chat protocol.

The runtime only knows about messages, tool calls and tool specs, so adding a
provider (AgentScope, Anthropic, a local runtime) never touches the engines.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class AssistantTurn(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ChatModel(Protocol):
    """Minimal interface: one non-streaming completion with optional tools."""

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> AssistantTurn: ...  # pragma: no cover - protocol
