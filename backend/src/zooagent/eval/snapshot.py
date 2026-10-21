"""Structural snapshot of a generated application.

Everything the checks need, captured from the engines (not from the AI's text),
so the evaluation measures what the platform actually built.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..application.service import AppContext, ApplicationService
from ..errors import NotFoundError

RESERVED_FIELD_KEYS = {"id", "revision", "created_at", "updated_at", "deleted_at"}
#: How many records per model get a content digest (for evolution checks).
DIGEST_LIMIT = 300


@dataclass
class FieldSnapshot:
    key: str
    name: str
    type: str
    required: bool
    unique: bool
    has_default: bool
    option_count: int
    target_model: str | None
    scale: int | None = None


@dataclass
class ModelSnapshot:
    key: str
    name: str
    version: int
    title_field: str
    fields: list[FieldSnapshot] = field(default_factory=list)
    record_count: int = 0
    #: record id -> content digest, so evolution checks can prove data survived
    record_digests: dict[str, str] = field(default_factory=dict)

    def field(self, key: str) -> FieldSnapshot | None:
        return next((item for item in self.fields if item.key == key), None)


@dataclass
class ActionSnapshot:
    key: str
    name: str
    version: int
    input_count: int
    step_count: int
    referenced_models: list[str] = field(default_factory=list)
    read_only: bool = False


@dataclass
class ComponentSnapshot:
    type: str
    model: str | None
    fields: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PageSnapshot:
    key: str
    title: str
    components: list[ComponentSnapshot] = field(default_factory=list)


@dataclass
class TurnResult:
    """What one user message produced."""

    prompt: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_failures: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    duration_s: float = 0.0

    @property
    def failed(self) -> bool:
        return bool(self.error)


@dataclass
class AppSnapshot:
    application_id: str
    application_name: str
    models: list[ModelSnapshot] = field(default_factory=list)
    actions: list[ActionSnapshot] = field(default_factory=list)
    pages: list[PageSnapshot] = field(default_factory=list)
    turns: list[TurnResult] = field(default_factory=list)

    # ------------------------------------------------------------------
    # derived views used by the checks
    # ------------------------------------------------------------------
    @property
    def model_count(self) -> int:
        return len(self.models)

    @property
    def field_count(self) -> int:
        return sum(len(model.fields) for model in self.models)

    @property
    def relation_count(self) -> int:
        return sum(
            1
            for model in self.models
            for item in model.fields
            if item.type == "relation"
        )

    @property
    def record_count(self) -> int:
        return sum(model.record_count for model in self.models)

    @property
    def action_count(self) -> int:
        return len(self.actions)

    @property
    def action_step_count(self) -> int:
        return sum(action.step_count for action in self.actions)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def component_count(self) -> int:
        return sum(len(page.components) for page in self.pages)

    @property
    def data_table_count(self) -> int:
        return sum(
            1 for page in self.pages for component in page.components if component.type == "DataTable"
        )

    @property
    def tool_calls(self) -> int:
        return sum(len(turn.tool_calls) for turn in self.turns)

    @property
    def tool_failures(self) -> int:
        return sum(len(turn.tool_failures) for turn in self.turns)

    @property
    def all_fields(self) -> list[tuple[ModelSnapshot, FieldSnapshot]]:
        return [(model, item) for model in self.models for item in model.fields]

    @property
    def chinese_field_name_ratio(self) -> float:
        fields = [item for _, item in self.all_fields if item.key not in RESERVED_FIELD_KEYS]
        if not fields:
            return 0.0
        chinese = sum(1 for item in fields if any("\u4e00" <= char <= "\u9fff" for char in item.name))
        return chinese / len(fields)

    def model_keys(self) -> set[str]:
        return {model.key for model in self.models}

    def models_referenced_by_pages(self) -> set[str]:
        return {
            component.model
            for page in self.pages
            for component in page.components
            if component.model
        }

    def metrics(self) -> dict[str, Any]:
        return {
            "models": self.model_count,
            "fields": self.field_count,
            "relations": self.relation_count,
            "enums": sum(
                1 for _, item in self.all_fields if item.type == "enum"
            ),
            "actions": self.action_count,
            "action_steps": self.action_step_count,
            "pages": self.page_count,
            "components": self.component_count,
            "data_tables": self.data_table_count,
            "records": self.record_count,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "chinese_names": round(self.chinese_field_name_ratio, 2),
            "duration_s": round(sum(turn.duration_s for turn in self.turns), 1),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "application_name": self.application_name,
            "metrics": self.metrics(),
            "models": [
                {
                    "key": model.key,
                    "name": model.name,
                    "version": model.version,
                    "records": model.record_count,
                    "fields": [
                        {
                            "key": item.key,
                            "name": item.name,
                            "type": item.type,
                            "required": item.required,
                            "unique": item.unique,
                            "has_default": item.has_default,
                            "options": item.option_count,
                            "target_model": item.target_model,
                        }
                        for item in model.fields
                    ],
                }
                for model in self.models
            ],
            "actions": [
                {
                    "key": action.key,
                    "name": action.name,
                    "steps": action.step_count,
                    "models": action.referenced_models,
                }
                for action in self.actions
            ],
            "pages": [
                {
                    "key": page.key,
                    "title": page.title,
                    "components": [
                        {
                            "type": component.type,
                            "model": component.model,
                            "fields": component.fields,
                            "actions": component.actions,
                        }
                        for component in page.components
                    ],
                }
                for page in self.pages
            ],
            "turns": [
                {
                    "prompt": turn.prompt,
                    "tool_calls": [item["name"] for item in turn.tool_calls],
                    "tool_failures": turn.tool_failures,
                    "final_text": turn.final_text,
                    "error": turn.error,
                    "duration_s": round(turn.duration_s, 2),
                }
                for turn in self.turns
            ],
        }


def _count_records(conn: sqlite3.Connection, model_id: str | None) -> int:
    if not model_id:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM records WHERE model_id = ? AND deleted_at IS NULL",
        (model_id,),
    ).fetchone()
    return int(row["total"])


def _record_digests(conn: sqlite3.Connection, model_id: str | None) -> dict[str, str]:
    """Bounded id -> content digest map, used to prove data survived an upgrade."""
    if not model_id:
        return {}
    rows = conn.execute(
        """
        SELECT id, data_json FROM records
        WHERE model_id = ? AND deleted_at IS NULL
        ORDER BY created_at LIMIT ?
        """,
        (model_id, DIGEST_LIMIT),
    ).fetchall()
    return {
        row["id"]: hashlib.sha1(row["data_json"].encode("utf-8")).hexdigest()[:12]
        for row in rows
    }


def capture_snapshot(
    service: ApplicationService,
    application_id: str,
    *,
    turns: list[TurnResult] | None = None,
) -> AppSnapshot:
    """Read the current structure of one application out of the engines."""
    with service.open_app(application_id) as ctx:
        return _snapshot_from_context(ctx, turns or [], service)


def _snapshot_from_context(
    ctx: AppContext, turns: list[TurnResult], service: ApplicationService
) -> AppSnapshot:
    models: list[ModelSnapshot] = []
    for model in ctx.models.list_models(ctx.conn):
        snapshot = ModelSnapshot(
            key=model.key,
            name=model.name,
            version=model.version,
            title_field=model.effective_title_field,
            record_count=_count_records(ctx.conn, model.id),
            record_digests=_record_digests(ctx.conn, model.id),
        )
        for item in model.fields:
            snapshot.fields.append(
                FieldSnapshot(
                    key=item.key,
                    name=item.name,
                    type=item.type,
                    required=item.required,
                    unique=item.unique,
                    has_default=item.default is not None,
                    option_count=len(item.option_values),
                    target_model=item.target_model,
                    scale=item.scale,
                )
            )
        models.append(snapshot)

    actions: list[ActionSnapshot] = []
    for action in ctx.actions.list_actions(ctx.conn):
        referenced: list[str] = []
        for step in action.steps:
            if step.model and step.model not in referenced:
                referenced.append(step.model)
        actions.append(
            ActionSnapshot(
                key=action.key,
                name=action.name,
                version=action.version,
                input_count=len(action.inputs),
                step_count=len(action.steps),
                referenced_models=referenced,
                read_only=action.read_only,
            )
        )

    pages = [
        PageSnapshot(
            key=page.key,
            title=page.title,
            components=[
                ComponentSnapshot(
                    type=component.type,
                    model=component.model,
                    fields=list(component.fields or []),
                    actions=[
                        action.model_dump() for action in (component.actions or [])
                    ],
                )
                for component in page.components
            ],
        )
        for page in ctx.ui.load(ctx.conn).pages
    ]

    return AppSnapshot(
        application_id=ctx.application.id,
        application_name=ctx.application.name,
        models=models,
        actions=actions,
        pages=pages,
        turns=turns,
    )


def _body_count(ctx: AppContext) -> int:  # pragma: no cover - helper for diagnostics
    try:
        return len(ctx.models.list_models(ctx.conn))
    except NotFoundError:
        return 0


def dumps(snapshot: AppSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2)


__all__ = [
    "ActionSnapshot",
    "AppSnapshot",
    "ComponentSnapshot",
    "FieldSnapshot",
    "ModelSnapshot",
    "PageSnapshot",
    "TurnResult",
    "capture_snapshot",
    "dumps",
]
