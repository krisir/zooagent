"""Safe query DSL -> parameterised SQL.

Agents and the UI describe *what* they want (filters, sorting, aggregates).
Only this module turns that into SQL, and every value is bound as a parameter.
Business field keys are validated against the model before being interpolated
into a ``json_extract`` path, so the LLM can never smuggle SQL in.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..errors import ValidationFailure
from ..models.schema import FieldDef, ModelDef

FilterOperator = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "startswith",
    "endswith",
    "in",
    "between",
    "is_null",
    "not_null",
]

SYSTEM_COLUMNS = {"id", "revision", "created_at", "updated_at", "deleted_at"}


class Filter(BaseModel):
    field: str
    operator: FilterOperator = "eq"
    value: Any = None


class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"


class QuerySpec(BaseModel):
    filters: list[Filter] = Field(default_factory=list)
    filter_logic: Literal["and", "or"] = "and"
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    include_deleted: bool = False
    expand: bool = True


class Aggregation(BaseModel):
    function: Literal["count", "sum", "avg", "min", "max"]
    field: str | None = None
    alias: str | None = None

    @property
    def output_name(self) -> str:
        if self.alias:
            return self.alias
        return f"{self.function}_{self.field}" if self.field else self.function


class AggregateSpec(BaseModel):
    filters: list[Filter] = Field(default_factory=list)
    filter_logic: Literal["and", "or"] = "and"
    group_by: list[str] = Field(default_factory=list)
    #: Bucket date/datetime group fields so trends are readable
    #: (e.g. 180 days of records -> 6 monthly points).
    granularity: Literal["day", "month", "year"] | None = None
    aggregations: list[Aggregation] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int = Field(default=500, ge=1, le=5000)


SQLITE_DATE_FORMATS: dict[str, str] = {
    "day": "%Y-%m-%d",
    "month": "%Y-%m",
    "year": "%Y",
}


def field_expr(model: ModelDef, field_key: str) -> tuple[str, FieldDef | None]:
    """SQL expression for a field, plus the field definition if it is business data."""
    if field_key in SYSTEM_COLUMNS:
        return field_key, None
    field = model.field(field_key)
    if field is None:
        raise ValidationFailure(
            f"模型 '{model.key}' 不存在字段 '{field_key}'",
            details={"known_fields": [f.key for f in model.fields]},
        )
    raw = f"json_extract(data_json, '$.{field.key}')"
    if field.type in {"integer", "boolean"}:
        return f"CAST({raw} AS INTEGER)", field
    if field.type == "decimal":
        return f"CAST({raw} AS REAL)", field
    return raw, field


def sql_param(field: FieldDef | None, value: Any) -> Any:
    if field is None:
        return value
    if field.type == "boolean":
        return 1 if value else 0
    if field.type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValidationFailure(f"字段 '{field.key}' 需要整数") from None
    if field.type == "decimal":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValidationFailure(f"字段 '{field.key}' 需要数字") from None
    if field.type == "json":
        raise ValidationFailure(f"字段 '{field.key}' 是 JSON，不能直接筛选")
    return str(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_sql(model: ModelDef, item: Filter) -> tuple[str, list[Any]]:
    expr, field = field_expr(model, item.field)
    op = item.operator

    if op == "is_null":
        return f"{expr} IS NULL", []
    if op == "not_null":
        return f"{expr} IS NOT NULL", []

    if op in {"in", "between"}:
        if not isinstance(item.value, (list, tuple)):
            raise ValidationFailure(f"操作符 {op} 需要一个数组参数")
        values = list(item.value)
        if op == "between":
            if len(values) != 2:
                raise ValidationFailure("between 需要两个值")
            return (
                f"{expr} BETWEEN ? AND ?",
                [sql_param(field, values[0]), sql_param(field, values[1])],
            )
        if not values:
            return "0 = 1", []
        placeholders = ", ".join("?" for _ in values)
        return f"{expr} IN ({placeholders})", [sql_param(field, v) for v in values]

    if item.value is None:
        if op == "eq":
            return f"{expr} IS NULL", []
        if op == "ne":
            return f"{expr} IS NOT NULL", []
        raise ValidationFailure(f"操作符 {op} 需要一个值")

    if op in {"contains", "startswith", "endswith"}:
        text = str(item.value)
        escaped = _escape_like(text)
        if op == "contains":
            pattern = f"%{escaped}%"
        elif op == "startswith":
            pattern = f"{escaped}%"
        else:
            pattern = f"%{escaped}"
        return f"{expr} LIKE ? ESCAPE '\\'", [pattern]

    symbol = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }[op]
    return f"{expr} {symbol} ?", [sql_param(field, item.value)]


def build_where(
    model: ModelDef,
    filters: list[Filter],
    *,
    filter_logic: str = "and",
    include_deleted: bool = False,
) -> tuple[str, list[Any]]:
    clauses = ["model_id = ?"]
    params: list[Any] = [model.id]
    if not include_deleted:
        clauses.append("deleted_at IS NULL")

    if filters:
        parts: list[str] = []
        for item in filters:
            sql, values = _filter_sql(model, item)
            parts.append(sql)
            params.extend(values)
        joiner = " OR " if filter_logic == "or" else " AND "
        clauses.append("(" + joiner.join(parts) + ")")

    return " AND ".join(clauses), params


def build_order_by(model: ModelDef, sort: list[SortSpec]) -> str:
    if not sort:
        return "ORDER BY created_at DESC"
    parts: list[str] = []
    for item in sort:
        expr, _ = field_expr(model, item.field)
        direction = "DESC" if item.direction == "desc" else "ASC"
        parts.append(f"{expr} {direction}")
    parts.append("created_at DESC")
    return "ORDER BY " + ", ".join(parts)


def build_aggregation(model: ModelDef, agg: Aggregation) -> str:
    if agg.function == "count":
        return "COUNT(*)"
    if not agg.field:
        raise ValidationFailure(f"聚合函数 {agg.function} 需要指定字段")
    expr, field = field_expr(model, agg.field)
    if agg.function in {"sum", "avg"} and (field is None or field.type not in {
        "integer",
        "decimal",
    }):
        raise ValidationFailure(f"字段 '{agg.field}' 不支持 {agg.function} 聚合")
    return f"{agg.function.upper()}({expr})"


def group_expr(
    model: ModelDef, field_key: str, granularity: str | None
) -> tuple[str, FieldDef | None]:
    """SQL expression for a group key, optionally bucketed by date."""
    expr, field = field_expr(model, field_key)
    if granularity and field is not None and field.type in {"date", "datetime"}:
        date_format = SQLITE_DATE_FORMATS.get(granularity)
        if date_format:
            return f"strftime('{date_format}', {expr})", field
    return expr, field


def title_of(model: ModelDef, data: dict[str, Any], record_id: str) -> str:
    value = data.get(model.effective_title_field)
    if value is None or value == "":
        return record_id
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
