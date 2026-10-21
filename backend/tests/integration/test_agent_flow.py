"""End-to-end: the design document's acceptance scenario, offline.

Nothing here needs an API key or network access: the offline planner calls the
same tools a real LLM would call, through the same engines.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from zooagent.agents.llm.base import ChatMessage
from zooagent.data.query import QuerySpec


async def collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]


def run_primary(state, text: str) -> list[dict[str, Any]]:
    import asyncio

    prepared = state.agents.primary()
    return asyncio.run(collect(prepared.runtime.stream(history=[], user_text=text)))


def run_app_agent(state, application_id: str, text: str) -> list[dict[str, Any]]:
    import asyncio

    prepared = state.agents.application(application_id)
    return asyncio.run(collect(prepared.runtime.stream(history=[], user_text=text)))


def test_primary_agent_creates_finance_app(state) -> None:
    events = run_primary(state, "我想记录家里的收入和支出，按分类统计")

    tool_names = [event["name"] for event in events if event["type"] == "tool_call"]
    assert tool_names[0] == "create_application"
    assert tool_names.count("define_model") == 3
    assert "define_action" in tool_names
    assert "set_pages" in tool_names
    # structure only: the primary assistant never touches business data
    assert "seed_records" not in tool_names

    final = events[-1]
    assert final["type"] == "assistant_message"
    assert "个人记账" in final["content"]

    applications = state.service.list_applications()
    assert len(applications) == 1
    application = applications[0]

    with state.service.open_app(application.id) as ctx:
        models = {model.key: model for model in ctx.models.list_models(ctx.conn)}
        assert set(models) == {"account", "category", "transaction"}
        assert models["transaction"].field("category").target_model == "category"

        actions = ctx.actions.list_actions(ctx.conn)
        assert [action.key for action in actions] == ["record_transaction"]

        ui = ctx.ui.load(ctx.conn)
        assert [page.key for page in ui.pages] == ["overview", "transactions", "accounts"]
        assert ui.home_page == "overview"

        # no initial data: reference rows are entered by the app assistant
        categories = ctx.data.query_records(
            ctx.conn, models["category"], QuerySpec(limit=50)
        )
        assert categories["total"] == 0


def add_category(state, application_id: str, name: str, kind: str = "expense") -> None:
    """Reference data normally comes from the app assistant; tests set it up directly."""
    with state.service.open_app(application_id) as ctx:
        model = ctx.models.get_model(ctx.conn, "category")
        with ctx.conn:
            ctx.data.create_record(ctx.conn, model, {"name": name, "kind": kind}, actor="test")


def test_app_agent_records_expense_and_reports_total(state) -> None:
    run_primary(state, "帮我做一个记账应用")
    application = state.service.list_applications()[0]
    add_category(state, application.id, "餐饮")

    events = run_app_agent(state, application.id, "餐饮 今天午餐花了 38 元")
    assert events[-1]["type"] == "assistant_message"
    assert "记录" in events[-1]["content"]
    # the confirmation quotes the note, not a raw record id
    assert "rec_" not in events[-1]["content"]

    with state.service.open_app(application.id) as ctx:
        model = ctx.models.get_model(ctx.conn, "transaction")

        result = ctx.data.query_records(ctx.conn, model, QuerySpec())
        assert result["total"] == 1
        record = result["items"][0]
        assert record["data"]["amount"] == "38.00"
        assert record["data"]["direction"] == "expense"
        # the user's own words become the note (and therefore the record title)
        assert "午餐" in record["data"]["note"]
        # "餐饮" was named in the message, so the relation is linked
        assert record["labels"]["category"] == "餐饮"

    # 花了多少 must exclude income
    run_app_agent(state, application.id, "这个月工资收入 12000 元")
    stats = run_app_agent(state, application.id, "这个月一共花了多少")
    assert stats[-1]["type"] == "assistant_message"
    assert "38" in stats[-1]["content"]
    assert "12038" not in stats[-1]["content"]
    aggregate_call = next(
        event
        for event in stats
        if event["type"] == "tool_call" and event["name"] == "aggregate"
    )
    assert aggregate_call["arguments"]["filters"][0]["value"] == "expense"


def test_app_agent_does_not_invent_relations(state) -> None:
    """Without a named category the relation stays empty instead of guessing."""
    run_primary(state, "帮我做一个记账应用")
    application = state.service.list_applications()[0]
    run_app_agent(state, application.id, "今天午餐花了 38 元")

    with state.service.open_app(application.id) as ctx:
        result = ctx.data.query_records(
            ctx.conn, ctx.models.get_model(ctx.conn, "transaction"), QuerySpec()
        )
        assert result["items"][0]["data"]["category"] is None


def test_primary_agent_creates_crm_app(state) -> None:
    events = run_primary(state, "帮我管理客户和跟进记录")
    assert events[-1]["type"] == "assistant_message"

    application = state.service.list_applications()[0]
    with state.service.open_app(application.id) as ctx:
        models = {model.key for model in ctx.models.list_models(ctx.conn)}
        assert models == {"customer", "contact_log"}
        assert [page.key for page in ctx.ui.load(ctx.conn).pages] == ["overview", "customers"]


def test_history_is_used_as_context(state) -> None:
    """A second turn sees the first turn's messages (no duplicate user message)."""
    import asyncio

    prepared = state.agents.primary()
    first = asyncio.run(collect(prepared.runtime.stream(history=[], user_text="你好")))
    assert first[-1]["type"] in {"assistant_message", "error"}

    history = [ChatMessage(role="user", content="你好"), ChatMessage(role="assistant", content="你好！")]
    second = asyncio.run(
        collect(prepared.runtime.stream(history=history, user_text="帮我做一个记账应用"))
    )
    assert any(
        event["type"] == "tool_call" and event["name"] == "create_application"
        for event in second
    )
