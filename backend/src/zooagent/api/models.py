"""Model, record, query and aggregate endpoints.

These are the same engines the agent tools use, exposed for the UI, tests and
manual JSON authoring (the M1 acceptance criterion: create an app without AI).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from ..data.query import AggregateSpec, QuerySpec
from ..errors import ValidationFailure
from ..models.schema import ModelDef
from ..state import AppState
from .deps import get_state, require_token

router = APIRouter(
    prefix="/applications/{application_id}",
    tags=["data"],
    dependencies=[Depends(require_token)],
)


@router.get("/models")
def list_models(
    application_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        models = ctx.models.list_models(ctx.conn)
    return {"models": [model.to_prompt_dict() for model in models]}


@router.post("/models", status_code=201)
def define_model(
    application_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    spec = ModelDef.model_validate(payload)
    created = False
    with state.service.open_app(application_id) as ctx:
        existing = ctx.models.get_model_optional(ctx.conn, spec.key)
        if existing is None:
            with ctx.conn:
                model = ctx.models.create_model(ctx.conn, spec)
            created = True
        else:
            with ctx.conn:
                model = ctx.models.update_model(ctx.conn, existing.key, spec)
    version = state.service.bump_version(application_id)
    state.service.refresh_manifest(application_id)
    return {
        "model": model.to_prompt_dict(),
        "created": created,
        "version": model.version,
        "application_version": version,
    }


@router.get("/models/{model_key}")
def get_model(
    application_id: str, model_key: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
    return {"model": model.to_prompt_dict()}


@router.delete("/models/{model_key}", status_code=204)
def delete_model(
    application_id: str, model_key: str, state: AppState = Depends(get_state)
) -> None:
    with state.service.open_app(application_id) as ctx:
        with ctx.conn:
            ctx.models.delete_model(ctx.conn, model_key)
    state.service.bump_version(application_id)
    state.service.refresh_manifest(application_id)


@router.post("/models/{model_key}/query")
def query_records(
    application_id: str,
    model_key: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    spec = QuerySpec.model_validate(payload)
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        return ctx.data.query_records(ctx.conn, model, spec)


@router.post("/models/{model_key}/aggregate")
def aggregate_records(
    application_id: str,
    model_key: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    spec = AggregateSpec.model_validate(payload)
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        return ctx.data.aggregate(ctx.conn, model, spec)


@router.post("/models/{model_key}/records", status_code=201)
def create_record(
    application_id: str,
    model_key: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        raise ValidationFailure("记录内容必须是对象")
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        with ctx.conn:
            record = ctx.data.create_record(
                ctx.conn, model, values, actor="user", record_id=payload.get("record_id")
            )
    return {"record": record}


@router.get("/models/{model_key}/records/{record_id}")
def get_record(
    application_id: str,
    model_key: str,
    record_id: str,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        record = ctx.data.get_record(ctx.conn, model, record_id)
    return {"record": record}


@router.patch("/models/{model_key}/records/{record_id}")
def update_record(
    application_id: str,
    model_key: str,
    record_id: str,
    payload: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    values = payload.get("values", payload)
    expected_revision = None
    if isinstance(payload.get("values"), dict) and payload.get("expected_revision") is not None:
        try:
            expected_revision = int(payload["expected_revision"])
        except (TypeError, ValueError):
            raise ValidationFailure("expected_revision 必须是整数") from None
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        with ctx.conn:
            record = ctx.data.update_record(
                ctx.conn,
                model,
                record_id,
                values,
                expected_revision=expected_revision,
                actor="user",
            )
    return {"record": record}


@router.delete("/models/{model_key}/records/{record_id}", status_code=204)
def delete_record(
    application_id: str,
    model_key: str,
    record_id: str,
    hard: bool = Query(default=False),
    state: AppState = Depends(get_state),
) -> None:
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        with ctx.conn:
            ctx.data.delete_record(ctx.conn, model, record_id, hard=hard, actor="user")


@router.post("/models/{model_key}/records/{record_id}/restore")
def restore_record(
    application_id: str,
    model_key: str,
    record_id: str,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        with ctx.conn:
            record = ctx.data.restore_record(ctx.conn, model, record_id, actor="user")
    return {"record": record}


@router.get("/models/{model_key}/records/{record_id}/history")
def record_history(
    application_id: str,
    model_key: str,
    record_id: str,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, model_key)
        rows = ctx.conn.execute(
            """
            SELECT revision, change_type, data_json, created_at, actor
            FROM record_history WHERE record_id = ? AND model_id = ?
            ORDER BY revision
            """,
            (record_id, model.id),
        ).fetchall()
    return {
        "history": [
            {
                "revision": row["revision"],
                "change_type": row["change_type"],
                "data": json.loads(row["data_json"]) if row["data_json"] else None,
                "created_at": row["created_at"],
                "actor": row["actor"],
            }
            for row in rows
        ]
    }
