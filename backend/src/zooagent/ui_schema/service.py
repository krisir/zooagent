"""Persist and validate UI schemas against the app's models and actions."""

from __future__ import annotations

import json
import sqlite3

from ..actions.engine import ActionEngine
from ..errors import ValidationFailure
from ..ids import utc_now
from ..models.engine import ModelEngine
from ..models.schema import ModelDef
from ..persistence import atomic
from .schema import UIComponent, UIPage, UISchema

DATA_COMPONENTS = {"DataTable", "Form", "DetailView", "StatCard", "Chart", "Calendar", "FilterPanel"}
FIELD_COMPONENTS = {"DataTable", "Form", "DetailView", "FilterPanel"}


class UISchemaService:
    def __init__(self, models: ModelEngine, actions: ActionEngine) -> None:
        self.models = models
        self.actions = actions

    def load(self, conn: sqlite3.Connection) -> UISchema:
        rows = conn.execute(
            """
            SELECT schema_json FROM ui_pages
            WHERE status = 'active'
            ORDER BY sort_order, title
            """
        ).fetchall()
        pages = [UIPage.model_validate(json.loads(row["schema_json"])) for row in rows]
        home = conn.execute("SELECT value FROM app_meta WHERE key = 'home_page'").fetchone()
        return UISchema(pages=pages, home_page=home["value"] if home else None)

    def replace(self, conn: sqlite3.Connection, schema: UISchema) -> UISchema:
        """Atomically replace every page after validating every reference.

        Validation happens here - at write time - so a user never opens a page
        that crashes because the AI referenced a model or field that does not
        exist.
        """
        self.validate(conn, schema)

        with atomic(conn):
            conn.execute("DELETE FROM ui_pages")
            now = utc_now()
            for order, page in enumerate(schema.pages):
                conn.execute(
                    """
                    INSERT INTO ui_pages(id, page_key, title, icon, schema_json,
                                         sort_order, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        page.id,
                        page.key,
                        page.title,
                        page.icon,
                        json.dumps(page.model_dump(), ensure_ascii=False),
                        order,
                        now,
                    ),
                )
            home = schema.home_page or (schema.pages[0].key if schema.pages else None)
            if home:
                conn.execute(
                    """
                    INSERT INTO app_meta(key, value) VALUES ('home_page', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (home,),
                )
        return schema

    def validate(self, conn: sqlite3.Connection, schema: UISchema) -> None:
        models = {model.key: model for model in self.models.list_models(conn)}
        action_keys = {action.key for action in self.actions.list_actions(conn)}
        page_keys = {page.key for page in schema.pages}
        problems: list[str] = []

        for page in schema.pages:
            for component in page.components:
                problems.extend(
                    self._check_component(component, models, action_keys, page_keys, page.key)
                )
        if problems:
            raise ValidationFailure("界面定义引用了不存在的模型、字段或 Action", details=problems)

    def _check_component(
        self,
        component: UIComponent,
        models: dict[str, ModelDef],
        action_keys: set[str],
        page_keys: set[str],
        page_key: str,
    ) -> list[str]:
        problems: list[str] = []
        prefix = f"页面 '{page_key}' 的 {component.type}"

        model: ModelDef | None = None
        if component.type in DATA_COMPONENTS:
            if not component.model:
                problems.append(f"{prefix} 缺少 model")
            elif component.model not in models:
                problems.append(f"{prefix} 引用的模型 '{component.model}' 不存在")
            else:
                model = models[component.model]

        if model is not None and component.type in FIELD_COMPONENTS:
            referenced: list[str] = list(component.fields or [])
            for column in component.columns or []:
                if isinstance(column, dict) and column.get("field"):
                    referenced.append(str(column["field"]))
            for field_key in referenced:
                if model.field(field_key) is None:
                    problems.append(
                        f"{prefix} 引用了模型 '{model.key}' 中不存在的字段 '{field_key}'"
                    )

        if model is not None and component.type in {"StatCard", "Chart", "Calendar"}:
            # The aggregate lives under props.aggregate, so checking only
            # props.field let `avg` on a non-existent field through - the card
            # then 422s on every render and silently shows 0.
            aggregate = component.props.get("aggregate")
            aggregate_field = (
                aggregate.get("field") if isinstance(aggregate, dict) else None
            )
            referenced = [aggregate_field] + list(component.props.get("group_by") or [])
            date_field = component.props.get("date_field")
            if date_field:
                referenced.append(date_field)
            title_field = component.props.get("title_field")
            if title_field:
                referenced.append(title_field)
            for item in component.props.get("filters") or []:
                if isinstance(item, dict) and item.get("field"):
                    referenced.append(item["field"])
            for field_key in referenced:
                if field_key and model.field(str(field_key)) is None:
                    problems.append(
                        f"{prefix} 引用了模型 '{model.key}' 中不存在的字段 '{field_key}'"
                    )

        for action in component.actions:
            if action.kind == "run_action":
                if not action.action:
                    problems.append(f"{prefix} 的 ActionButton 缺少 action")
                elif action.action not in action_keys:
                    problems.append(f"{prefix} 引用的 Action '{action.action}' 不存在")
            if action.kind == "open_page" and action.page not in page_keys:
                problems.append(f"{prefix} 引用的页面 '{action.page}' 不存在")
        return problems
