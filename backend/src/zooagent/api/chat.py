"""Chat endpoints. Responses are newline-delimited JSON event streams.

NDJSON (one JSON object per line) is used instead of SSE because it works
identically with ``fetch`` + ``POST`` and needs no polyfill in the webview.
Event types: ``assistant_note``, ``tool_call``, ``tool_result``,
``assistant_message``, ``error``, ``done``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse

from ..application.conversations import Conversation
from ..errors import ValidationFailure
from ..state import AppState
from .deps import get_state, require_token

router = APIRouter(tags=["chat"], dependencies=[Depends(require_token)])

NDJSON_MEDIA_TYPE = "application/x-ndjson"

#: Relayed messages keep running even if the user closes the stream (they were
#: already told "已经转达"), so hold a reference until the task finishes.
_RELAY_TASKS: set[asyncio.Task[Any]] = set()


def _sender_peer(
    state: AppState,
    *,
    scope: str,
    application_id: str | None,
    agent_id: str,
    agent_name: str,
) -> dict[str, Any]:
    """How the sender is labelled on the other assistant's message card."""
    if scope == "primary" or not application_id:
        return {"id": agent_id, "name": "主助手", "application_id": None}
    try:
        name = state.service.get_application(application_id).name
    except Exception:  # pragma: no cover - app deleted mid-flight
        name = agent_name
    return {"id": agent_id, "name": name, "application_id": application_id}


def _resolve_conversation(
    state: AppState,
    *,
    agent_id: str,
    application_id: str | None,
    message: str,
    conversation_id: str | None,
) -> Conversation:
    if conversation_id:
        conversation = state.conversations.get(conversation_id)
        if conversation.agent_id != agent_id:
            raise ValidationFailure("该会话不属于这个助手")
        return conversation
    return state.conversations.get_or_create(
        agent_id, application_id, title=message[:40]
    )


