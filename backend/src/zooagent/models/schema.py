"""Business model definitions (metadata, not physical tables).

A ``ModelDef`` is what the AI writes when it "creates a table": a stable id, a
stable ``key`` for code/Action/UI references, a user-facing ``name``, and a
list of typed fields. Records are stored as JSON, so adding a field never
requires DDL and never rewrites existing rows.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ValidationFailure
from ..ids import new_id

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

FieldType = Literal[
    "text",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "relation",
    "json",
]

RESERVED_FIELD_KEYS = {
    "id",
    "revision",
    "created_at",
    "updated_at",
    "deleted_at",
}


class FieldDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    key: str
    name: str
    type: FieldType
    description: str | None = None
    required: bool = False
    unique: bool = False
    hidden: bool = False
    default: Any = None
    # enum
    options: list[dict[str, str]] | None = None
    # decimal
    scale: int | None = None
    # relation
    target_model: str | None = None
    multiple: bool = False

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        value = value.strip().lower()
        if not KEY_PATTERN.match(value):
            raise ValueError(
                "字段 key 必须以小写字母开头，只能包含小写字母、数字和下划线"
            )
        if value in RESERVED_FIELD_KEYS:
            raise ValueError(f"字段 key '{value}' 是系统保留字段")
        return value

    @model_validator(mode="after")
    def _check_typed_options(self) -> FieldDef:
        if self.type == "enum":
            if not self.options:
                raise ValueError(f"枚举字段 '{self.key}' 必须提供 options")
            for option in self.options:
                if "value" not in option:
                    raise ValueError(f"枚举字段 '{self.key}' 的选项必须包含 value")
        if self.type == "relation" and not self.target_model:
            raise ValueError(f"关系字段 '{self.key}' 必须指定 target_model")
        if self.type == "decimal" and self.scale is not None:
            if not 0 <= self.scale <= 6:
                raise ValueError("decimal.scale 必须介于 0 和 6 之间")
        if self.default is not None and self.required:
            raise ValueError(f"字段 '{self.key}' 既是必填又有默认值，请二选一")
        return self

    @property
    def option_values(self) -> list[str]:
        return [option["value"] for option in (self.options or [])]

    def to_prompt_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"key": self.key, "name": self.name, "type": self.type}
        if self.required:
            data["required"] = True
        if self.unique:
            data["unique"] = True
        if self.options:
            data["options"] = [
                {"value": option["value"], "label": option.get("label") or option["value"]}
                for option in self.options
            ]
        if self.scale is not None:
            data["scale"] = self.scale
        if self.target_model:
            data["target_model"] = self.target_model
        if self.multiple:
            data["multiple"] = True
        if self.default is not None:
            data["default"] = self.default
        if self.description:
            data["description"] = self.description
        return data


class ModelDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    key: str
    name: str
    description: str | None = None
    title_field: str | None = None
    version: int = 1
    status: str = "active"
    fields: list[FieldDef] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        value = value.strip().lower()
        if not KEY_PATTERN.match(value):
            raise ValueError(
                "模型 key 必须以小写字母开头，只能包含小写字母、数字和下划线"
            )
        return value

    @model_validator(mode="after")
    def _check_fields(self) -> ModelDef:
        if not self.fields:
            raise ValidationFailure(f"模型 '{self.key}' 至少需要一个字段")
        seen: set[str] = set()
        for field in self.fields:
            if field.key in seen:
                raise ValidationFailure(f"模型 '{self.key}' 存在重复字段 '{field.key}'")
            seen.add(field.key)
        if self.title_field and self.title_field not in seen:
            raise ValidationFailure(
                f"title_field '{self.title_field}' 不在模型 '{self.key}' 的字段中"
            )
        return self

    def ensure_field_ids(self) -> ModelDef:
        for field in self.fields:
            if not field.id:
                field.id = new_id("fld")
        return self

    def field(self, key: str) -> FieldDef | None:
        for field in self.fields:
            if field.key == key:
                return field
        return None

    def field_by_id(self, field_id: str) -> FieldDef | None:
        for field in self.fields:
            if field.id == field_id:
                return field
        return None

    @property
    def effective_title_field(self) -> str:
        if self.title_field:
            return self.title_field
        for field in self.fields:
            if field.type == "text" and not field.hidden:
                return field.key
        return self.fields[0].key

    def relation_fields(self) -> list[FieldDef]:
        return [f for f in self.fields if f.type == "relation"]

    def to_prompt_dict(self, include_titles: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "title_field": self.effective_title_field,
            "fields": [field.to_prompt_dict() for field in self.fields],
        }
        if self.description:
            data["description"] = self.description
        return data


def merge_field_ids(existing: ModelDef, incoming: ModelDef) -> ModelDef:
    """Keep stable field ids across model versions.

    Fields are matched by ``key``: renaming a display name never changes the
    id, and a genuinely new key gets a fresh id.
    """
    merged = incoming.model_copy(deep=True)
    for field in merged.fields:
        previous = existing.field(field.key)
        if previous is not None and previous.id:
            field.id = previous.id
        elif not field.id:
            field.id = new_id("fld")
    return merged
