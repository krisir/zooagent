"""Action Engine: execute declarative business operations deterministically.

An Action is composed once (by the AI) and then runs like normal software:
from a button, from an app agent tool, or from a schedule. No LLM is involved
in execution.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from ..data.engine import DataEngine
from ..data.query import AggregateSpec, Aggregation, Filter, QuerySpec, SortSpec
from ..errors import ActionExecutionError, NotFoundError, ValidationFailure
from ..ids import new_id, utc_now
from ..models.engine import ModelEngine
from ..models.validation import coerce_record_values
from ..persistence import atomic
from .expression import resolve_value
from .schema import ActionDef, ActionStep
from .validation import validate_action


def _validate(model_class: type, payload: Any, label: str) -> Any:
    try:
        return model_class.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailure(f"{label} 定义不合法", details=exc.errors()) from exc


class ActionEngine:
    def __init__(self, models: ModelEngine, data: DataEngine) -> None:
        self.models = models
        self.data = data

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------
    def list_actions(
        self, conn: sqlite3.Connection, *, include_deleted: bool = False
    ) -> list[ActionDef]:
        sql = """
            SELECT a.active_version, v.schema_json
            FROM actions a
            JOIN action_versions v
              ON v.action_id = a.id AND v.version = a.active_version
        """
        if not include_deleted:
            sql += " WHERE a.status = 'active'"
        sql += " ORDER BY a.name COLLATE NOCASE"
        return [ActionDef.model_validate(json.loads(row["schema_json"])) for row in conn.execute(sql)]

    def get_action_optional(
        self, conn: sqlite3.Connection, ref: str
    ) -> ActionDef | None:
        row = conn.execute(
            """
            SELECT a.id, a.action_key, a.name, a.description, a.active_version,
                   a.status, v.schema_json
            FROM actions a
            JOIN action_versions v
              ON v.action_id = a.id AND v.version = a.active_version
            WHERE (a.id = ? OR a.action_key = ?) AND a.status = 'active'
            """,
            (ref, ref),
        ).fetchone()
        if row is None:
            return None
        action = ActionDef.model_validate(json.loads(row["schema_json"]))
        action.id = row["id"]
        return action

    def get_action(self, conn: sqlite3.Connection, ref: str) -> ActionDef:
        action = self.get_action_optional(conn, ref)
        if action is None:
            raise NotFoundError(f"Action '{ref}' 不存在")
        return action

    def create_action(
        self, conn: sqlite3.Connection, spec: ActionDef
    ) -> ActionDef:
        exists = conn.execute(
            "SELECT id FROM actions WHERE action_key = ?", (spec.key,)
        ).fetchone()
        if exists is not None:
            raise ValidationFailure(f"Action '{spec.key}' 已存在，请使用更新")
        validate_action(spec)
        action = spec.model_copy(deep=True)
        action.id = new_id("act")
        action.version = 1
        now = utc_now()
        with atomic(conn):
            conn.execute(
                """
                INSERT INTO actions(id, action_key, name, description, active_version,
                                    status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, 'active', ?, ?)
                """,
                (action.id, action.key, action.name, action.description, now, now),
            )
            conn.execute(
                """
                INSERT INTO action_versions(action_id, version, schema_json, created_at)
                VALUES (?, 1, ?, ?)
                """,
                (action.id, json.dumps(action.model_dump(), ensure_ascii=False), now),
            )
        return action

    def update_action(
        self, conn: sqlite3.Connection, ref: str, spec: ActionDef
    ) -> ActionDef:
        existing = self.get_action(conn, ref)
        if spec.key != existing.key:
            raise ValidationFailure("Action key 不能修改")
        validate_action(spec)
        action = spec.model_copy(deep=True)
        action.id = existing.id
        action.version = existing.version + 1
        previous_fields = {field.key: field for field in existing.inputs}
        for field in action.inputs:
            if field.key in previous_fields and not field.id:
                field.id = previous_fields[field.key].id
        now = utc_now()
        with atomic(conn):
            conn.execute(
                "UPDATE actions SET name = ?, description = ?, active_version = ?, updated_at = ?"
                " WHERE id = ?",
                (action.name, action.description, action.version, now, action.id),
            )
            conn.execute(
                """
                INSERT INTO action_versions(action_id, version, schema_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    action.id,
                    action.version,
                    json.dumps(action.model_dump(), ensure_ascii=False),
                    now,
                ),
            )
        return action

    def delete_action(self, conn: sqlite3.Connection, ref: str) -> None:
        action = self.get_action(conn, ref)
        with atomic(conn):
            conn.execute(
                "UPDATE actions SET status = 'deleted', updated_at = ? WHERE id = ?",
                (utc_now(), action.id),
            )

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def run(
        self,
        conn: sqlite3.Connection,
        action: ActionDef,
        inputs: dict[str, Any] | None = None,
        *,
        actor: str = "user",
        app_name: str = "",
        application_id: str = "",
    ) -> dict[str, Any]:
        inputs = inputs or {}
        trace: list[dict[str, Any]] = []
        effects: list[dict[str, Any]] = []
        output: dict[str, Any] | None = None
        failed_step: ActionStep | None = None
        failed_index: int | None = None

        try:
            if action.inputs:
                validated = coerce_record_values(action.input_model(), inputs)
            else:
                if inputs:
                    raise ValidationFailure(f"Action '{action.name}' 不接受参数")
                validated = {}

            context: dict[str, Any] = {
                "input": validated,
                "steps": {},
                "now": utc_now(),
                "app": {"name": app_name},
            }

            # One transaction for the whole Action: a failure in step N must undo
            # the writes of steps 1..N-1. Step-level `atomic()` blocks become nested
            # savepoints, so they still protect a single record write on their own.
            with atomic(conn):
                for index, step in enumerate(action.steps):
                    failed_step, failed_index = step, index
                    resolved = self._run_step(
                        conn, action, step, context, trace, effects, actor
                    )
                    if step.type == "return":
                        output = resolved
                        break

                # Resolve the final output inside the transaction too: a broken
                # output reference must undo the writes, not report failure after
                # the data has already been committed.
                if output is None:
                    if action.output:
                        output = resolve_value(action.output, context)
                    else:
                        last_output = action.steps[-1].output
                        output = (
                            context["steps"].get(last_output)
                            if last_output
                            else context["steps"]
                        )
        except ActionExecutionError:
            raise
        except Exception as exc:
            raise ActionExecutionError(
                _failure_message(action, failed_step, exc),
                action=action,
                inputs=inputs,
                step=failed_step,
                step_index=failed_index,
                trace=trace,
                effects=effects,
                app_name=app_name,
                application_id=application_id,
                original=exc,
            ) from exc

        return {"output": output, "trace": trace, "effects": effects}

    def _run_step(
        self,
        conn: sqlite3.Connection,
        action: ActionDef,
        step: ActionStep,
        context: dict[str, Any],
        trace: list[dict[str, Any]],
        effects: list[dict[str, Any]],
        actor: str,
    ) -> Any:
        entry: dict[str, Any] = {"id": step.id, "type": step.type}
        trace.append(entry)
        try:
            result = self._dispatch(conn, action, step, context, effects, actor)
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            entry["error_code"] = getattr(exc, "code", type(exc).__name__)
            raise
        entry["status"] = "ok"
        if step.output:
            context["steps"][step.output] = result
            entry["output"] = step.output
        entry["preview"] = _preview(result)
        return result

    def _dispatch(
        self,
        conn: sqlite3.Connection,
        action: ActionDef,
        step: ActionStep,
        context: dict[str, Any],
        effects: list[dict[str, Any]],
        actor: str,
    ) -> Any:
        if step.type in {"query", "aggregate", "create", "update", "delete"}:
            if not step.model:
                raise ValidationFailure(f"步骤 {step.id} 缺少 model")
            model = self.models.get_model(conn, step.model)

        if step.type == "query":
            spec = QuerySpec(
                filters=_filters(step, context),
                filter_logic=step.filter_logic,
                sort=[_sort(item) for item in step.sort],
                limit=step.limit or 50,
                expand=False,
            )
            return self.data.query_records(conn, model, spec)

        if step.type == "aggregate":
            spec = AggregateSpec(
                filters=_filters(step, context),
                filter_logic=step.filter_logic,
                group_by=step.group_by,
                aggregations=[_aggregation(item) for item in step.aggregations],
                limit=step.limit or 500,
            )
            return self.data.aggregate(conn, model, spec)

        if step.type == "create":
            values = resolve_value(step.values, context)
            record = self.data.create_record(conn, model, values, actor=actor)
            effects.append(
                {
                    "type": "created",
                    "model": model.key,
                    "record_id": record["id"],
                    "title": record["title"],
                }
            )
            return record

        if step.type == "update":
            record_id = resolve_value(step.record_id, context)
            values = resolve_value(step.values, context)
            expected = (
                resolve_value(step.expected_revision, context)
                if step.expected_revision is not None
                else None
            )
            record = self.data.update_record(
                conn,
                model,
                str(record_id),
                values,
                expected_revision=int(expected) if expected is not None else None,
                actor=actor,
            )
            effects.append(
                {
                    "type": "updated",
                    "model": model.key,
                    "record_id": record["id"],
                    "title": record["title"],
                }
            )
            return record

        if step.type == "delete":
            record_id = str(resolve_value(step.record_id, context))
            self.data.delete_record(conn, model, record_id, actor=actor)
            effects.append(
                {"type": "deleted", "model": model.key, "record_id": record_id}
            )
            return {"id": record_id, "deleted": True}

        if step.type == "compute":
            from .expression import evaluate

            return evaluate(step.expression or "", context)

        if step.type == "return":
            return resolve_value(step.value or {}, context)

        raise ValidationFailure(f"未知步骤类型 {step.type}")


