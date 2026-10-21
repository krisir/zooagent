"""Agent runtime: a small, provider-neutral tool-calling loop.

The loop is deliberately simple and observable - it streams every tool call and
its result to the client so the user can always see what the assistant did.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from ..errors import ZooAgentError
from .llm.base import ChatMessage, ChatModel
from .tools.registry import ToolRegistry


class AgentRuntimeProtocol(Protocol):
    """What the HTTP layer needs from a runtime (built-in or AgentScope)."""

    def stream(
        self, *, history: list[ChatMessage], user_text: str
    ) -> AsyncIterator[dict[str, Any]]: ...  # pragma: no cover - protocol


class AgentRuntime:
    def __init__(
        self,
        *,
        chat_model: ChatModel,
        registry: ToolRegistry,
        system_prompt: str,
        max_iterations: int = 20,
    ) -> None:
        self.chat_model = chat_model
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations

    async def stream(
        self,
        *,
        history: list[ChatMessage],
        user_text: str,
    ) -> AsyncIterator[dict[str, Any]]:
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            *history,
            ChatMessage(role="user", content=user_text),
        ]
        tool_log: list[dict[str, Any]] = []
        refresh = False

        for _ in range(self.max_iterations):
            try:
                turn = await self.chat_model.complete(messages, self.registry.specs())
            except ZooAgentError as exc:
                yield {"type": "error", "message": exc.message, "details": exc.details}
                return
            except Exception as exc:  # pragma: no cover - provider surprises
                yield {"type": "error", "message": f"模型调用失败: {exc}"}
                return

            if not turn.tool_calls:
                yield {
                    "type": "assistant_message",
                    "content": turn.content or "",
                    "tools": tool_log,
                    "refresh": refresh,
                }
                return

            if turn.content:
                yield {"type": "assistant_note", "content": turn.content}

            messages.append(
                ChatMessage(
                    role="assistant", content=turn.content, tool_calls=turn.tool_calls
                )
            )

            for call in turn.tool_calls:
                yield {
                    "type": "tool_call",
                    "name": call.name,
                    "arguments": call.arguments,
                }
                result = self.registry.call(call.name, call.arguments)
                if result.get("ok") and self.registry.needs_refresh(call.name):
                    refresh = True
                yield {
                    "type": "tool_result",
                    "name": call.name,
                    "ok": result.get("ok", False),
                    "result": result.get("result"),
                    "error": result.get("error"),
                    "refresh": refresh,
                }
                tool_log.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": result.get("ok", False),
                    }
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id or f"call_{len(tool_log)}",
                        name=call.name,
                        content=_serialize_result(result),
                    )
                )

        yield {
            "type": "error",
            "message": f"助手连续调用了 {self.max_iterations} 次工具仍未完成任务，已停止。",
            "tools": tool_log,
        }


#: Tool results are replayed to the model on the next turn, so they have to stay
#: inside a sane token budget *and* remain valid JSON - truncating the serialized
#: text would hand the model a broken envelope it cannot parse.
MAX_TOOL_RESULT_CHARS = 20_000

TRUNCATION_NOTE = (
    "工具结果过大，已截断。请缩小范围（更小的 limit/offset、更精确的 filters、"
    "或改用 aggregate 聚合）后再试。"
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _shrink(value: Any, budget: int) -> Any:
    """Return a JSON-safe value of about ``budget`` characters."""
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        items = value["items"]
        keep = len(items)
        while keep > 0:
            candidate = {**value, "items": items[:keep], "truncated": True}
            if len(_dumps(candidate)) <= budget:
                return candidate
            keep //= 2
        return {
            **{key: item for key, item in value.items() if key != "items"},
            "items": [],
            "truncated": True,
        }
    try:
        text = _dumps(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)
    if len(text) <= budget:
        return value
    return {"truncated": True, "preview": text[: max(budget, 200)]}


def _serialize_result(result: dict[str, Any]) -> str:
    """Send the whole envelope (``ok`` plus payload) back to the model so it can
    tell success from failure and recover from validation errors.

    An oversized result is shrunk *before* serialisation: the model must always
    receive parseable JSON, so it is told the payload was truncated instead of
    getting a half-written object.
    """
    try:
        text = _dumps(result)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return _dumps({"ok": result.get("ok", False), "result": str(result)})
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text

    shrunk: dict[str, Any] = {
        key: value for key, value in result.items() if key != "result"
    }
    shrunk["result"] = _shrink(result.get("result"), MAX_TOOL_RESULT_CHARS // 2)
    shrunk["truncated"] = True
    shrunk["note"] = TRUNCATION_NOTE
    text = _dumps(shrunk)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return _dumps({"ok": result.get("ok", False), "truncated": True, "note": TRUNCATION_NOTE})


def to_history_messages(rows: list[dict[str, Any]], limit: int = 20) -> list[ChatMessage]:
    """Convert stored conversation rows into LLM history (text turns only)."""
    messages: list[ChatMessage] = []
    for row in rows:
        if row["role"] not in {"user", "assistant"}:
            continue
        if not row.get("content"):
            continue
        messages.append(ChatMessage(role=row["role"], content=row["content"]))
    return messages[-limit:]
