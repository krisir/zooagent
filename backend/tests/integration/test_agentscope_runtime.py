"""AgentScope integration: real ReAct loop, offline, deterministically.

These tests run AgentScope's own ``Agent`` (ReAct loop + Toolkit + tool
protocol) against zooagent's real engines, using the mock model from
``agents/agentscope/mock_model.py``. No API key, no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from zooagent.data.query import QuerySpec
from zooagent.state import AppState

pytest.importorskip("agentscope")

from zooagent.agents.agentscope.mock_model import MockChatModel, MockPlanner  # noqa: E402
from zooagent.agents.agentscope.models import resolve_family  # noqa: E402
from zooagent.agents.agentscope.runtime import AgentScopeRuntime  # noqa: E402
from zooagent.agents.prompts import primary_system_prompt  # noqa: E402
from zooagent.agents.tools import build_primary_tools  # noqa: E402
from zooagent.config import ModelProfile  # noqa: E402


def activate_mock(state: AppState) -> None:
    settings = state.settings.load()
    settings.active_model_profile_id = "profile_mock"
    state.settings.save(settings)


async def collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]


def run(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return asyncio.run(collect(stream))


# ----------------------------------------------------------------------
# model/credential mapping
# ----------------------------------------------------------------------
def test_family_resolution_from_profile() -> None:
    deepseek = ModelProfile(id="p", name="ds", model_name="deepseek-chat")
    assert resolve_family(deepseek) == "deepseek"

    qwen = ModelProfile(
        id="p",
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_name="qwen-plus",
    )
    assert resolve_family(qwen) == "dashscope"

    local = ModelProfile(id="p", name="local", provider="ollama", model_name="qwen2.5:7b")
    assert resolve_family(local) == "ollama"

    explicit = ModelProfile(id="p", name="x", model_name="my-model", model_family="openai")
    assert resolve_family(explicit) == "openai"


def test_profile_builds_agentscope_model() -> None:
    from zooagent.agents.agentscope.models import build_chat_model

    profile = ModelProfile(
        id="p",
        name="DeepSeek",
        provider="agentscope",
        base_url="https://api.deepseek.com/v1",
        model_name="deepseek-chat",
        api_key="sk-test",
        temperature=0.3,
    )
    model = build_chat_model(profile)
    assert type(model).__name__ == "DeepSeekChatModel"
    assert model.model == "deepseek-chat"
    assert model.stream is False
    credential = model.credential
    assert credential.base_url == "https://api.deepseek.com/v1"
    assert credential.api_key.get_secret_value() == "sk-test"


# ----------------------------------------------------------------------
# planner behaviour (no AgentScope needed)
# ----------------------------------------------------------------------
def test_mock_planner_reads_requested_count() -> None:
    planner = MockPlanner()
    assert planner.requested_count("给交易生成 200 条测试数据", 120) == 200
    assert planner.requested_count("生成测试数据", 120) == 120
    assert planner.requested_count("生成 99999 条", 120) == 2000


# ----------------------------------------------------------------------
# the real AgentScope loop
# ----------------------------------------------------------------------
def test_agentscope_primary_builds_structure_only(state) -> None:
    activate_mock(state)
    prepared = state.agents.primary()
    assert isinstance(prepared.runtime, AgentScopeRuntime)

    events = run(
        prepared.runtime.stream(
            history=[],
            user_text="帮我做一个记账应用，并生成 150 条测试数据",
        )
    )
    types = [event["type"] for event in events]
    assert "error" not in types, events
    final = events[-1]
    assert final["type"] == "assistant_message"
    assert final["refresh"] is True

    tool_names = [event["name"] for event in events if event["type"] == "tool_call"]
    assert tool_names[0] == "create_application"
    assert "define_model" in tool_names
    # the primary has no data tools, even when the user asks for test data
    assert "generate_mock_data" not in tool_names
    assert "seed_records" not in tool_names
    # every reported result must be a success
    assert all(
        event["ok"] for event in events if event["type"] == "tool_result"
    ), [event for event in events if event["type"] == "tool_result" and not event["ok"]]

    applications = state.service.list_applications()
    assert len(applications) == 1
    application = applications[0]
    assert application.name == "个人记账"

    with state.service.open_app(application.id) as ctx:
        models = {model.key: model for model in ctx.models.list_models(ctx.conn)}
        assert set(models) == {"account", "category", "transaction"}

        # the app is a working shell: models, action and pages, but no rows
        for model in models.values():
            assert ctx.data.query_records(ctx.conn, model, QuerySpec(limit=1))["total"] == 0
        assert [page.key for page in ctx.ui.load(ctx.conn).pages] == [
            "overview",
            "transactions",
            "accounts",
        ]


def clear_model(ctx, model_key: str) -> int:
    """Hard-delete every record of a model (so a test starts from a clean slate)."""
    from zooagent.data.query import QuerySpec as Spec

    model = ctx.models.get_model(ctx.conn, model_key)
    items = ctx.data.query_records(ctx.conn, model, Spec(limit=1000))["items"]
    for item in items:
        ctx.data.delete_record(ctx.conn, model, item["id"], hard=True)
    return len(items)


def test_agentscope_app_agent_generates_more_data(state) -> None:
    activate_mock(state)
    primary = state.agents.primary()
    run(primary.runtime.stream(history=[], user_text="帮我做一个记账应用"))

    application = state.service.list_applications()[0]
    with state.service.open_app(application.id) as ctx:
        # the primary never seeds data: the new app starts empty
        model = ctx.models.get_model(ctx.conn, "transaction")
        assert ctx.data.query_records(ctx.conn, model, QuerySpec(limit=1))["total"] == 0
        # reference data the app assistant (or the user) would add first
        category = ctx.models.get_model(ctx.conn, "category")
        with ctx.conn:
            ctx.data.create_record(ctx.conn, category, {"name": "餐饮", "kind": "expense"}, actor="test")

    app_agent = state.agents.application(application.id)
    assert isinstance(app_agent.runtime, AgentScopeRuntime)

    events = run(
        app_agent.runtime.stream(history=[], user_text="给交易生成 80 条测试数据")
    )
    assert "error" not in [event["type"] for event in events], events
    assert events[-1]["type"] == "assistant_message"
    assert "80" in events[-1]["content"]

    with state.service.open_app(application.id) as ctx:
        model = ctx.models.get_model(ctx.conn, "transaction")
        assert ctx.data.query_records(ctx.conn, model, QuerySpec(limit=1))["total"] == 80
        # generated rows are real business data: enums valid, relations linked
        samples = ctx.data.query_records(ctx.conn, model, QuerySpec(limit=10))["items"]
        assert all(item["data"]["direction"] in {"expense", "income"} for item in samples)
        assert any(item["labels"].get("category") for item in samples)


def test_agentscope_runtime_replays_history(state) -> None:
    """Multi-turn context: prior messages must reach the model (awaited observe)."""
    from zooagent.agents.agentscope.convert import text_of
    from zooagent.agents.llm.base import ChatMessage
    from zooagent.agents.prompts import primary_system_prompt
    from zooagent.agents.tools import build_primary_tools

    class RecordingMock(MockChatModel):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[str] = []

        async def __call__(self, messages=None, tools=None, tool_choice=None, **kwargs):
            self.seen.append(" ".join(text_of(message) for message in (messages or [])))
            return await super().__call__(messages, tools, tool_choice, **kwargs)

    model = RecordingMock()
    runtime = AgentScopeRuntime(
        model=model,
        registry=build_primary_tools(state.service),
        system_prompt=primary_system_prompt([]),
    )
    run(runtime.stream(history=[], user_text="帮我做一个记账应用"))
    assert model.seen, "第一轮应当调用模型"

    model.seen.clear()
    run(
        runtime.stream(
            history=[
                ChatMessage(role="user", content="帮我做一个记账应用"),
                ChatMessage(role="assistant", content="好的，先确认三点：记什么、怎么统计、哪个操作要一键完成"),
            ],
            user_text="可以，就这样建",
        )
    )
    assert model.seen, "第二轮应当调用模型"
    assert any("先确认三点" in seen for seen in model.seen), "历史没有送到模型"
    assert any("帮我做一个记账应用" in seen for seen in model.seen)


def test_agentscope_app_agent_answers_with_aggregate(state) -> None:
    activate_mock(state)
    run(state.agents.primary().runtime.stream(history=[], user_text="帮我做一个记账应用"))
    application = state.service.list_applications()[0]

    with state.service.open_app(application.id) as ctx:
        clear_model(ctx, "transaction")  # keep only the row we add below

    app_agent = state.agents.application(application.id)
    run(app_agent.runtime.stream(history=[], user_text="餐饮 今天午餐花了 38 元"))
    stats = run(app_agent.runtime.stream(history=[], user_text="这个月一共花了多少"))
    assert stats[-1]["type"] == "assistant_message"
    assert "38" in stats[-1]["content"]
    aggregate_call = next(
        event
        for event in stats
        if event["type"] == "tool_call" and event["name"] == "aggregate"
    )
    assert aggregate_call["arguments"]["filters"], "统计应带上支出过滤"


def test_agentscope_runtime_stops_at_max_iterations(state) -> None:
    """A runaway plan must stop with an error event, not hang."""
    activate_mock(state)
    registry = build_primary_tools(state.service)
    runtime = AgentScopeRuntime(
        model=MockChatModel(),
        registry=registry,
        system_prompt=primary_system_prompt([]),
        max_iterations=3,
    )
    events = run(
        runtime.stream(history=[], user_text="帮我做一个记账应用，并生成 5 条测试数据")
    )
    assert events[-1]["type"] == "error"
    assert "次工具" in events[-1]["message"]
