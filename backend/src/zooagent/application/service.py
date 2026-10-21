"""Application registry and per-application workspace lifecycle.

Each Application gets its own directory and its own SQLite database. Deleting
an app can therefore never damage another app's business data, and exporting an
app is just copying a folder.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..actions.engine import ActionEngine
from ..config import WorkspacePaths
from ..data.engine import DataEngine
from ..errors import ConflictError, NotFoundError, ValidationFailure
from ..ids import new_id, utc_now
from ..models.engine import ModelEngine
from ..persistence.sqlite import connect, init_application_db
from ..ui_schema.service import UISchemaService

PRIMARY_AGENT_ID = "agent_primary"

PRIMARY_INSTRUCTIONS = """你是 zooagent 主助手。你帮助用户用自然语言创建、使用和演化个人业务应用。
你通过工具操作平台，绝不输出 SQL，也不假设自己拥有文件系统或网络权限。
创建应用时按顺序执行：create_application -> define_model（先被引用的模型）->
define_action -> set_pages -> seed_records（可选）-> 用中文向用户总结。"""


@dataclass
class Application:
    id: str
    name: str
    description: str | None
    icon: str | None
    color: str | None
    status: str
    active_version: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Application:
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            icon=row["icon"],
            color=row["color"],
            status=row["status"],
            active_version=row["active_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "status": self.status,
            "active_version": self.active_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Agent:
    id: str
    application_id: str | None
    name: str
    role: str
    instructions: str | None
    greeting: str | None
    model_profile_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Agent:
        return cls(
            id=row["id"],
            application_id=row["application_id"],
            name=row["name"],
            role=row["role"],
            instructions=row["instructions"],
            greeting=row["greeting"],
            model_profile_id=row["model_profile_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "application_id": self.application_id,
            "name": self.name,
            "role": self.role,
            "instructions": self.instructions,
            "greeting": self.greeting,
            "model_profile_id": self.model_profile_id,
        }


@dataclass
class AppContext:
    application: Application
    agent: Agent | None
    conn: sqlite3.Connection
    models: ModelEngine
    data: DataEngine
    actions: ActionEngine
    ui: UISchemaService


class ApplicationService:
    def __init__(
        self,
        paths: WorkspacePaths,
        models: ModelEngine,
        data: DataEngine,
        actions: ActionEngine,
        ui: UISchemaService,
    ) -> None:
        self.paths = paths
        self.models = models
        self.data = data
        self.actions = actions
        self.ui = ui

    # ------------------------------------------------------------------
    # system db
    # ------------------------------------------------------------------
    @contextmanager
    def system_conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.paths.system_db)
        try:
            yield conn
        finally:
            conn.close()

    def ensure_primary_agent(self) -> Agent:
        with self.system_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE role = 'primary' LIMIT 1"
            ).fetchone()
            if row is not None:
                return Agent.from_row(row)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO agents(id, application_id, name, role, instructions,
                                   greeting, created_at, updated_at)
                VALUES (?, NULL, ?, 'primary', ?, ?, ?, ?)
                """,
                (
                    PRIMARY_AGENT_ID,
                    "zooagent",
                    PRIMARY_INSTRUCTIONS,
                    "告诉我你想管理什么，我来为你搭一个专属的业务应用。",
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (PRIMARY_AGENT_ID,)
            ).fetchone()
            return Agent.from_row(row)

    def get_primary_agent(self) -> Agent:
        with self.system_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE role = 'primary' LIMIT 1"
            ).fetchone()
        if row is None:
            return self.ensure_primary_agent()
        return Agent.from_row(row)

    def get_agent(self, application_id: str) -> Agent | None:
        with self.system_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM agents WHERE application_id = ?
                ORDER BY created_at LIMIT 1
                """,
                (application_id,),
            ).fetchone()
        return Agent.from_row(row) if row else None

    def update_agent(self, agent_id: str, patch: dict[str, Any]) -> Agent:
        allowed = {"name", "instructions", "greeting", "model_profile_id"}
        updates = {key: value for key, value in patch.items() if key in allowed}
        if not updates:
            raise ValidationFailure("没有可更新的字段")
        with self.system_conn() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"助手 '{agent_id}' 不存在")
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE agents SET {assignments}, updated_at = ? WHERE id = ?",
                [*updates.values(), utc_now(), agent_id],
            )
            conn.commit()
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return Agent.from_row(row)

    # ------------------------------------------------------------------
    # applications
    # ------------------------------------------------------------------
    def list_applications(self) -> list[Application]:
        with self.system_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status != 'deleted' ORDER BY created_at"
            ).fetchall()
        return [Application.from_row(row) for row in rows]

    def get_application(self, application_id: str) -> Application:
        with self.system_conn() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE id = ? AND status != 'deleted'",
                (application_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"应用 '{application_id}' 不存在")
        return Application.from_row(row)

    def create_application(
        self,
        name: str,
        description: str | None = None,
        *,
        icon: str | None = None,
        color: str | None = None,
        agent_name: str | None = None,
        agent_instructions: str | None = None,
    ) -> Application:
        name = (name or "").strip()
        if not name:
            raise ValidationFailure("应用名称不能为空")
        application_id = new_id("app")
        directory = self.paths.application_dir(application_id)
        for sub in ("attachments", "backups", "snapshots"):
            (directory / sub).mkdir(parents=True, exist_ok=True)
        init_application_db(self.paths.application_db(application_id))

        now = utc_now()
        application = Application(
            id=application_id,
            name=name,
            description=description,
            icon=icon,
            color=color,
            status="active",
            active_version=1,
            created_at=now,
            updated_at=now,
        )
        agent_id = new_id("agent")
        with self.system_conn() as conn:
            conn.execute(
                """
                INSERT INTO applications(id, name, description, icon, color, status,
                                         active_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)
                """,
                (application_id, name, description, icon, color, now, now),
            )
            conn.execute(
                """
                INSERT INTO agents(id, application_id, name, role, instructions,
                                   greeting, created_at, updated_at)
                VALUES (?, ?, ?, 'application', ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    application_id,
                    agent_name or f"{name} 助手",
                    agent_instructions
                    or f"你是「{name}」的专属业务助手，帮助用户录入、查询和分析应用中的数据。",
                    "你可以直接告诉我发生了什么，或问我数据问题。",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_logs(id, application_id, actor_type, actor_id, action,
                                       target_type, target_id, after_json, created_at)
                VALUES (?, ?, 'primary_agent', ?, 'application.created', 'application',
                        ?, ?, ?)
                """,
                (
                    new_id("log"),
                    application_id,
                    PRIMARY_AGENT_ID,
                    application_id,
                    json.dumps(application.to_dict(), ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()

        self._write_manifest(application_id)
        return application

    def update_application(self, application_id: str, patch: dict[str, Any]) -> Application:
        application = self.get_application(application_id)
        allowed = {"name", "description", "icon", "color", "status"}
        updates = {key: value for key, value in patch.items() if key in allowed}
        if "name" in updates and not str(updates["name"]).strip():
            raise ValidationFailure("应用名称不能为空")
        if not updates:
            return application
        with self.system_conn() as conn:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE applications SET {assignments}, updated_at = ? WHERE id = ?",
                [*updates.values(), utc_now(), application_id],
            )
            conn.commit()
        self._write_manifest(application_id)
        return self.get_application(application_id)

    def delete_application(self, application_id: str) -> None:
        self.get_application(application_id)
        with self.system_conn() as conn:
            conn.execute(
                "UPDATE applications SET status = 'deleted', updated_at = ? WHERE id = ?",
                (utc_now(), application_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # app db
    # ------------------------------------------------------------------
    @contextmanager
    def app_conn(self, application_id: str) -> Iterator[sqlite3.Connection]:
        self.get_application(application_id)
        conn = connect(self.paths.application_db(application_id))
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def open_app(self, application_id: str) -> Iterator[AppContext]:
        application = self.get_application(application_id)
        agent = self.get_agent(application_id)
        conn = connect(self.paths.application_db(application_id))
        try:
            yield AppContext(
                application=application,
                agent=agent,
                conn=conn,
                models=self.models,
                data=self.data,
                actions=self.actions,
                ui=self.ui,
            )
        finally:
            conn.close()

    def bump_version(self, application_id: str) -> int:
        with self.system_conn() as conn:
            row = conn.execute(
                "SELECT active_version FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"应用 '{application_id}' 不存在")
            version = row["active_version"] + 1
            conn.execute(
                "UPDATE applications SET active_version = ?, updated_at = ? WHERE id = ?",
                (version, utc_now(), application_id),
            )
            conn.commit()
        return version

    def refresh_manifest(self, application_id: str) -> None:
        """Re-write the human-readable snapshot after a structural change."""
        self._write_manifest(application_id)

    def _write_manifest(self, application_id: str) -> None:
        """Human-readable snapshot next to the database, useful for export/debug."""
        application = self.get_application(application_id)
        manifest: dict[str, Any] = {"application": application.to_dict()}
        try:
            with self.open_app(application_id) as ctx:
                manifest["models"] = [
                    model.to_prompt_dict() for model in ctx.models.list_models(ctx.conn)
                ]
                manifest["actions"] = [
                    action.to_prompt_dict() for action in ctx.actions.list_actions(ctx.conn)
                ]
                manifest["ui"] = ctx.ui.load(ctx.conn).model_dump()
        except (NotFoundError, ConflictError):  # pragma: no cover - defensive
            pass
        path = self.paths.application_config(application_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
