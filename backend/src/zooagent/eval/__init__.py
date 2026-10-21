"""Generation-quality evaluation: a fixed requirement set scored structurally.

The point (design document 13.4) is not "does it look nice" but "given the same
requirements, does the generated application actually have the models, actions
and pages needed to do the job, and does it survive a follow-up requirement" -
so one case set can be run against different models and compared.

Layout:

* ``cases.py``     - case/expectation schema + TOML loader
* ``snapshot.py``  - turn an application into a comparable structure snapshot
* ``checks.py``    - evaluate expectations against a snapshot (+ metrics)
* ``runner.py``    - run cases (fresh workspaces, multiple turns) and report
* ``__main__.py``  - CLI: ``uv run python -m zooagent.eval``
"""

from __future__ import annotations

from .cases import Case, Expectation, load_cases
from .checks import CheckResult, evaluate
from .runner import CaseResult, EvalReport, run_cases
from .snapshot import AppSnapshot, TurnResult, capture_snapshot

__all__ = [
    "AppSnapshot",
    "Case",
    "CaseResult",
    "CheckResult",
    "EvalReport",
    "Expectation",
    "TurnResult",
    "capture_snapshot",
    "evaluate",
    "load_cases",
    "run_cases",
]
