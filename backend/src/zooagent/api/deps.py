"""Shared FastAPI dependencies: app state and local session authentication."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status

from ..state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.za


def require_token(request: Request, state: AppState = Depends(get_state)) -> None:
    """Every /api call must present the session token.

    The server only listens on 127.0.0.1, but a local token also protects
    against other local processes and against browser-based requests reaching
    the loopback interface.
    """
    expected = state.token
    if not expected:
        return
    provided = request.headers.get("authorization", "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    if not provided:
        provided = request.query_params.get("token", "") or request.headers.get(
            "x-zooagent-token", ""
        )
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未授权的本地会话"
        )