def _chat_response(
    state: AppState,
    *,
    scope: str,
    application_id: str | None,
    payload: dict[str, Any],
) -> StreamingResponse:
    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValidationFailure("消息不能为空")

    prepared = (
        state.agents.primary()
        if scope == "primary"
        else state.agents.application(str(application_id))
    )
    conversation = _resolve_conversation(
        state,
        agent_id=prepared.agent.id,
        application_id=application_id,
        message=message,
        conversation_id=payload.get("conversation_id"),
    )
    history = state.agents.history(conversation.id)
    state.conversations.append(conversation.id, "user", message)
    sender = _sender_peer(
        state,
        scope=scope,
        application_id=application_id,
        agent_id=prepared.agent.id,
        agent_name=prepared.agent.name,
    )
    registry = getattr(prepared.runtime, "registry", None)

    async def generator() -> AsyncIterator[str]:
        import json

        # 先报会话 id：用户随时可能点"停止"（断开连接），客户端得知道这一轮
        # 落在哪个会话里，下一次发言才不会再开一个新会话。
        yield (
            json.dumps({"type": "start", "conversation_id": conversation.id}, ensure_ascii=False)
            + "\n"
        )

        final_text = ""
        tool_log: list[dict[str, Any]] = []
        refresh = False
        errored = False

        async for event in prepared.runtime.stream(history=history, user_text=message):
            event_type = event.get("type")
            if event_type == "assistant_message":
                final_text = event.get("content") or ""
                tool_log = event.get("tools") or []
                refresh = bool(event.get("refresh"))
            elif event_type == "error":
                errored = True
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"

        stored = None
        if final_text or tool_log:
            stored = state.conversations.append(
                conversation.id,
                "assistant",
                final_text or None,
                {"tools": tool_log, "refresh": refresh, "errored": errored},
            )

        # Assistant-to-assistant messages queued during the turn are delivered
        # now: the assistant has already answered the user ("已经转达…"), and the
        # other assistant's reply arrives as a collapsible card.
        for outgoing in registry.pending_messages() if registry is not None else []:
            yield (
                json.dumps(
                    {
                        "type": "tool_call",
                        "name": "await_reply",
                        "arguments": {"to": outgoing.target_name},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            relay = await _deliver(state, outgoing, sender, conversation.id)
            yield (
                json.dumps(
                    {
                        "type": "tool_result",
                        "name": "await_reply",
                        "ok": relay["ok"],
                        "result": {"peer": relay["peer"]},
                        "error": relay.get("error"),
                        "refresh": False,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            yield (
                json.dumps(
                    {
                        "type": "agent_message",
                        "direction": "out",
                        "ok": relay["ok"],
                        "peer": relay["peer"],
                        "request": relay["request"],
                        "reply": relay["reply"],
                        "error": relay.get("error"),
                        "conversation_id": conversation.id,
                        "message_id": relay.get("message_id"),
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )

        # Failed Actions are handed to the primary assistant for a diagnosis.
        # This happens after the application assistant has answered, so the
        # user always sees the failure first and the report second.
        failures = (
            registry.failed_action_runs()
            if scope == "application" and registry is not None
            else []
        )
        for failure in failures:
            action = failure.get("action") or {}
            yield (
                json.dumps(
                    {
                        "type": "tool_call",
                        "name": "diagnose_action",
                        "arguments": {"action_key": action.get("key")},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            diagnosis = await state.diagnosis.diagnose(
                failure,
                relay_application_id=application_id,
                relay_conversation_id=conversation.id,
            )
            yield (
                json.dumps(
                    {
                        "type": "tool_result",
                        "name": "diagnose_action",
                        "ok": bool(diagnosis.get("ok")),
                        "result": {
                            key: diagnosis.get(key)
                            for key in (
                                "summary",
                                "definition_problem",
                                "has_proposal",
                            )
                        },
                        "error": diagnosis.get("error"),
                        "refresh": False,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            if diagnosis.get("relay"):
                # Persisted by the diagnoser already; this event only paints it.
                yield (
                    json.dumps(
                        {
                            "type": "assistant_message",
                            "content": diagnosis["relay"],
                            "tools": [],
                            "refresh": False,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        yield (
            json.dumps(
                {
                    "type": "done",
                    "conversation_id": conversation.id,
                    "message_id": stored["id"] if stored else None,
                    "refresh": refresh,
                    "errored": errored,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    return StreamingResponse(generator(), media_type=NDJSON_MEDIA_TYPE)


async def _deliver(
    state: AppState,
    outgoing: Any,
    sender: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    """Deliver one queued message without being killed by a client disconnect."""
    task = asyncio.create_task(
        state.messaging.deliver(
            outgoing, sender=sender, sender_conversation_id=conversation_id
        )
    )
    _RELAY_TASKS.add(task)
    task.add_done_callback(_RELAY_TASKS.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 用户点了「停止」：转达已经发出，让对方答完再落库。
        raise


def _conversation_payload(state: AppState, conversation: Conversation) -> dict[str, Any]:
    return {
        "conversation": conversation.to_dict(),
        "messages": state.conversations.messages(conversation.id),
    }


@router.post("/chat/primary")
def chat_primary(
    payload: dict[str, Any] = Body(...), state: AppState = Depends(get_state)
) -> StreamingResponse:
    return _chat_response(state, scope="primary", application_id=None, payload=payload)


@router.get("/chat/primary")
def primary_conversation(state: AppState = Depends(get_state)) -> dict[str, Any]:
    agent = state.service.get_primary_agent()
    conversation = state.conversations.get_or_create(agent.id)
    return _conversation_payload(state, conversation)


@router.post("/applications/{application_id}/chat")
def chat_application(
    application_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    return _chat_response(
        state, scope="application", application_id=application_id, payload=payload
    )


@router.get("/applications/{application_id}/chat")
def application_conversation(
    application_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    agent = state.service.get_agent(application_id)
    if agent is None:
        raise ValidationFailure("该应用还没有专属助手")
    conversation = state.conversations.get_or_create(agent.id, application_id)
    return _conversation_payload(state, conversation)


@router.get("/conversations/{conversation_id}/messages")
def conversation_messages(
    conversation_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    conversation = state.conversations.get(conversation_id)
    return _conversation_payload(state, conversation)


@router.post("/conversations/{conversation_id}/reset")
def reset_conversation(
    conversation_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    """Start a fresh conversation for the same agent (the old one is kept)."""
    conversation = state.conversations.get(conversation_id)
    created = state.conversations.create(
        conversation.agent_id, conversation.application_id
    )
    return _conversation_payload(state, created)
