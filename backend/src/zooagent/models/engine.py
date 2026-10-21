"""Model Engine: create, version and evolve business models."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import ConflictError, NotFoundError, ValidationFailure
from ..ids import new_id, utc_now
from ..persistence import atomic
from .schema import FieldDef, ModelDef, merge_field_ids


def _row_to_model(row: sqlite3.Row) -> ModelDef:
    payload = json.loads(row["schema_json"])
    return ModelDef.model_validate(payload)


class ModelEngine:
    """Metadata layer for business models.

    The engine never issues DDL for a business model: versions are rows in
    ``model_versions`` and records keep living in the fixed ``records`` table.
    """

    def list_models(
        self, conn: sqlite3.Connection, *, include_deleted: bool = False
    ) -> list[ModelDef]:
        sql = """
            SELECT m.id, m.model_key, m.name, m.description, m.active_version,
                   m.status, v.schema_json
            FROM models m
            JOIN model_versions v
              ON v.model_id = m.id AND v.version = m.active_version
        """
        params: list[Any] = []
        if not include_deleted:
            sql += " WHERE m.status = 'active'"
        sql += " ORDER BY m.name COLLATE NOCASE"
        return [_row_to_model(row) for row in conn.execute(sql, params)]

    def _load_model(
        self, conn: sqlite3.Connection, ref: str, *, include_deleted: bool = False
    ) -> ModelDef | None:
        sql = """
            SELECT m.id, m.model_key, m.name, m.description, m.active_version,
                   m.status, v.schema_json
            FROM models m
            JOIN model_versions v
              ON v.model_id = m.id AND v.version = m.active_version
            WHERE (m.id = ? OR m.model_key = ?)
        """
        if not include_deleted:
            sql += " AND m.status = 'active'"
        row = conn.execute(sql, (ref, ref)).fetchone()
        return _row_to_model(row) if row else None

    def get_model_optional(
        self, conn: sqlite3.Connection, ref: str
    ) -> ModelDef | None:
        return self._load_model(conn, ref)

    def get_model(self, conn: sqlite3.Connection, ref: str) -> ModelDef:
        model = self._load_model(conn, ref)
        if model is None:
            raise NotFoundError(f"模型 '{ref}' 不存在")
        return model

    def create_model(self, conn: sqlite3.Connection, spec: ModelDef) -> ModelDef:
        model = spec.model_copy(deep=True)
        model.version = 1
        model.status = "active"
        model.ensure_field_ids()
        self._validate_references(conn, model)

        existing = conn.execute(
            "SELECT id, status FROM models WHERE model_key = ?", (model.key,)
        ).fetchone()
        if existing is not None:
            if existing["status"] == "active":
                raise ConflictError(f"模型 '{model.key}' 已存在")
            # Revive a soft-deleted model, keeping its id so references stay valid.
            return self.update_model(conn, existing["id"], model, revive=True)

        model.id = model.id or new_id("mdl")
        now = utc_now()
        with atomic(conn):
            conn.execute(
                """
                INSERT INTO models(id, model_key, name, description, active_version,
                                   status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, 'active', ?, ?)
                """,
                (model.id, model.key, model.name, model.description, now, now),
            )
            conn.execute(
                """
                INSERT INTO model_versions(model_id, version, schema_json, created_at, created_by)
                VALUES (?, 1, ?, ?, 'agent')
                """,
                (model.id, json.dumps(model.model_dump(), ensure_ascii=False), now),
            )
            self.rebuild_indexes(conn, model)
        return model

    def update_model(
        self,
        conn: sqlite3.Connection,
        ref: str,
        spec: ModelDef,
        *,
        revive: bool = False,
    ) -> ModelDef:
        existing = self._load_model(conn, ref, include_deleted=True)
        if existing is None:
            raise NotFoundError(f"模型 '{ref}' 不存在")
        if spec.key != existing.key:
            raise ValidationFailure(
                "模型 key 不能修改（Action 与界面依赖它）。如确需重命名，请新建模型。"
            )
        merged = merge_field_ids(existing, spec)
        merged.id = existing.id
        merged.version = existing.version + 1
        merged.status = "active"
        merged.ensure_field_ids()
        self._validate_references(conn, merged)

        removed_relation_fields = [
            field.key
            for field in existing.relation_fields()
            if merged.field(field.key) is None
        ]

        now = utc_now()
        with atomic(conn):
            conn.execute(
                """
                UPDATE models
                   SET name = ?, description = ?, active_version = ?, status = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    merged.name,
                    merged.description,
                    merged.version,
                    "active" if revive else existing.status,
                    now,
                    merged.id,
                ),
            )
            conn.execute(
                """
                INSERT INTO model_versions(model_id, version, schema_json, created_at, created_by)
                VALUES (?, ?, ?, ?, 'agent')
                """,
                (
                    merged.id,
                    merged.version,
                    json.dumps(merged.model_dump(), ensure_ascii=False),
                    now,
                ),
            )
            for field_key in removed_relation_fields:
                conn.execute(
                    "DELETE FROM record_relations WHERE model_id = ? AND field_key = ?",
                    (merged.id, field_key),
                )
            self.rebuild_indexes(conn, merged)
        return merged

    def delete_model(self, conn: sqlite3.Connection, ref: str) -> None:
        model = self.get_model(conn, ref)
        with atomic(conn):
            conn.execute(
                "UPDATE models SET status = 'deleted', updated_at = ? WHERE id = ?",
                (utc_now(), model.id),
            )
            conn.execute("DELETE FROM record_unique_keys WHERE model_id = ?", (model.id,))

    def _validate_references(self, conn: sqlite3.Connection, model: ModelDef) -> None:
        for field in model.relation_fields():
            assert field.target_model is not None
            if field.target_model == model.key:
                continue
            row = conn.execute(
                "SELECT 1 FROM models WHERE model_key = ? AND status = 'active'",
                (field.target_model,),
            ).fetchone()
            if row is None:
                raise ValidationFailure(
                    f"字段 '{field.key}' 关联的模型 '{field.target_model}' 不存在。"
                    "请先创建被关联的模型。"
                )

    def rebuild_indexes(self, conn: sqlite3.Connection, model: ModelDef) -> None:
        """Rebuild unique-key rows for a model version.

        Called whenever a model version changes so that toggling ``unique`` on
        a field is enforced against existing records instead of silently
        ignored.
        """
        assert model.id is not None
        unique_fields: list[FieldDef] = [f for f in model.fields if f.unique]
        conn.execute("DELETE FROM record_unique_keys WHERE model_id = ?", (model.id,))
        if not unique_fields:
            return

        rows = conn.execute(
            "SELECT id, data_json FROM records WHERE model_id = ? AND deleted_at IS NULL",
            (model.id,),
        ).fetchall()
        from .validation import normalize_unique_value

        for row in rows:
            data = json.loads(row["data_json"])
            for field in unique_fields:
                normalized = normalize_unique_value(field, data.get(field.key))
                if normalized is None:
                    continue
                try:
                    conn.execute(
                        """
                        INSERT INTO record_unique_keys(model_id, field_key, value, record_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (model.id, field.key, normalized, row["id"]),
                    )
                except sqlite3.IntegrityError:
                    raise ConflictError(
                        f"字段 '{field.name}' 存在重复值 '{data.get(field.key)}'，"
                        "无法启用唯一约束"
                    ) from None
