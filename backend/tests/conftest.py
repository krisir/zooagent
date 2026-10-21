from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zooagent.state import AppState  # noqa: E402


@pytest.fixture()
def state(tmp_path: Path) -> AppState:
    return AppState.create(tmp_path / "workspace", token="test-token")


@pytest.fixture()
def ctx(state: AppState):
    """An open application context (business database connection + engines)."""
    application = state.service.create_application("测试应用", "fixture")
    with state.service.open_app(application.id) as context:
        yield context
