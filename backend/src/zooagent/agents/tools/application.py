"""Tools available to an application assistant.

An app assistant is bound to one application id at construction time, so it can
never reach another application's data.
"""

from __future__ import annotations

from typing import Any

from ...application.service import ApplicationService
from ...data.query import AggregateSpec, Filter, QuerySpec, SortSpec
from .registry import ToolRegistry, filters_schema, object_schema, sort_schema


def _filters(raw: Any) -> list[Filter]:
    return [
        Filter(
            field=item["field"],
            operator=item.get("operator", "eq"),
            value=item.get("value"),
        )
        for item in raw or []
    ]


def _sort(raw: Any) -> list[SortSpec]:
    return [
        SortSpec(field=item["field"], direction=item.get("direction", "asc"))
        for item in raw or []
    ]


def build_application_tools(
    service: ApplicationService, application_id: str
) -> ToolRegistry:
    registry = ToolRegistry()

    def list_models(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            models = ctx.models.list_models(ctx.conn)
            return {"models": [model.to_prompt_dict() for model in models]}

    registry.register(
        "list_models",
        "列出当前应用的所有业务模型及其字段。操作数据前先调用。",
        object_schema({}),
        list_models,
    )

    def describe_model(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            return {"model": model.to_prompt_dict()}

    registry.register(
        "describe_model",
        "查看某个模型的完整字段定义。",
        object_schema({"model_key": {"type": "string"}}, ["model_key"]),
        describe_model,
    )

    def list_actions(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            actions = ctx.actions.list_actions(ctx.conn)
            return {"actions": [action.to_prompt_dict() for action in actions]}

    registry.register(
        "list_actions",
        "列出当前应用已发布的业务 Action。",
        object_schema({}),
        list_actions,
    )

    def query_records(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            spec = QuerySpec(
                filters=_filters(args.get("filters")),
                sort=_sort(args.get("sort")),
                limit=int(args.get("limit", 20)),
                offset=int(args.get("offset", 0)),
                expand=bool(args.get("expand", True)),
            )
            return ctx.data.query_records(ctx.conn, model, spec)

    registry.register(
        "query_records",
        "查询记录。filters 支持 eq/ne/gt/gte/lt/lte/contains/in/between/is_null/not_null。",
        object_schema(
            {
                "model_key": {"type": "string"},
                "filters": filters_schema(),
                "sort": sort_schema(),
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            ["model_key"],
        ),
        query_records,
    )

    def aggregate(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            spec = AggregateSpec(
                filters=_filters(args.get("filters")),
                group_by=args.get("group_by") or [],
                granularity=args.get("granularity"),
                aggregations=args.get("aggregations") or [{"function": "count"}],
                limit=int(args.get("limit", 200)),
            )
            return ctx.data.aggregate(ctx.conn, model, spec)

    registry.register(
        "aggregate",
        "聚合统计：count/sum/avg/min/max，可按字段分组。回答统计类问题必须用它，不要自己心算。",
        object_schema(
            {
                "model_key": {"type": "string"},
                "filters": filters_schema(),
                "group_by": {"type": "array", "items": {"type": "string"}},
                "granularity": {
                    "type": "string",
                    "enum": ["day", "month", "year"],
                    "description": "按日期分组的粒度，可用于趋势图（如按月）",
                },
                "aggregations": {"type": "array", "items": {"type": "object"}},
                "limit": {"type": "integer"},
            },
            ["model_key"],
        ),
        aggregate,
    )

    def create_record(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            with ctx.conn:
                return ctx.data.create_record(
                    ctx.conn, model, args.get("values") or {}, actor="app_agent"
                )

    registry.register(
        "create_record",
        "新增一条记录。values 的 key 必须是模型字段 key，类型必须匹配。关系字段填目标记录 id。",
        object_schema(
            {
                "model_key": {"type": "string"},
                "values": {"type": "object"},
            },
            ["model_key", "values"],
        ),
        create_record,
        mutates=True,
        refresh=True,
    )

    def update_record(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            with ctx.conn:
                return ctx.data.update_record(
                    ctx.conn,
                    model,
                    args["record_id"],
                    args.get("values") or {},
                    expected_revision=args.get("expected_revision"),
                    actor="app_agent",
                )

    registry.register(
        "update_record",
        "修改一条记录（只传需要修改的字段）。传入 expected_revision 可避免覆盖他人修改。",
        object_schema(
            {
                "model_key": {"type": "string"},
                "record_id": {"type": "string"},
                "values": {"type": "object"},
                "expected_revision": {"type": "integer"},
            },
            ["model_key", "record_id", "values"],
        ),
        update_record,
        mutates=True,
        refresh=True,
    )

    def delete_record(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            with ctx.conn:
                ctx.data.delete_record(
                    ctx.conn, model, args["record_id"], actor="app_agent"
                )
        return {"deleted": args["record_id"]}

    registry.register(
        "delete_record",
        "删除一条记录（软删除，可恢复）。删除前应先向用户确认。",
        object_schema(
            {"model_key": {"type": "string"}, "record_id": {"type": "string"}},
            ["model_key", "record_id"],
        ),
        delete_record,
        mutates=True,
        refresh=True,
    )

    def run_action(args: dict[str, Any]) -> Any:
        with service.open_app(application_id) as ctx:
            action = ctx.actions.get_action(ctx.conn, args["action_key"])
            with ctx.conn:
                return ctx.actions.run(
                    ctx.conn,
                    action,
                    args.get("input") or {},
                    actor="app_agent",
                    app_name=ctx.application.name,
                    application_id=application_id,
                )

    registry.register(
        "run_action",
        "执行一个已发布的业务 Action（事务内执行，失败自动回滚）。有对应 Action 时优先使用它。",
        object_schema(
            {"action_key": {"type": "string"}, "input": {"type": "object"}},
            ["action_key"],
        ),
        run_action,
        mutates=True,
        refresh=True,
    )

    def generate_mock_records(args: dict[str, Any]) -> Any:
        from ..mock_data import generate_records

        count = int(args.get("count", 60))
        seed = args.get("seed")
        with service.open_app(application_id) as ctx:
            model = ctx.models.get_model(ctx.conn, args["model_key"])
            with ctx.conn:
                report = generate_records(
                    ctx.conn,
                    ctx.models,
                    ctx.data,
                    model,
                    count,
                    seed=int(seed) if seed is not None else None,
                    actor="mock",
                )
        service.refresh_manifest(application_id)
        return report

    registry.register(
        "generate_mock_records",
        "为某个模型批量生成测试数据，用于演示和界面测试（表格、图表、分页）。",
        object_schema(
            {
                "model_key": {"type": "string"},
                "count": {"type": "integer", "description": "生成条数，1-2000"},
                "seed": {"type": "integer", "description": "随机种子，可复现同一批数据"},
            },
            ["model_key"],
        ),
        generate_mock_records,
        mutates=True,
        refresh=True,
    )

    return registry
