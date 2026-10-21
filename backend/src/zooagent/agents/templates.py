"""App templates shared by the offline and mock providers.

These are complete, working application definitions (models -> action ->
pages). They live next to the offline planner because it was their first
consumer; this module is the stable import point for both providers, and the
place to move the literal definitions to when a third consumer appears.
"""

from __future__ import annotations

from .llm.offline import (
    GENERIC_SUMMARY,
    TEMPLATES,
)
from .llm.offline import (
    _create_args as create_args,
)
from .llm.offline import (
    _crm_plan as crm_plan,
)
from .llm.offline import (
    _detect as detect_template,
)
from .llm.offline import (
    _finance_plan as finance_plan,
)
from .llm.offline import (
    _list_plan as list_plan,
)

__all__ = [
    "GENERIC_SUMMARY",
    "TEMPLATES",
    "create_args",
    "crm_plan",
    "detect_template",
    "finance_plan",
    "list_plan",
]
