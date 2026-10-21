"""AgentScope's ReAct Agent as zooagent's agent runtime.

This is the integration point required by the design document (ADR-004):
AgentScope owns the reasoning loop, the model access and the tool protocol,
while zooagent keeps owning the engines. Our :class:`ToolRegistry` is bridged
into an AgentScope ``Toolkit`` (see ``toolkit.py``), and AgentScope's rich
reply stream is translated into the NDJSON events the desktop app already
understands.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from ...errors import ConfigurationError, ZooAgentError
from ..llm.base import ChatMessage
from ..tools.registry import ToolRegistry
from .convert import parse_tool_arguments, text_of, to_agentscope_messages, tool_calls_in
from .models import require_agentscope
from .toolkit import build_toolkit

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the optional extra
    from agentscope.agent import Agent, ReActConfig
    from agentscope.message import Msg, UserMsg

    AGENTSCOPE_AGENT_AVAILABLE = True
except ImportError:  # pragma: no cover
    AGENTSCOPE_AGENT_AVAILABLE = False


class AgentScopeRuntime:
    """Runs one conversation turn through an AgentScope ReAct agent."""

    def __init__(
        self,
        *,
        model: Any,
        registry: ToolRegistry,
        system_prompt: str,
        max_iterations: int = 20,
        agent_name: str = "zooagent",
    ) -> None:
        self.model = model
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.agent_name = agent_name

    async def stream(
        self, *, history: list[ChatMessage], user_text: str
    ) -> AsyncIterator[dict[str, Any]]:
        if not AGENTSCOPE_AGENT_AVAILABLE:
            require_agentscope()
            raise ConfigurationError("AgentScope Agent 不可用")

        toolkit = await build_toolkit(self.registry)
        agent = Agent(
            name=self.agent_name,
            system_prompt=self.system_prompt,
            model=self.model,
            toolkit=toolkit,
            react_config=ReActConfig(max_iters=self.max_iterations),
        )
        # history must actually reach the agent: without the await the coroutine
        # is dropped and every turn starts from scratch (no multi-turn context).
        observed = to_agentscope_messages(history)
        if observed:
            await agent.observe(observed)

        names: dict[str, str] = {}
        pending_arguments: dict[str, str] = {}
        text_parts: list[str] = []
        tool_log: list[dict[str, Any]] = []
        refresh = False
        error: str | None = None
        final_message: Any | None = None

        def event_kind(item: Any) -> str:
            """Events carry a stable literal `type`; fall back to the class name."""
            literal = getattr(item, "type", None)
            if isinstance(literal, str) and literal:
                return literal.upper()
            name = type(item).__name__
            return name.upper()

        try:
            async for event in agent.reply_stream(
                UserMsg(name="user", content=user_text), yield_final_msg=True
            ):
                if isinstance(event, Msg) or not hasattr(event, "type"):
                    final_message = event
                    continue

                kind = event_kind(event)
                call_id = getattr(event, "tool_call_id", "") or ""

                if kind.startswith("TOOL_CALL_START"):
                    names[call_id] = getattr(event, "tool_call_name", "") or "tool"

                elif kind.startswith("TOOL_CALL_DELTA"):
                    # Arguments stream in as JSON fragments; keep them together
                    # so the UI (and logs) see the real tool input.
                    pending_arguments[call_id] = pending_arguments.get(call_id, "") + (
                        getattr(event, "delta", "") or ""
                    )

                elif kind.startswith("TOOL_CALL_END"):
                    yield {
                        "type": "tool_call",
                        "name": names.get(call_id, "tool"),
                        "arguments": parse_tool_arguments(pending_arguments.get(call_id, "")),
                    }

                elif kind.startswith("TOOL_RESULT_END"):
                    name = names.get(call_id, "tool")
                    state_value = str(getattr(event, "state", "")).lower()
                    ok = "success" in state_value
                    if ok and self.registry.needs_refresh(name):
                        refresh = True
                    tool_log.append({"name": name, "ok": ok})
                    metadata = getattr(event, "metadata", None) or {}
                    yield {
                        "type": "tool_result",
                        "name": name,
                        "ok": ok,
                        "result": metadata.get("result") if isinstance(metadata, dict) else None,
                        "refresh": refresh,
                    }

                elif kind.startswith("TEXT_BLOCK_DELTA"):
                    text_parts.append(getattr(event, "delta", "") or "")

                elif kind.startswith("EXCEED_MAX_ITERS"):
                    error = f"助手连续调用了 {self.max_iterations} 次工具仍未完成任务，已停止。"

                elif kind.startswith("REPLY_END"):
                    if getattr(event, "error", None):
                        error = str(event.error)
                    else:
                        finished = getattr(event, "finished_reason", None)
                        logger.debug("agentscope reply finished: %s", finished)

        except ZooAgentError as exc:
            yield {"type": "error", "message": exc.message, "details": exc.details}
            return
        except Exception as exc:  # pragma: no cover - provider/agent surprises
            logger.exception("agentscope runtime failed")
            yield {"type": "error", "message": f"AgentScope 运行失败: {exc}"}
            return

        content = text_of(final_message) if final_message is not None else ""
        if not content:
            content = "".join(text_parts)

        if error:
            yield {"type": "error", "message": error, "tools": tool_log}
            return

        yield {
            "type": "assistant_message",
            "content": content,
            "tools": tool_log,
            "refresh": refresh,
        }

    @staticmethod
    def tool_calls_seen(messages: list[Any]) -> list[dict[str, Any]]:
        """Exposed for tests and providers that need the AgentScope view."""
        return tool_calls_in(messages)


__all__ = ["AGENTSCOPE_AGENT_AVAILABLE", "AgentScopeRuntime"]
