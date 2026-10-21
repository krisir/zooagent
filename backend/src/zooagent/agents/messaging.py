"""Assistants talking to each other.

The primary assistant owns application *structure*; each application assistant
owns that application's *data*. When the user asks an application assistant for
something only the primary can do (add a field, redo a page, rewrite the
assistant itself), the application assistant forwards the request instead of
pretending it can do it.

Delivery is asynchronous from the user's point of view: the sender answers
first ("已经转给主助手"), then the platform runs the other assistant and records
both sides of the exchange as collapsible message cards:

* receiver  — ``kind: agent_message`` / ``direction: in``  「收到了「个人记账」的消息」
* sender    — ``kind: agent_message`` / ``direction: out`` 「回复了「主助手」」

The primary assistant is **read-only** when it is contacted this way: it answers
with a plan, and the user confirms inside the primary conversation, where the
write tools live. That keeps a request arriving from an application assistant
(whose context may contain user data) from silently changing structure.

Only the primary assistant and application assistants can talk to each other,
never two application assistants: an application's data must stay inside that
application.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ValidationFailure
from .tools.registry import ToolRegistry, object_schema

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..application.conversations import ConversationStore
    from ..application.service import ApplicationService
    from .factory import AgentFactory, PreparedAgent

logger = logging.getLogger(__name__)

#: A relayed request is a side task: give the other assistant a bounded window.
MESSAGE_TIMEOUT_SECONDS = 90.0

PRIMARY_LABEL = "主助手"


@dataclass(slots=True)
class OutgoingMessage:
    """One message queued by a tool call, delivered after the turn ends."""

    target: str  # "primary" 或 application_id
    target_name: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "target_name": self.target_name,
            "text": self.text,
        }


def primary_message_tool(
    registry: ToolRegistry, *, target_name: str = PRIMARY_LABEL
) -> None:
    """应用助手 → 主助手。"""

    def handler(args: dict[str, Any]) -> Any:
        text = str(args.get("message") or "").strip()
        if not text:
            raise ValidationFailure("消息不能为空")
        registry.outbox.append(
            OutgoingMessage(target="primary", target_name=target_name, text=text)
        )
        return {
            "queued": True,
            "to": target_name,
            "note": (
                f"已经转达{target_name}，答复会以折叠卡片出现在这个对话里。"
                "先用一句话告诉用户已转达；不要自己编造对方的答复。"
            ),
        }

    registry.register(
        "send_message_to_primary",
        "把用户关于**应用本身**的诉求（加字段、改页面、加操作、改这个助手的设定）"
        "转给主助手。主助手只做结构设计并给出方案，真正的改动要用户在「主助手」对话里确认。"
        "业务数据（录入、查询、统计）自己处理，不要转给主助手。",
        object_schema(
            {
                "message": {
                    "type": "string",
                    "description": "要转达的内容：用户的诉求原话 + 你补充的必要背景",
                }
            },
            ["message"],
        ),
        handler,
    )


def application_message_tool(
    registry: ToolRegistry, service: ApplicationService
) -> None:
    """主助手 → 某个应用助手。"""

    def handler(args: dict[str, Any]) -> Any:
        application_id = str(args.get("application_id") or "").strip()
        if not application_id:
            raise ValidationFailure("application_id 不能为空")
        text = str(args.get("message") or "").strip()
        if not text:
            raise ValidationFailure("消息不能为空")
        application = service.get_application(application_id)
        registry.outbox.append(
            OutgoingMessage(
                target=application.id, target_name=application.name, text=text
            )
        )
        return {
            "queued": True,
            "to": application.name,
            "note": (
                f"已经转达「{application.name}」的助手，答复会以折叠卡片出现在这个对话里。"
                "先告诉用户你在等对方回话，不要编造答复。"
            ),
        }

    registry.register(
        "send_message_to_application",
        "联系某个已有应用的专属助手，让它去查自己应用里的情况或执行它那边的操作。"
        "调用前先用 list_applications 拿到 application_id。",
        object_schema(
            {
                "application_id": {"type": "string", "description": "目标应用 ID"},
                "message": {
                    "type": "string",
                    "description": "要交给对方助手的事情，一次说清",
                },
            },
            ["application_id", "message"],
        ),
        handler,
    )


class AgentMessenger:
    """Runs one relayed turn and records both sides of the exchange."""

    def __init__(
        self,
        service: ApplicationService,
        conversations: ConversationStore,
        agents: AgentFactory,
    ) -> None:
        self.service = service
        self.conversations = conversations
        self.agents = agents

    async def deliver(
        self,
        outgoing: OutgoingMessage,
        *,
        sender: dict[str, Any],
        sender_conversation_id: str | None,
    ) -> dict[str, Any]:
        """Never raises: a broken relay must not hide the sender's answer."""
        peer = {
            "id": "",
            "name": outgoing.target_name,
            "application_id": outgoing.target if outgoing.target != "primary" else None,
        }
        try:
            prepared, peer = self._prepare(outgoing, sender)
            target_conversation = self.conversations.get_or_create(
                prepared.agent.id, prepared.agent.application_id
            )
            history = self.agents.history(target_conversation.id)
            # The card is the receiver's record of the request; it is stored as a
            # user turn so the receiver's own follow-up turns keep the context.
            self.conversations.append(
                target_conversation.id,
                "user",
                outgoing.text,
                {"kind": "agent_message", "direction": "in", "peer": sender},
            )
            reply, error = await self._run(prepared, outgoing, history)
            if reply:
                self.conversations.append(
                    target_conversation.id,
                    "assistant",
                    reply,
                    {"kind": "agent_message_reply", "peer": sender},
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("assistant message delivery failed")
            reply, error = None, f"转达失败: {exc}"
            target_conversation = None

        content = reply or f"没能联系上「{peer['name']}」：{error or '未知问题'}"
        outbound = self._store_outbound(
            sender_conversation_id,
            content=content,
            peer=peer,
            request=outgoing.text,
            reply=reply,
            error=error,
        )
        return {
            "ok": bool(reply),
            "reply": reply,
            "error": error,
            "peer": peer,
            "request": outgoing.text,
            "message_id": outbound["id"] if outbound else None,
            "conversation_id": target_conversation.id if target_conversation else None,
        }

    # ------------------------------------------------------------------
    def _store_outbound(
        self,
        conversation_id: str | None,
        *,
        content: str,
        peer: dict[str, Any],
        request: str,
        reply: str | None,
        error: str | None,
    ) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        try:
            return self.conversations.append(
                conversation_id,
                "assistant",
                content,
                {
                    "kind": "agent_message",
                    "direction": "out",
                    "peer": peer,
                    "request": request,
                    "reply": reply,
                    "error": error,
                },
            )
        except Exception:  # pragma: no cover - conversation may be gone
            logger.warning("could not record outbound message", exc_info=True)
            return None

    def _prepare(
        self, outgoing: OutgoingMessage, sender: dict[str, Any]
    ) -> tuple[PreparedAgent, dict[str, Any]]:
        if outgoing.target == "primary":
            prepared = self.agents.for_messaging(sender)
            return prepared, {
                "id": prepared.agent.id,
                "name": PRIMARY_LABEL,
                "application_id": None,
            }
        application = self.service.get_application(outgoing.target)
        prepared = self.agents.application(outgoing.target, with_messaging=False)
        return prepared, {
            "id": prepared.agent.id,
            "name": application.name,
            "application_id": application.id,
        }

    async def _run(
        self,
        prepared: PreparedAgent,
        outgoing: OutgoingMessage,
        history: list[dict[str, Any]],
    ) -> tuple[str | None, str | None]:
        chunks: list[str] = []
        error: str | None = None

        user_text = (
            outgoing.text
            if outgoing.target == "primary"
            else f"主助手转来一条消息：\n\n{outgoing.text}"
        )

        async def consume() -> None:
            nonlocal error
            async for event in prepared.runtime.stream(
                history=history, user_text=user_text
            ):
                kind = event.get("type")
                if kind == "assistant_message":
                    chunks.append(event.get("content") or "")
                elif kind == "error":
                    error = event.get("message") or "对方助手没有返回结果"

        try:
            await asyncio.wait_for(consume(), timeout=MESSAGE_TIMEOUT_SECONDS)
        except TimeoutError:
            error = f"等了 {int(MESSAGE_TIMEOUT_SECONDS)} 秒没有回复"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - provider surprises
            logger.exception("relayed assistant run failed")
            error = str(exc)

        text = "\n".join(part.strip() for part in chunks if part.strip()).strip()
        if text:
            return text, None
        return None, error or "对方助手没有回复内容"
