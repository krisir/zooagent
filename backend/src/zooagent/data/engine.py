"""Data Engine: the only path that writes business records.

Every mutation validates against the active model version, enforces unique
keys and relation integrity, bumps ``revision`` for optimistic concurrency,
and appends to ``record_history``. Transactions are owned by the caller so an
Action can group several writes atomically.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import ConflictError, NotFoundError, ValidationFailure
from ..ids import new_id, utc_now
from ..models.engine import ModelEngine
from ..models.schema import ModelDef
from ..models.validation import coerce_record_values, normalize_unique_value
from ..persistence import atomic
from .query import (
    AggregateSpec,
    QuerySpec,
    build_aggregation,
    build_order_by,
    build_where,
    field_expr,
    group_expr,
    title_of,
)


class DataEngine:
    def __init__(self, model_engine: ModelEngine) -> None:
        self.models = model_engine

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get_record(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        record_id: str,
        *,
        include_deleted: bool = False,
        expand: bool = True,
    ) -> dict[str, Any]:
        sql = "SELECT * FROM records WHERE id = ? AND model_id = ?"
        params: list[Any] = [record_id, model.id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError(f"记录 '{record_id}' 不存在")
        return self._serialize(conn, model, row, expand=expand)

    def query_records(
        self, conn: sqlite3.Connection, model: ModelDef, spec: QuerySpec
    ) -> dict[str, Any]:
        where, params = build_where(
            model,
            spec.filters,
            filter_logic=spec.filter_logic,
            include_deleted=spec.include_deleted,
        )
        total = conn.execute(
            f"SELECT COUNT(*) AS total FROM records WHERE {where}", params
        ).fetchone()["total"]

        order = build_order_by(model, spec.sort)
        rows = conn.execute(
            f"SELECT * FROM records WHERE {where} {order} LIMIT ? OFFSET ?",
            [*params, spec.limit, spec.offset],
        ).fetchall()

        return {
            "items": [
                self._serialize(conn, model, row, expand=spec.expand) for row in rows
            ],
            "total": total,
            "limit": spec.limit,
            "offset": spec.offset,
            "model": model.key,
        }

    def aggregate(
        self, conn: sqlite3.Connection, model: ModelDef, spec: AggregateSpec
    ) -> dict[str, Any]:
        aggregations = spec.aggregations or []
        if not aggregations:
            from .query import Aggregation

            aggregations = [Aggregation(function="count")]

        select_parts: list[str] = []
        group_aliases: list[str] = []
        for index, key in enumerate(spec.group_by):
            if spec.granularity:
                expr, field = group_expr(model, key, spec.granularity)
                if field is None or field.type not in {"date", "datetime"}:
                    raise ValidationFailure(
                        f"granularity 只能用于日期字段，'{key}' 不是日期字段"
                    )
            else:
                expr, field = group_expr(model, key, None)
            alias = f"group_{index}"
            select_parts.append(f"{expr} AS {alias}")
            group_aliases.append(alias)

        value_aliases: list[tuple[str, Any]] = []
        for agg in aggregations:
            alias = agg.output_name
            select_parts.append(f"{build_aggregation(model, agg)} AS {alias}")
            _, field = field_expr(model, agg.field) if agg.field else ("", None)
            value_aliases.append((alias, field))

        where, params = build_where(
            model, spec.filters, filter_logic=spec.filter_logic
        )
        sql = f"SELECT {', '.join(select_parts)} FROM records WHERE {where}"
        if spec.group_by:
            sql += " GROUP BY " + ", ".join(
                str(index + 1) for index in range(len(spec.group_by))
            )

        order_parts: list[str] = []
        for item in spec.sort:
            if item.field in spec.group_by:
                index = spec.group_by.index(item.field)
                order_parts.append(f"group_{index} {item.direction.upper()}")
            elif any(item.field == agg.output_name for agg in aggregations):
                order_parts.append(f"{item.field} {item.direction.upper()}")
            else:
                raise ValidationFailure(
                    f"聚合排序字段 '{item.field}' 必须是分组字段或聚合别名"
                )
        if order_parts:
            sql += " ORDER BY " + ", ".join(order_parts)
        sql += " LIMIT ?"
        params = [*params, spec.limit]

        rows = conn.execute(sql, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {}
            for index, key in enumerate(spec.group_by):
                item[key] = row[f"group_{index}"]
            for alias, field in value_aliases:
                value = row[alias]
                if isinstance(value, float) and field is not None and field.type == "decimal":
                    value = round(value, field.scale if field.scale is not None else 2)
                item[alias] = value
            items.append(item)
        return {
            "items": items,
            "group_by": spec.group_by,
            "aggregations": [agg.output_name for agg in aggregations],
            "model": model.key,
        }

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def create_record(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        values: dict[str, Any],
        *,
        actor: str = "user",
        record_id: str | None = None,
    ) -> dict[str, Any]:
        data = coerce_record_values(model, values)
        self._validate_relations(conn, model, data)

        record_id = record_id or new_id("rec")
        now = utc_now()
        with atomic(conn):
            conn.execute(
                """
                INSERT INTO records(id, model_id, data_json, revision, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (record_id, model.id, json.dumps(data, ensure_ascii=False), now, now),
            )
            self._sync_unique_keys(conn, model, record_id, data)
            self._sync_relations(conn, model, record_id, data)
            self._append_history(conn, model, record_id, 1, "created", data, actor)
        return self.get_record(conn, model, record_id)

    def update_record(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        record_id: str,
        values: dict[str, Any],
        *,
        expected_revision: int | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ? AND model_id = ? AND deleted_at IS NULL",
            (record_id, model.id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"记录 '{record_id}' 不存在")

        if expected_revision is not None and row["revision"] != expected_revision:
            raise ConflictError(
                "记录已被其他操作修改，请刷新后重试",
                details={
                    "current_revision": row["revision"],
                    "expected_revision": expected_revision,
                },
            )

        existing = json.loads(row["data_json"])
        data = coerce_record_values(model, values, partial=True, existing=existing)
        self._validate_relations(conn, model, data)

        revision = row["revision"] + 1
        with atomic(conn):
            conn.execute(
                "UPDATE records SET data_json = ?, revision = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), revision, utc_now(), record_id),
            )
            self._sync_unique_keys(conn, model, record_id, data)
            self._sync_relations(conn, model, record_id, data)
            self._append_history(conn, model, record_id, revision, "updated", data, actor)
        return self.get_record(conn, model, record_id)

    def delete_record(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        record_id: str,
        *,
        hard: bool = False,
        actor: str = "user",
    ) -> None:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ? AND model_id = ?",
            (record_id, model.id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"记录 '{record_id}' 不存在")

        data = json.loads(row["data_json"])
        revision = row["revision"] + 1
        with atomic(conn):
            if hard:
                conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
            else:
                conn.execute(
                    "UPDATE records SET deleted_at = ?, revision = ?, updated_at = ? WHERE id = ?",
                    (utc_now(), revision, utc_now(), record_id),
                )
            conn.execute("DELETE FROM record_relations WHERE from_record = ?", (record_id,))
            conn.execute("DELETE FROM record_unique_keys WHERE record_id = ?", (record_id,))
            self._append_history(conn, model, record_id, revision, "deleted", data, actor)

    def restore_record(
        self, conn: sqlite3.Connection, model: ModelDef, record_id: str, *, actor: str = "user"
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ? AND model_id = ?",
            (record_id, model.id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"记录 '{record_id}' 不存在")
        data = json.loads(row["data_json"])
        revision = row["revision"] + 1
        with atomic(conn):
            conn.execute(
                "UPDATE records SET deleted_at = NULL, revision = ?, updated_at = ? WHERE id = ?",
                (revision, utc_now(), record_id),
            )
            self._sync_unique_keys(conn, model, record_id, data)
            self._sync_relations(conn, model, record_id, data)
            self._append_history(conn, model, record_id, revision, "restored", data, actor)
        return self.get_record(conn, model, record_id)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _serialize(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        row: sqlite3.Row,
        *,
        expand: bool,
    ) -> dict[str, Any]:
        data = json.loads(row["data_json"])
        record: dict[str, Any] = {
            "id": row["id"],
            "model": model.key,
            "model_id": model.id,
            "data": data,
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
            "title": title_of(model, data, row["id"]),
        }
        if expand:
            labels = self._relation_labels(conn, model, data)
            if labels:
                record["labels"] = labels
        return record

    def _relation_labels(
        self, conn: sqlite3.Connection, model: ModelDef, data: dict[str, Any]
    ) -> dict[str, Any]:
        labels: dict[str, Any] = {}
        for field in model.relation_fields():
            if field.target_model is None:
                continue
            value = data.get(field.key)
            if value in (None, "", []):
                continue
            target = self.models.get_model_optional(conn, field.target_model)
            if target is None:
                continue
            is_list = isinstance(value, list)
            ids = [str(item) for item in value] if is_list else [str(value)]
            if not ids:
                continue
            placeholders = ", ".join("?" for _ in ids)
            rows = conn.execute(
                "SELECT id, data_json, deleted_at FROM records "
                f"WHERE model_id = ? AND id IN ({placeholders})",
                [target.id, *ids],
            ).fetchall()
            mapping = {}
            for row in rows:
                title = title_of(target, json.loads(row["data_json"]), row["id"])
                mapping[row["id"]] = title if row["deleted_at"] is None else f"{title}（已删除）"
            resolved = [mapping.get(item, "(已删除)") for item in ids]
            labels[field.key] = resolved if is_list else resolved[0]
        return labels

    def _validate_relations(
        self, conn: sqlite3.Connection, model: ModelDef, data: dict[str, Any]
    ) -> None:
        for field in model.relation_fields():
            if field.target_model is None:
                continue
            value = data.get(field.key)
            if value in (None, "", []):
                continue
            target = self.models.get_model_optional(conn, field.target_model)
            if target is None:
                raise ValidationFailure(
                    f"字段 '{field.name}' 关联的模型 '{field.target_model}' 不存在"
                )
            ids = value if isinstance(value, list) else [value]
            for target_id in ids:
                exists = conn.execute(
                    "SELECT 1 FROM records WHERE id = ? AND model_id = ? AND deleted_at IS NULL",
                    (str(target_id), target.id),
                ).fetchone()
                if exists is None:
                    raise ValidationFailure(
                        f"字段 '{field.name}' 引用的记录 '{target_id}' 不存在",
                        details={"field": field.key, "record_id": str(target_id)},
                    )

    def _sync_unique_keys(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        record_id: str,
        data: dict[str, Any],
    ) -> None:
        conn.execute(
            "DELETE FROM record_unique_keys WHERE record_id = ?", (record_id,)
        )
        for field in model.fields:
            if not field.unique:
                continue
            normalized = normalize_unique_value(field, data.get(field.key))
            if normalized is None:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO record_unique_keys(model_id, field_key, value, record_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (model.id, field.key, normalized, record_id),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(
                    f"字段 '{field.name}' 的值 '{data.get(field.key)}' 已存在"
                ) from None

    def _sync_relations(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        record_id: str,
        data: dict[str, Any],
    ) -> None:
        conn.execute("DELETE FROM record_relations WHERE from_record = ?", (record_id,))
        for field in model.relation_fields():
            value = data.get(field.key)
            if value in (None, "", []):
                continue
            ids = value if isinstance(value, list) else [value]
            for target_id in ids:
                conn.execute(
                    """
                    INSERT INTO record_relations(model_id, from_record, field_key, to_record)
                    VALUES (?, ?, ?, ?)
                    """,
                    (model.id, record_id, field.key, str(target_id)),
                )

    def _append_history(
        self,
        conn: sqlite3.Connection,
        model: ModelDef,
        record_id: str,
        revision: int,
        change_type: str,
        data: dict[str, Any],
        actor: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO record_history(id, model_id, record_id, revision, change_type,
                                       data_json, created_at, actor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("hist"),
                model.id,
                record_id,
                revision,
                change_type,
                json.dumps(data, ensure_ascii=False),
                utc_now(),
                actor,
            ),
        )