def _failure_message(
    action: ActionDef, step: ActionStep | None, exc: BaseException
) -> str:
    if step is None:
        return f"Action '{action.name}' 的输入不合法: {exc}"
    return (
        f"Action '{action.name}' 执行失败"
        f"（步骤 {step.id or step.type} / {step.type}）: {exc}"
    )


def _filters(step: ActionStep, context: dict[str, Any]) -> list[Filter]:
    result: list[Filter] = []
    for item in step.filters:
        resolved = resolve_value(item.value, context)
        try:
            result.append(
                Filter(field=item.field, operator=item.operator, value=resolved)  # type: ignore[arg-type]
            )
        except ValidationError as exc:
            raise ValidationFailure(
                f"筛选条件不合法: {item.field} {item.operator}", details=exc.errors()
            ) from exc
    return result


def _sort(item: dict[str, Any]) -> SortSpec:
    try:
        return SortSpec(field=item["field"], direction=item.get("direction", "asc"))
    except (KeyError, ValidationError) as exc:
        raise ValidationFailure(f"排序定义不合法: {item}") from exc


def _aggregation(item: dict[str, Any]) -> Aggregation:
    try:
        return Aggregation.model_validate(item)
    except ValidationError as exc:
        raise ValidationFailure(f"聚合定义不合法: {item}", details=exc.errors()) from exc


def _preview(result: Any) -> Any:
    if isinstance(result, dict):
        if "items" in result and isinstance(result["items"], list):
            return {
                "total": result.get("total", len(result["items"])),
                "sample": result["items"][:3],
            }
        if "id" in result:
            return {"id": result["id"], "title": result.get("title")}
        return {key: result[key] for key in list(result)[:6]}
    if isinstance(result, list):
        return result[:3]
    if result is None or isinstance(result, (str, int, float, bool)):
        return result
    return str(result)
