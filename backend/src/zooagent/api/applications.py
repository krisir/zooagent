"""Application + agent endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from ..errors import ValidationFailure
from ..state import AppState
from .deps import get_state, require_token

router = APIRouter(
    prefix="/applications", tags=["applications"], dependencies=[Depends(require_token)]
)


@router.get("")
def list_applications(state: AppState = Depends(get_state)) -> dict[str, Any]:
    applications = state.service.list_applications()
    return {"applications": [app.to_dict() for app in applications]}


@router.post("", status_code=201)
def create_application(
    payload: dict[str, Any] = Body(...), state: AppState = Depends(get_state)
) -> dict[str, Any]:
    application = state.service.create_application(
        name=str(payload.get("name", "")),
        description=payload.get("description"),
        icon=payload.get("icon"),
        color=payload.get("color"),
        agent_name=payload.get("agent_name"),
        agent_instructions=payload.get("agent_instructions"),
    )
    return {"application": application.to_dict()}


@router.get("/{application_id}")
def get_application(
    application_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        models = ctx.models.list_models(ctx.conn)
        actions = ctx.actions.list_actions(ctx.conn)
        ui = ctx.ui.load(ctx.conn)
        agent = ctx.agent
    return {
        "application": ctx.application.to_dict(),
        "agent": agent.to_dict() if agent else None,
        "models": [model.to_prompt_dict() for model in models],
        "actions": [action.to_prompt_dict() for action in actions],
        "ui": ui.model_dump(),
    }


@router.patch("/{application_id}")
def update_application(
    application_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    application = state.service.update_application(application_id, payload)
    return {"application": application.to_dict()}


@router.delete("/{application_id}", status_code=204)
def delete_application(application_id: str, state: AppState = Depends(get_state)) -> None:
    state.service.delete_application(application_id)


@router.get("/{application_id}/agent")
def get_agent(application_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    agent = state.service.get_agent(application_id)
    if agent is None:
        raise ValidationFailure("该应用还没有专属助手")
    return {"agent": agent.to_dict()}


@router.patch("/{application_id}/agent")
def update_agent(
    application_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    agent = state.service.get_agent(application_id)
    if agent is None:
        raise ValidationFailure("该应用还没有专属助手")
    updated = state.service.update_agent(agent.id, payload)
    return {"agent": updated.to_dict()}
