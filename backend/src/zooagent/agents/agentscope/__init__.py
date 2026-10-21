"""AgentScope integration.

* ``models.py``   - zooagent model profiles -> AgentScope models/credentials
* ``toolkit.py``  - our ToolRegistry -> AgentScope Toolkit
* ``runtime.py``  - AgentScope ReAct Agent as the agent runtime
* ``mock_model.py`` - offline mock model implementing AgentScope's interface
* ``convert.py``  - message/response conversion, kept in one place
"""

from __future__ import annotations

from .models import (
    AGENTSCOPE_AVAILABLE,
    MODEL_FAMILIES,
    ModelFamily,
    build_chat_model,
    require_agentscope,
    resolve_family,
)
from .runtime import AGENTSCOPE_AGENT_AVAILABLE, AgentScopeRuntime
from .toolkit import build_toolkit

__all__ = [
    "AGENTSCOPE_AGENT_AVAILABLE",
    "AGENTSCOPE_AVAILABLE",
    "MODEL_FAMILIES",
    "AgentScopeRuntime",
    "ModelFamily",
    "build_chat_model",
    "build_toolkit",
    "require_agentscope",
    "resolve_family",
]
