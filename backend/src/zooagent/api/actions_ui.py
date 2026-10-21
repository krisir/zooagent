"""Action and UI schema endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from ..actions.schema import ActionDef
from ..errors import ActionExecutionError
from ..state import AppState
from ..ui_schema.schema import UISchema
from .deps import get_state, require_token

router = APIRouter(
    prefix="/applications/{application_id}",
    tags=["actions"],
    dependencies=[Depends(require_token)],
)


@router.get("/actions")
def list_actions(
    application_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        actions = ctx.actions.list_actions(ctx.conn)
        return {"actions": [action.model_dump() for action in actions]}


@router.post("/actions", status_code=201)
def define_action(
    application_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    spec = ActionDef.model_validate(payload)
    created = False
    with state.service.open_app(application_id) as ctx:
        existing = ctx.actions.get_action_optional(ctx.conn, spec.key)
        with ctx.conn:
            if existing is None:
                action = ctx.actions.create_action(ctx.conn, spec)
                created = True
            else:
                action = ctx.actions.update_action(ctx.conn, existing.key, spec)
    version = state.service.bump_version(application_id)
    state.service.refresh_manifest(application_id)
    return {"action": action.model_dump(), "created": created, "version": action.version,
            "application_version": version}


@router.get("/actions/{action_key}")
def get_action(
    application_id: str, action_key: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        action = ctx.actions.get_action(ctx.conn, action_key)
        return {"action": action.model_dump()}


@router.post("/actions/{action_key}/run")
async def run_action(
    application_id: str,
    action_key: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    input_values = payload.get("input", payload if isinstance(payload, dict) else {})
    with state.service.open_app(application_id) as ctx:
        action = ctx.actions.get_action(ctx.conn, action_key)
        try:
            with ctx.conn:
                result = ctx.actions.run(
                    ctx.conn,
                    action,
                    input_values,
                    actor="user",
                    app_name=ctx.application.name,
                    application_id=application_id,
                )
        except ActionExecutionError as exc:
            # The button must fail fast; the diagnosis runs detached because it
            # may call a model. Its report lands in the primary conversation and
            # a short relay in this application's conversation.
            state.diagnosis.schedule(
                exc.details, relay_application_id=application_id
            )
            if isinstance(exc.details, dict):
                exc.details["diagnosis"] = {
                    "status": "pending",
                    "where": "primary",
                    "hint": "已在主助手里排查，稍后到主助手或本应用对话里看结论。",
                }
            raise
    state.service.refresh_manifest(application_id)
    return result


@router.delete("/actions/{action_key}", status_code=204)
def delete_action(
    application_id: str, action_key: str, state: AppState = Depends(get_state)
) -> None:
    with state.service.open_app(application_id) as ctx:
        with ctx.conn:
            ctx.actions.delete_action(ctx.conn, action_key)
    state.service.bump_version(application_id)
    state.service.refresh_manifest(application_id)


@router.get("/ui")
def get_ui(application_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        return {"ui": ctx.ui.load(ctx.conn).model_dump()}


@router.put("/ui")
def put_ui(
    application_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    schema = UISchema.model_validate(payload)
    with state.service.open_app(application_id) as ctx:
        with ctx.conn:
            ctx.ui.replace(ctx.conn, schema)
    version = state.service.bump_version(application_id)
    state.service.refresh_manifest(application_id)
    return {"ui": schema.model_dump(), "application_version": version}
