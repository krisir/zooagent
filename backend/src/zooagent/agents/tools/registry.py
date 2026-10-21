"""Tool registry shared by the primary and application assistants."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ...errors import ZooAgentError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..messaging import OutgoingMessage

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Any]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    #: True when the tool changes application structure or business data.
    mutates: bool = False
    #: True when the UI should refresh after a successful call.
    refresh: bool = False


@dataclass
class ToolCallRecord:
    """One executed tool call, kept so the chat layer can react to failures."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    error: dict[str, Any] | None = None


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    #: Calls made during one agent turn (in order), including failures.
    calls: list[ToolCallRecord] = field(default_factory=list)
    #: Messages queued by tools, delivered after the turn ends (see messaging).
    outbox: list[OutgoingMessage] = field(default_factory=list)

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Handler,
        *,
        mutates: bool = False,
        refresh: bool = False,
    ) -> None:
        if name in self.tools:
            raise ValueError(f"重复注册工具 {name}")
        self.tools[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            mutates=mutates,
            refresh=refresh,
        )

    def has(self, name: str) -> bool:
        return name in self.tools

    def subset(self, names: set[str] | frozenset[str]) -> ToolRegistry:
        """A registry with only the allowed tools (used for bounded runs)."""
        return ToolRegistry(
            tools={name: tool for name, tool in self.tools.items() if name in names}
        )

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools.values()
        ]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "mutates": tool.mutates,
            }
            for tool in self.tools.values()
        ]

    def pending_messages(self) -> list[OutgoingMessage]:
        """Assistant-to-assistant messages queued during this turn."""
        return list(self.outbox)

    def needs_refresh(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and tool.refresh)

    def call(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments or {}
        result = self._call(name, arguments)
        self.calls.append(
            ToolCallRecord(
                name=name,
                arguments=arguments,
                ok=bool(result.get("ok")),
                error=result.get("error"),
            )
        )
        return result

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "error": {
                    "code": "unknown_tool",
                    "message": f"不存在工具 '{name}'，可用工具: {sorted(self.tools)}",
                },
            }
        try:
            result = tool.handler(arguments)
            return {"ok": True, "result": result}
        except ZooAgentError as exc:
            return {"ok": False, "error": exc.to_dict()["error"]}
        except ValidationError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "工具参数不合法",
                    "details": exc.errors(),
                },
            }
        except KeyError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": f"缺少参数 {exc}",
                },
            }
        except (TypeError, ValueError) as exc:
            return {
                "ok": False,
                "error": {"code": "invalid_arguments", "message": str(exc)},
            }
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tool %s failed", name)
            return {
                "ok": False,
                "error": {"code": "internal_error", "message": str(exc)},
            }


# ----------------------------------------------------------------------
# JSON schema helpers
# ----------------------------------------------------------------------
    def failed_action_runs(self) -> list[dict[str, Any]]:
        """Action failures worth diagnosing: the last run of each Action failed.

        An Action that failed and was then retried successfully (for example
        after the assistant corrected the input) is *not* a definition bug, so
        a later success clears the earlier failure.
        """
        failed: dict[str, dict[str, Any]] = {}
        for call in self.calls:
            if call.name != "run_action":
                continue
            key = str(call.arguments.get("action_key") or "")
            if call.ok:
                failed.pop(key, None)
                continue
            error = call.error or {}
            if error.get("code") != "action_failed":
                continue
            details = error.get("details")
            if isinstance(details, dict):
                failed[key] = details
        return list(failed.values())


def object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


FIELD_TYPES = [
    "text",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "relation",
    "json",
]


def field_schema() -> dict[str, Any]:
    return object_schema(
        {
            "key": {"type": "string", "description": "小写字母开头的英文标识，如 amount"},
            "name": {"type": "string", "description": "用户可见的中文名称"},
            "type": {"type": "string", "enum": FIELD_TYPES},
            "description": {"type": "string"},
            "required": {"type": "boolean"},
            "unique": {"type": "boolean"},
            "default": {
                "description": "默认值，类型需与字段类型一致",
                "type": ["string", "number", "boolean", "null"],
            },
            "options": {
                "type": "array",
                "description": "enum 字段的选项",
                "items": object_schema(
                    {"value": {"type": "string"}, "label": {"type": "string"}},
                    ["value"],
                ),
            },
            "scale": {"type": "integer", "description": "decimal 小数位，默认 2"},
            "target_model": {
                "type": "string",
                "description": "relation 字段关联的模型 key",
            },
            "multiple": {"type": "boolean", "description": "relation 是否多选"},
        },
        ["key", "name", "type"],
    )


def page_schema() -> dict[str, Any]:
    return object_schema(
        {
            "key": {"type": "string"},
            "title": {"type": "string"},
            "icon": {"type": "string"},
            "description": {"type": "string"},
            "layout": {
                "type": "string",
                "enum": ["vertical", "grid", "two_column"],
            },
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": (
                        "组件：type 为 Heading/Text/DataTable/Form/DetailView/StatCard/"
                        "Chart/Calendar/Tabs/ActionButton/FilterPanel/ChatPanel"
                    ),
                    "properties": {
                        "type": {"type": "string"},
                        "title": {"type": "string"},
                        "model": {"type": "string"},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "columns": {"type": "array", "items": {"type": "object"}},
                        "actions": {"type": "array", "items": {"type": "object"}},
                        "props": {"type": "object"},
                    },
                    "additionalProperties": True,
                    "required": ["type"],
                },
            },
        },
        ["key", "title", "components"],
    )


def filters_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": object_schema(
            {
                "field": {"type": "string"},
                "operator": {
                    "type": "string",
                    "enum": [
                        "eq",
                        "ne",
                        "gt",
                        "gte",
                        "lt",
                        "lte",
                        "contains",
                        "startswith",
                        "endswith",
                        "in",
                        "between",
                        "is_null",
                        "not_null",
                    ],
                },
                "value": {},
            },
            ["field", "operator"],
        ),
    }


def sort_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": object_schema(
            {
                "field": {"type": "string"},
                "direction": {"type": "string", "enum": ["asc", "desc"]},
            },
            ["field"],
        ),
    }
