"""Domain errors shared by every engine.

Engines raise these instead of HTTP exceptions so that they can be reused by
agent tools and tests without an HTTP layer in sight.
"""

from __future__ import annotations

import json
from typing import Any


class ZooAgentError(Exception):
    code = "error"
    status_code = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return {"error": body}


class NotFoundError(ZooAgentError):
    code = "not_found"
    status_code = 404


class ConflictError(ZooAgentError):
    code = "conflict"
    status_code = 409


class ValidationFailure(ZooAgentError):
    code = "validation_error"
    status_code = 422


class PermissionDenied(ZooAgentError):
    code = "permission_denied"
    status_code = 403


def jsonable(value: Any) -> Any:
    """Best-effort conversion so an error payload always serialises to JSON."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


class ActionExecutionError(ZooAgentError):
    """An Action failed while running.

    Carries everything needed to diagnose it *after* the transaction has been
    rolled back: the definition that ran, the inputs, the failing step, the
    trace of earlier steps and the writes that were undone. The envelope is
    what the automatic diagnosis hands to the primary assistant.
    """

    code = "action_failed"
    status_code = 422

    def __init__(
        self,
        message: str,
        *,
        action: Any,
        inputs: dict[str, Any] | None = None,
        step: Any = None,
        step_index: int | None = None,
        trace: list[dict[str, Any]] | None = None,
        effects: list[dict[str, Any]] | None = None,
        app_name: str = "",
        application_id: str = "",
        original: BaseException | None = None,
    ) -> None:
        self.action = action
        self.inputs = dict(inputs or {})
        self.step = step
        self.step_index = step_index
        self.trace = list(trace or [])
        self.effects = list(effects or [])
        self.app_name = app_name
        self.application_id = application_id
        self.original = original

        definition = (
            action.model_dump() if hasattr(action, "model_dump") else action
        )
        details = {
            "application": {"id": application_id, "name": app_name},
            "action": jsonable(definition),
            "inputs": jsonable(self.inputs),
            "step": (
                {
                    "id": getattr(step, "id", None),
                    "type": getattr(step, "type", None),
                    "index": step_index,
                }
                if step is not None
                else None
            ),
            "trace": jsonable(self.trace),
            "effects": jsonable(self.effects),
            "error": {
                "code": getattr(original, "code", type(original).__name__)
                if original is not None
                else "error",
                "message": str(original) if original is not None else message,
                "details": jsonable(getattr(original, "details", None)),
            },
            # Every step ran inside one transaction; the failure undid all of it.
            "rolled_back": True,
        }
        super().__init__(message, details=details)

    def to_dict(self) -> dict[str, Any]:
        body = super().to_dict()
        body["error"]["retryable"] = False
        return body


class ProviderError(ZooAgentError):
    """The configured LLM provider failed or is not available."""

    code = "provider_error"
    status_code = 502


class ConfigurationError(ZooAgentError):
    code = "configuration_error"
    status_code = 400
