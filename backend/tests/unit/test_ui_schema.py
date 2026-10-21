"""Declarative UI is validated against the models at write time.

A component that points at a missing field would 422 on every render and the
browser silently falls back to 0 - which looks like "the data did not update".
"""

from __future__ import annotations

import pytest

from zooagent.errors import ValidationFailure
from zooagent.models.schema import FieldDef, ModelDef
from zooagent.ui_schema.schema import UISchema


def _period_model() -> ModelDef:
    return ModelDef(
        key="period_record",
        name="经期记录",
        title_field="start_date",
        fields=[
            FieldDef(key="start_date", name="开始日期", type="date", required=True),
            FieldDef(key="end_date", name="结束日期", type="date"),
        ],
    )


def _schema(*components: dict) -> UISchema:
    return UISchema.model_validate(
        {
            "pages": [
                {
                    "key": "overview",
                    "title": "概览",
                    "components": list(components),
                }
            ],
            "home_page": "overview",
        }
    )


def test_aggregate_field_is_validated(ctx) -> None:
    ctx.models.create_model(ctx.conn, _period_model())

    broken = _schema(
        {
            "type": "StatCard",
            "title": "平均经期天数",
            "model": "period_record",
            "props": {"aggregate": {"function": "avg", "field": "duration_days"}},
        }
    )
    with pytest.raises(ValidationFailure) as excinfo:
        ctx.ui.replace(ctx.conn, broken)
    assert "duration_days" in str(excinfo.value.details)

    ok = _schema(
        {
            "type": "StatCard",
            "title": "累计记录次数",
            "model": "period_record",
            "props": {"aggregate": {"function": "count"}},
        }
    )
    ctx.ui.replace(ctx.conn, ok)
    assert [page.key for page in ctx.ui.load(ctx.conn).pages] == ["overview"]


def test_calendar_and_filter_fields_are_validated(ctx) -> None:
    ctx.models.create_model(ctx.conn, _period_model())

    with pytest.raises(ValidationFailure):
        ctx.ui.replace(
            ctx.conn,
            _schema(
                {
                    "type": "Calendar",
                    "model": "period_record",
                    "props": {"date_field": "starts_on"},
                }
            ),
        )

    with pytest.raises(ValidationFailure):
        ctx.ui.replace(
            ctx.conn,
            _schema(
                {
                    "type": "FilterPanel",
                    "model": "period_record",
                    "fields": ["flow"],
                }
            ),
        )
