"""Message and response conversion between zooagent and AgentScope.

Keeping this in one module makes the coupling surface explicit and easy to
re-verify when AgentScope changes.
"""

from __future__ import annotations

from typing import Any

from ..llm.base import ChatMessage

try:  # pragma: no cover - depends on the optional extra
    from agentscope.message import (
        AssistantMsg,
        Msg,
        SystemMsg,
        ToolCallBlock,
        ToolResultBlock,
        UserMsg,
    )

    AGENTSCOPE_MESSAGE_AVAILABLE = True
except ImportError:  # pragma: no cover
    AGENTSCOPE_MESSAGE_AVAILABLE = False


def to_agentscope_messages(history: list[ChatMessage]) -> list[Any]:
    """Convert stored history (text turns only) into AgentScope messages."""
    if not AGENTSCOPE_MESSAGE_AVAILABLE:  # pragma: no cover
        return []
    messages: list[Any] = []
    for message in history:
        if not message.content:
            continue
        if message.role == "user":
            messages.append(UserMsg(name="user", content=message.content))
        elif message.role == "assistant":
            messages.append(AssistantMsg(name="zooagent", content=message.content))
        elif message.role == "system":
            messages.append(SystemMsg(name="system", content=message.content))
    return messages


def text_of(message: Any) -> str:
    """Extract concatenated text from an AgentScope message or block list."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type == "text":
            parts.append(getattr(block, "text", None) or (block.get("text", "") if isinstance(block, dict) else ""))
    return "\n".join(part for part in parts if part)


def blocks_of(message: Any) -> list[Any]:
    content = getattr(message, "content", None)
    if isinstance(content, (list, tuple)):
        return list(content)
    return []


def tool_calls_in(messages: list[Any]) -> list[dict[str, Any]]:
    """Collect every tool call (and its result) found in a message list."""
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for message in messages:
        for block in blocks_of(message):
            if isinstance(block, ToolCallBlock):
                key = block.id or f"{block.name}-{len(order)}"
                calls[key] = {
                    "id": key,
                    "name": block.name,
                    "arguments": parse_tool_arguments(block.input),
                    "state": None,
                    "result": None,
                }
                order.append(key)
            elif isinstance(block, ToolResultBlock):
                key = block.id or ""
                target = calls.get(key)
                if target is None:
                    target = {
                        "id": key,
                        "name": block.name,
                        "arguments": {},
                        "state": None,
                        "result": None,
                    }
                    calls[key] = target
                    order.append(key)
                target["state"] = str(getattr(block.state, "value", block.state))
                target["result"] = parse_tool_output(block.output)
    return [calls[key] for key in order]


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """AgentScope stores tool call input as a raw JSON string."""
    import json

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"__raw__": raw}
    return parsed if isinstance(parsed, dict) else {"__value__": parsed}


def parse_tool_output(output: Any) -> Any:
    """Tool results travel as JSON text inside tool result blocks."""
    import json

    text = ""
    if isinstance(output, str):
        text = output
    elif isinstance(output, (list, tuple)):
        text = text_of(output)
    else:
        text = text_of(output) or ("" if output is None else str(output))
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


__all__ = [
    "AGENTSCOPE_MESSAGE_AVAILABLE",
    "AssistantMsg",
    "Msg",
    "UserMsg",
    "blocks_of",
    "parse_tool_arguments",
    "parse_tool_output",
    "text_of",
    "to_agentscope_messages",
    "tool_calls_in",
]
