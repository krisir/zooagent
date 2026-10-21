"""Bridge zooagent tools into AgentScope's Toolkit.

Our tools are plain ``(dict) -> Any`` callables with a JSON schema. AgentScope
expects ``FunctionTool`` instances whose callable returns a ``ToolChunk``. This
module is the only place that knows both shapes.
"""

from __future__ import annotations

import json
from typing import Any

from ...errors import ConfigurationError
from ..tools.registry import Tool, ToolRegistry

try:  # pragma: no cover - depends on the optional extra
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool, ToolChunk, Toolkit

    AGENTSCOPE_TOOLING_AVAILABLE = True
except ImportError:  # pragma: no cover
    AGENTSCOPE_TOOLING_AVAILABLE = False


#: zooagent tools are already validated by the engines (types, constraints,
#: relations, transactions) and the platform deliberately exposes no shell or
#: filesystem access (ADR-013). AgentScope's default mode asks the user for
#: every call, which would stall an unattended run, so we answer ALLOW here
#: instead of prompting.
ALLOW_DECISION_MESSAGE = "由 zooagent 引擎校验并执行"


def _text_content(text: str) -> Any:
    """ToolChunk.content accepts blocks; keep a text fallback for safety."""
    try:
        return TextBlock(type="text", text=text)
    except Exception:  # pragma: no cover - defensive
        return text


def _chunk(payload: Any, state: Any) -> Any:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        return ToolChunk(content=text, state=state, is_last=True)
    except Exception:  # pragma: no cover - depends on content typing
        return ToolChunk(content=[_text_content(text)], state=state, is_last=True)


def make_function_tool(registry: ToolRegistry, tool: Tool) -> Any:
    """Wrap one registry tool as an AgentScope ``FunctionTool``."""

    def invoke(*args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], dict) and not kwargs:
            arguments = dict(args[0])
        else:
            arguments = {key: value for key, value in kwargs.items() if value is not None}
        result = registry.call(tool.name, arguments)
        if result.get("ok"):
            return _chunk(result.get("result"), ToolResultState.SUCCESS)
        return _chunk(result.get("error"), ToolResultState.ERROR)

    return FunctionTool(
        func=invoke,
        name=tool.name,
        description=tool.description,
        input_schema=tool.parameters,
        is_read_only=not tool.mutates,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=ALLOW_DECISION_MESSAGE,
        ),
    )


def build_toolkit(registry: ToolRegistry) -> Any:
    """Convert an entire registry into AgentScope's toolkit (awaitable).

    AgentScope's ``Toolkit.add_tool`` is a coroutine, so this helper is async.
    """
    if not AGENTSCOPE_TOOLING_AVAILABLE:
        raise ConfigurationError("AgentScope 未安装，无法构建工具集")

    async def _build() -> Any:
        toolkit = Toolkit()
        for tool in registry.tools.values():
            await toolkit.add_tool(make_function_tool(registry, tool))
        return toolkit

    return _build()


__all__ = ["build_toolkit", "make_function_tool"]
