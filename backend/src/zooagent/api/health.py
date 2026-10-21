"""Health and workspace metadata."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import __version__
from ..state import AppState
from .deps import get_state

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.get("/workspace")
def workspace(state: AppState = Depends(get_state)) -> dict[str, Any]:
    from ..agents.factory import describe_runtime

    profile = state.settings.active_profile()
    return {
        "version": __version__,
        "workspace": str(state.paths.root),
        "active_model_profile_id": profile.id,
        "active_model_name": profile.name,
        "offline_mode": profile.is_offline,
        "agent_runtime": describe_runtime(profile),
        "platform": __import__("sys").platform,
    }


@router.get("/workspace/overview")
def workspace_overview(state: AppState = Depends(get_state)) -> dict[str, Any]:
    """主助手数据页：工作台里有哪些助手、各自的数据规模。

    只读结构信息（模型 / 操作 / 页面），不含任何业务数据 —— 主助手按设计
    碰不到业务数据，它的数据页也不能成为后门。
    """
    totals = {"assistants": 0, "models": 0, "actions": 0, "pages": 0}
    assistants: list[dict[str, Any]] = []
    for application in state.service.list_applications():
        with state.service.open_app(application.id) as ctx:
            counts = {
                "models": len(ctx.models.list_models(ctx.conn)),
                "actions": len(ctx.actions.list_actions(ctx.conn)),
                "pages": len(ctx.ui.load(ctx.conn).pages),
            }
        totals["assistants"] += 1
        for key, value in counts.items():
            totals[key] += value
        assistants.append({**application.to_dict(), "counts": counts})

    return {"assistants": assistants, "totals": totals}
