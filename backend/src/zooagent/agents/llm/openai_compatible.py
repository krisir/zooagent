"""OpenAI-compatible chat completions.

Works with OpenAI, DeepSeek, Moonshot, Qwen (DashScope compatible mode),
OpenRouter and any local server exposing the same API (Ollama, LM Studio,
vLLM). No provider-specific SDK is required.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ...config import ModelProfile
from ...errors import ProviderError
from .base import AssistantTurn, ChatMessage, ToolCall


def _to_payload_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            payload.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id or f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for index, call in enumerate(message.tool_calls)
                    ],
                }
            )
        elif message.role == "tool":
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
            )
        else:
            payload.append({"role": message.role, "content": message.content or ""})
    return payload


class OpenAICompatibleModel:
    def __init__(self, profile: ModelProfile, *, timeout: float = 120.0) -> None:
        self.profile = profile
        self.timeout = timeout

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        base_url = (self.profile.base_url or "").rstrip("/")
        if not base_url:
            raise ProviderError("模型配置缺少 base_url")
        payload: dict[str, Any] = {
            "model": self.profile.model_name,
            "messages": _to_payload_messages(messages),
            "temperature": self.profile.temperature,
            "max_tokens": self.profile.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.profile.api_key:
            headers["Authorization"] = f"Bearer {self.profile.api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"无法连接模型服务: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"模型服务返回 {response.status_code}",
                details=response.text[:800],
            )

        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderError("模型服务返回了无法解析的响应") from exc

        tool_calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    raise ProviderError(
                        f"模型生成的工具参数不是合法 JSON: {function.get('name')}"
                    ) from None
            elif isinstance(arguments, dict):
                parsed = arguments
            else:
                parsed = {}
            tool_calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{index}",
                    name=function.get("name") or "",
                    arguments=parsed,
                )
            )

        return AssistantTurn(content=message.get("content"), tool_calls=tool_calls)
