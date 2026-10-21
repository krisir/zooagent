"""Conversation + message persistence (system database)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..config import WorkspacePaths
from ..errors import NotFoundError
from ..ids import new_id, utc_now
from ..persistence.sqlite import connect

MAX_HISTORY_MESSAGES = 40


@dataclass
class Conversation:
    id: str
    agent_id: str
    application_id: str | None
    title: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Conversation:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            application_id=row["application_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "application_id": self.application_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _message_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        "created_at": row["created_at"],
    }


class ConversationStore:
    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.paths.system_db)
        try:
            yield conn
        finally:
            conn.close()

    def get_or_create(
        self,
        agent_id: str,
        application_id: str | None = None,
        *,
        title: str | None = None,
    ) -> Conversation:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM conversations WHERE agent_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
            if row is not None:
                return Conversation.from_row(row)
            return self._create(conn, agent_id, application_id, title)

    def create(
        self,
        agent_id: str,
        application_id: str | None = None,
        *,
        title: str | None = None,
    ) -> Conversation:
        with self._conn() as conn:
            return self._create(conn, agent_id, application_id, title)

    def _create(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        application_id: str | None,
        title: str | None,
    ) -> Conversation:
        conversation_id = new_id("conv")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO conversations(id, agent_id, application_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, agent_id, application_id, title, now, now),
        )
        conn.commit()
        return Conversation(
            id=conversation_id,
            agent_id=agent_id,
            application_id=application_id,
            title=title,
            created_at=now,
            updated_at=now,
        )

    def get(self, conversation_id: str) -> Conversation:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"会话 '{conversation_id}' 不存在")
        return Conversation.from_row(row)

    def list_for_agent(self, agent_id: str) -> list[Conversation]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE agent_id = ? ORDER BY updated_at DESC",
                (agent_id,),
            ).fetchall()
        return [Conversation.from_row(row) for row in rows]

    def rename(self, conversation_id: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, utc_now(), conversation_id),
            )
            conn.commit()

    def append(
        self,
        conversation_id: str,
        role: str,
        content: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = new_id("msg")
        now = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO messages(id, conversation_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    role,
                    content,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                    now,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            conn.commit()
        return {
            "id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata": metadata,
            "created_at": now,
        }

    def history(
        self, conversation_id: str, limit: int = MAX_HISTORY_MESSAGES
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE conversation_id = ?
                    ORDER BY created_at DESC LIMIT ?
                ) ORDER BY created_at ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        return [_message_to_dict(row) for row in rows]

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
        return [_message_to_dict(row) for row in rows]
