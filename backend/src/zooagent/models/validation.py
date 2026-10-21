"""Record value validation and coercion.

Constraints are enforced here, in code, never by asking the LLM to behave.
Every write path (REST API, Action Engine, agent tools) funnels through
:func:`coerce_record_values`.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..errors import ValidationFailure
from .schema import FieldDef, ModelDef

TRUE_STRINGS = {"true", "1", "yes", "y", "是"}
FALSE_STRINGS = {"false", "0", "no", "n", "否"}


def _fail(field: FieldDef, message: str) -> ValidationFailure:
    return ValidationFailure(
        f"字段 '{field.name}'({field.key})校验失败: {message}",
        details={"field": field.key, "reason": message},
    )


def coerce_value(field: FieldDef, value: Any) -> Any:
    """Return the canonical storage form of ``value`` for ``field``."""
    if value is None:
        if field.required:
            raise _fail(field, "不能为空")
        return None

    field_type = field.type

    if field_type == "text":
        if isinstance(value, (dict, list)):
            raise _fail(field, "必须是文本")
        text = str(value)
        if len(text) > 100_000:
            raise _fail(field, "长度超过 100000 字符")
        return text

    if field_type == "integer":
        if isinstance(value, bool):
            raise _fail(field, "必须是整数")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise _fail(field, "必须是整数") from None

    if field_type == "decimal":
        if isinstance(value, bool):
            raise _fail(field, "必须是数字")
        try:
            decimal_value = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError, TypeError):
            raise _fail(field, "必须是数字") from None
        if not decimal_value.is_finite():
            raise _fail(field, "必须是有限数字")
        scale = field.scale if field.scale is not None else 2
        quantized = decimal_value.quantize(
            Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP
        )
        return f"{quantized:.{scale}f}"

    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRUE_STRINGS:
                return True
            if lowered in FALSE_STRINGS:
                return False
        raise _fail(field, "必须是布尔值")

    if field_type == "date":
        if isinstance(value, dt.datetime):
            return value.date().isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        try:
            return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
        except ValueError:
            raise _fail(field, "必须是 ISO 日期，例如 2026-01-31") from None

    if field_type == "datetime":
        if isinstance(value, dt.datetime):
            moment = value
        else:
            raw = str(value).strip().replace("Z", "+00:00")
            try:
                moment = dt.datetime.fromisoformat(raw)
            except ValueError:
                raise _fail(field, "必须是 ISO 时间，例如 2026-01-31T09:00:00Z") from None
        if moment.tzinfo is not None:
            moment = moment.astimezone(dt.UTC).replace(tzinfo=None)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    if field_type == "enum":
        text = str(value)
        allowed = field.option_values
        if text not in allowed:
            raise _fail(field, f"取值必须是 {allowed} 之一")
        return text

    if field_type == "relation":
        if field.multiple:
            if not isinstance(value, list):
                raise _fail(field, "必须是记录 ID 列表")
            return [str(item) for item in value]
        if isinstance(value, (list, dict)):
            raise _fail(field, "必须是单个记录 ID")
        return str(value)

    if field_type == "json":
        if not isinstance(value, (dict, list)):
            raise _fail(field, "必须是 JSON 对象或数组")
        return value

    raise _fail(field, f"不支持的字段类型 {field_type}")  # pragma: no cover


def coerce_record_values(
    model: ModelDef,
    values: dict[str, Any],
    *,
    partial: bool = False,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a full or partial record payload.

    Returns a normalised dict containing only known field keys, with defaults
    applied. Unknown keys are rejected so that a confused agent fails loudly
    instead of silently writing junk.
    """
    if not isinstance(values, dict):
        raise ValidationFailure("记录内容必须是对象")

    known = {field.key for field in model.fields}
    unknown = [key for key in values if key not in known]
    if unknown:
        raise ValidationFailure(
            f"模型 '{model.key}' 不存在字段: {', '.join(sorted(unknown))}",
            details={"unknown_fields": sorted(unknown), "known_fields": sorted(known)},
        )

    result: dict[str, Any] = dict(existing or {}) if partial else {}

    for field in model.fields:
        if field.key in values:
            result[field.key] = coerce_value(field, values[field.key])
        elif not partial:
            if field.default is not None:
                result[field.key] = coerce_value(field, field.default)
            elif field.required:
                raise _fail(field, "必填字段缺失")
            else:
                result[field.key] = None

    for field in model.fields:
        if partial and field.required and result.get(field.key) is None:
            raise _fail(field, "必填字段缺失")

    return result


def normalize_unique_value(field: FieldDef, value: Any) -> str | None:
    """Canonical key used by ``record_unique_keys`` (case-insensitive text)."""
    if value is None:
        return None
    if field.type == "text":
        return str(value).strip().lower()
    if field.type == "boolean":
        return "1" if value else "0"
    if field.type in {"relation", "json"}:
        return None
    return str(value)
