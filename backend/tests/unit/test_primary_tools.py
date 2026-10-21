"""The primary assistant's permission boundary.

The primary assistant creates and evolves applications. It must never be able
to read or write the business data inside them — that belongs to each
application's own assistant. These tests pin the exact tool set so a future
"convenience" tool cannot silently widen the boundary.
"""

from __future__ import annotations

import pytest

from zooagent.agents.tools import build_application_tools, build_primary_tools

STRUCTURE_TOOLS = {
    "list_applications",
    "create_application",
    "get_application_overview",
    "define_model",
    "define_action",
    "set_pages",
    "set_app_agent",
    "delete_application",
}

#: Anything in this set would let the primary touch business data.
DATA_TOOLS = {
    "query_records",
    "aggregate",
    "create_record",
    "update_record",
    "delete_record",
    "run_action",
    "seed_records",
    "generate_mock_data",
    "list_records",
    "list_models",
    "describe_model",
    "list_actions",
}


@pytest.fixture()
def registry(state):
    return build_primary_tools(state.service)


def test_primary_has_exactly_the_structure_tools(registry) -> None:
    assert set(registry.tools) == STRUCTURE_TOOLS


def test_primary_exposes_no_data_tools(registry) -> None:
    assert DATA_TOOLS & set(registry.tools) == set()


@pytest.mark.parametrize("name", sorted(DATA_TOOLS))
def test_calling_a_data_tool_is_rejected(registry, name) -> None:
    result = registry.call(name, {"application_id": "app_x", "model_key": "anything"})
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_tool"


def test_primary_prompt_states_the_boundary() -> None:
    from zooagent.agents.prompts import primary_system_prompt

    prompt = primary_system_prompt([])
    assert "不碰业务数据" in prompt or "不能查询用户已经录入的数据" in prompt
    assert "seed_records" not in prompt
    assert "应用助手" in prompt


def test_primary_can_delete_an_application_only_with_its_name(state) -> None:
    application = state.service.create_application("客户管理", "test")
    registry = build_primary_tools(state.service)

    wrong = registry.call(
        "delete_application",
        {"application_id": application.id, "confirm_name": "个人记账"},
    )
    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "validation_error"
    assert [app.id for app in state.service.list_applications()] == [application.id]

    deleted = registry.call(
        "delete_application",
        {"application_id": application.id, "confirm_name": "客户管理"},
    )
    assert deleted["ok"] is True
    assert deleted["result"]["deleted"]["name"] == "客户管理"
    assert state.service.list_applications() == []


def test_app_assistant_cannot_delete_its_app(state) -> None:
    """Deleting an assistant is the primary assistant's job, never the app's."""
    application = state.service.create_application("客户管理", "test")
    registry = build_application_tools(state.service, application.id)
    assert "delete_application" not in registry.tools


def test_primary_prompt_routes_deletion_through_conversation() -> None:
    from zooagent.agents.prompts import primary_system_prompt

    prompt = primary_system_prompt([])
    assert "delete_application" in prompt
    assert "没有删除按钮" in prompt


def test_primary_cannot_read_records_after_app_exists(state) -> None:
    """Even with an application present, there is no tool that reads its rows."""
    application = state.service.create_application("记账", "test")
    registry = build_primary_tools(state.service)

    overview = registry.call("get_application_overview", {"application_id": application.id})
    assert overview["ok"] is True
    # structure only: no record payload is returned by the overview
    assert set(overview["result"]) == {"application", "models", "actions", "pages"}

    records = registry.call(
        "query_records", {"application_id": application.id, "model_key": "transaction"}
    )
    assert records["ok"] is False
    assert "不存在工具" in records["error"]["message"]
