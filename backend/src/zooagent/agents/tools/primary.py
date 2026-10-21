"""Tools available to the primary assistant.

The primary assistant owns the *structure* of applications: it creates apps,
models, actions and pages. It deliberately has **no** business-data tools — it
cannot read records the user has entered, and cannot write records into an
existing application. Business data belongs to each application's own
assistant, so a buggy or prompt-injected primary can never leak one app's data
into another conversation.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ...actions.schema import ActionDef
from ...application.service import ApplicationService
from ...errors import ValidationFailure
from ...models.schema import ModelDef
from ...ui_schema.schema import UISchema
from .registry import (
    ToolRegistry,
    field_schema,
    filters_schema,
    object_schema,
    page_schema,
    sort_schema,
)


def _validate(model_class: type, payload: Any, label: str) -> Any:
    try:
        return model_class.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailure(
            f"{label} 不合法: " + "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ),
            details=exc.errors(),
        ) from exc


def build_primary_tools(service: ApplicationService) -> ToolRegistry:
    registry = ToolRegistry()

    def list_applications(args: dict[str, Any]) -> Any:
        return {
            "applications": [
                {
                    "id": app.id,
                    "name": app.name,
                    "description": app.description,
                }
                for app in service.list_applications()
            ]
        }

    registry.register(
        "list_applications",
        "列出用户已有的业务应用。创建新应用前先调用，避免重复创建。",
        object_schema({}),
        list_applications,
    )

    def create_application(args: dict[str, Any]) -> Any:
        application = service.create_application(
            name=args["name"],
            description=args.get("description"),
            icon=args.get("icon"),
            color=args.get("color"),
        )
        return {"application": application.to_dict()}

    registry.register(
        "create_application",
        "创建一个新的业务应用。返回 application.id，后续 define_model/define_action/set_pages 都要用到它。",
        object_schema(
            {
                "name": {"type": "string", "description": "应用名称，例如 个人记账"},
                "description": {"type": "string"},
                "icon": {"type": "string"},
                "color": {"type": "string"},
            },
            ["name"],
        ),
        create_application,
        mutates=True,
        refresh=True,
    )

    def get_application_overview(args: dict[str, Any]) -> Any:
        application_id = args["application_id"]
        with service.open_app(application_id) as ctx:
            models = ctx.models.list_models(ctx.conn)
            actions = ctx.actions.list_actions(ctx.conn)
            ui = ctx.ui.load(ctx.conn)
            return {
                "application": ctx.application.to_dict(),
                "models": [model.to_prompt_dict() for model in models],
                "actions": [action.to_prompt_dict() for action in actions],
                "pages": [page.model_dump() for page in ui.pages],
            }

    registry.register(
        "get_application_overview",
        "查看一个应用当前的模型、Action 和页面定义。修改应用前先调用以了解现状。",
        object_schema(
            {"application_id": {"type": "string"}}, ["application_id"]
        ),
        get_application_overview,
    )

    def define_model(args: dict[str, Any]) -> Any:
        application_id = args["application_id"]
        spec = _validate(ModelDef, args["model"], "模型定义")
        with service.open_app(application_id) as ctx:
            existing = ctx.models.get_model_optional(ctx.conn, spec.key)
            if existing is not None:
                merged_fields = {field.key: field for field in existing.fields}
                for field in spec.fields:
                    merged_fields[field.key] = field
                spec.fields = list(merged_fields.values())
                if not spec.title_field:
                    spec.title_field = existing.title_field
                if not spec.description:
                    spec.description = existing.description
                with ctx.conn:
                    model = ctx.models.update_model(ctx.conn, existing.key, spec)
                created = False
            else:
                with ctx.conn:
                    model = ctx.models.create_model(ctx.conn, spec)
                created = True
        version = service.bump_version(application_id)
        service.refresh_manifest(application_id)
        return {
            "model": model.to_prompt_dict(),
            "created": created,
            "version": model.version,
            "application_version": version,
        }

    registry.register(
        "define_model",
        "创建或更新一个业务模型（数据表）。已存在同名模型时，字段按 key 合并，"
        "不会丢失已有字段；新版本自动保留历史数据。",
        object_schema(
            {
                "application_id": {"type": "string"},
                "model": object_schema(
                    {
                        "key": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "title_field": {
                            "type": "string",
                            "description": "用于显示记录标题的字段 key",
                        },
                        "fields": {"type": "array", "items": field_schema()},
                    },
                    ["key", "name", "fields"],
                ),
            },
            ["application_id", "model"],
        ),
        define_model,
        mutates=True,
        refresh=True,
    )

    def define_action(args: dict[str, Any]) -> Any:
        application_id = args["application_id"]
        spec = _validate(ActionDef, args["action"], "Action 定义")
        with service.open_app(application_id) as ctx:
            existing = ctx.actions.get_action_optional(ctx.conn, spec.key)
            with ctx.conn:
                if existing is not None:
                    action = ctx.actions.update_action(ctx.conn, existing.key, spec)
                else:
                    action = ctx.actions.create_action(ctx.conn, spec)
        version = service.bump_version(application_id)
        service.refresh_manifest(application_id)
        return {
            "action": action.to_prompt_dict(),
            "created": existing is None,
            "version": action.version,
            "application_version": version,
        }

    workflow_schema = object_schema(
        {
            "id": {"type": "string"},
            "type": {
                "type": "string",
                "enum": ["query", "aggregate", "create", "update", "delete", "compute", "return"],
            },
            "model": {"type": "string"},
            "filters": filters_schema(),
            "filter_logic": {"type": "string", "enum": ["and", "or"]},
            "sort": sort_schema(),
            "limit": {"type": "integer"},
            "group_by": {"type": "array", "items": {"type": "string"}},
            "aggregations": {"type": "array", "items": {"type": "object"}},
            "values": {
                "type": "object",
                "description": (
                    "字段值。字面量直接写；引用变量用 {\"ref\": \"input.amount\"}；"
                    "计算用 {\"expr\": \"...\"}"
                ),
            },
            "record_id": {},
            "expected_revision": {},
            "expression": {"type": "string"},
            "value": {"type": "object"},
            "output": {"type": "string"},
        },
        ["type"],
    )

    registry.register(
        "define_action",
        "创建或更新一个可重复执行的业务 Action（工作流）。执行时不调用大模型，因此必须写成确定性的步骤。",
        object_schema(
            {
                "application_id": {"type": "string"},
                "action": object_schema(
                    {
                        "key": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "read_only": {"type": "boolean"},
                        "inputs": {"type": "array", "items": field_schema()},
                        "steps": {"type": "array", "items": workflow_schema},
                        "output": {"type": "object"},
                    },
                    ["key", "name", "steps"],
                ),
            },
            ["application_id", "action"],
        ),
        define_action,
        mutates=True,
        refresh=True,
    )

    def set_pages(args: dict[str, Any]) -> Any:
        application_id = args["application_id"]
        schema = _validate(
            UISchema,
            {"pages": args.get("pages") or [], "home_page": args.get("home_page")},
            "界面定义",
        )
        with service.open_app(application_id) as ctx:
            with ctx.conn:
                ctx.ui.replace(ctx.conn, schema)
        version = service.bump_version(application_id)
        service.refresh_manifest(application_id)
        return {
            "pages": [{"key": page.key, "title": page.title} for page in schema.pages],
            "home_page": schema.home_page,
            "application_version": version,
        }

    registry.register(
        "set_pages",
        "用声明式 UI 定义整体替换应用的页面。组件只使用预置类型，不生成代码。",
        object_schema(
            {
                "application_id": {"type": "string"},
                "home_page": {"type": "string"},
                "pages": {"type": "array", "items": page_schema()},
            },
            ["application_id", "pages"],
        ),
        set_pages,
        mutates=True,
        refresh=True,
    )

    def set_app_agent(args: dict[str, Any]) -> Any:
        application_id = args["application_id"]
        agent = service.get_agent(application_id)
        if agent is None:
            raise ValidationFailure("该应用还没有专属助手")
        patch = {
            key: args[key]
            for key in ("name", "instructions", "greeting")
            if args.get(key) is not None
        }
        updated = service.update_agent(agent.id, patch)
        return {"agent": updated.to_dict()}

    registry.register(
        "set_app_agent",
        "设置应用专属助手的名称、系统指令和开场白。",
        object_schema(
            {
                "application_id": {"type": "string"},
                "name": {"type": "string"},
                "instructions": {"type": "string"},
                "greeting": {"type": "string"},
            },
            ["application_id"],
        ),
        set_app_agent,
        mutates=True,
    )

    def delete_application(args: dict[str, Any]) -> Any:
        application_id = args["application_id"]
        application = service.get_application(application_id)
        # Deleting an assistant is destructive and irreversible from the app's
        # own pages, so the caller must echo the name it is deleting. A wrong
        # id (or a mistaken confirmation) fails instead of wiping the wrong app.
        expected = str(args.get("confirm_name") or "").strip()
        if expected != application.name:
            raise ValidationFailure(
                "confirm_name 与要删除的助手名称不一致，未删除任何内容",
                details={"expected": application.name, "given": expected},
            )
        service.delete_application(application_id)
        return {
            "deleted": {"id": application.id, "name": application.name},
            "hint": "该助手已从列表移除，它的对话和页面都不再显示。",
        }

    registry.register(
        "delete_application",
        "删除一个业务应用（连带它的数据、Action 和页面）。这是不可恢复的操作："
        "必须先把助手名称复述给用户并得到明确确认，再传入 confirm_name 与其名称完全一致。",
        object_schema(
            {
                "application_id": {"type": "string"},
                "confirm_name": {
                    "type": "string",
                    "description": "用户确认要删除的助手名称，必须与应用名完全一致",
                },
            },
            ["application_id", "confirm_name"],
        ),
        delete_application,
        mutates=True,
        refresh=True,
    )

    return registry
