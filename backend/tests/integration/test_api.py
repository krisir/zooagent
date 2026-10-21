"""HTTP surface: auth, manual JSON authoring, optimistic locking, chat stream."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from zooagent.main import create_app

CUSTOMER_MODEL = {
    "key": "customer",
    "name": "客户",
    "title_field": "name",
    "fields": [
        {"key": "name", "name": "客户名称", "type": "text", "required": True, "unique": True},
        {
            "key": "level",
            "name": "等级",
            "type": "enum",
            "options": [
                {"value": "normal", "label": "普通"},
                {"value": "vip", "label": "VIP"},
            ],
            "default": "normal",
        },
        {"key": "balance", "name": "欠款", "type": "decimal", "scale": 2, "default": "0.00"},
    ],
}

ORDER_MODEL = {
    "key": "order",
    "name": "订单",
    "fields": [
        {"key": "code", "name": "编号", "type": "text", "required": True},
        {"key": "customer", "name": "客户", "type": "relation", "target_model": "customer"},
    ],
}


@pytest.fixture()
def client(state):
    app = create_app(state)
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {state.token}"})
        test_client.state = state  # type: ignore[attr-defined]
        yield test_client


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_missing_token_is_rejected(client: TestClient) -> None:
    client.headers.pop("Authorization")
    assert client.get("/api/applications").status_code == 401
    assert client.get("/api/applications?token=test-token").status_code == 200


def test_authoring_data_without_ai(client: TestClient) -> None:
    created = client.post("/api/applications", json={"name": "客户管理"})
    assert created.status_code == 201
    application_id = created.json()["application"]["id"]

    assert client.post(
        f"/api/applications/{application_id}/models", json=CUSTOMER_MODEL
    ).status_code == 201
    relation_ok = client.post(
        f"/api/applications/{application_id}/models", json=ORDER_MODEL
    )
    assert relation_ok.status_code == 201, relation_ok.text

    # relation to an unknown model is rejected
    bad = dict(ORDER_MODEL, key="bad_order")
    bad["fields"] = [{"key": "customer", "name": "客户", "type": "relation", "target_model": "nope"}]
    assert client.post(f"/api/applications/{application_id}/models", json=bad).status_code == 422

    # upsert bumps the model version and keeps the existing data
    with_notes = {
        **CUSTOMER_MODEL,
        "fields": [
            *CUSTOMER_MODEL["fields"],
            {"key": "notes", "name": "备注", "type": "text"},
        ],
    }
    updated = client.post(
        f"/api/applications/{application_id}/models", json=with_notes
    )
    assert updated.json()["version"] == 2
    assert updated.json()["created"] is False

    customer = client.post(
        f"/api/applications/{application_id}/models/customer/records",
        json={"values": {"name": "李四", "balance": "120"}},
    )
    assert customer.status_code == 201
    record = customer.json()["record"]
    assert record["data"]["balance"] == "120.00"
    assert record["title"] == "李四"

    listed = client.post(
        f"/api/applications/{application_id}/models/customer/query",
        json={"filters": [{"field": "level", "operator": "eq", "value": "normal"}]},
    )
    assert listed.json()["total"] == 1

    aggregated = client.post(
        f"/api/applications/{application_id}/models/customer/aggregate",
        json={"aggregations": [{"function": "sum", "field": "balance", "alias": "total"}]},
    )
    assert aggregated.json()["items"][0]["total"] == 120.0

    # stale revision -> 409 instead of a silent overwrite
    conflict = client.patch(
        f"/api/applications/{application_id}/models/customer/records/{record['id']}",
        json={"values": {"level": "vip"}, "expected_revision": 99},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"

    ok = client.patch(
        f"/api/applications/{application_id}/models/customer/records/{record['id']}",
        json={"values": {"level": "vip"}, "expected_revision": 1},
    )
    assert ok.json()["record"]["revision"] == 2

    history = client.get(
        f"/api/applications/{application_id}/models/customer/records/{record['id']}/history"
    )
    assert [row["change_type"] for row in history.json()["history"]] == ["created", "updated"]


def test_ui_schema_validation_rejects_unknown_references(client: TestClient) -> None:
    application_id = client.post("/api/applications", json={"name": "测试"}).json()["application"]["id"]
    client.post(f"/api/applications/{application_id}/models", json=CUSTOMER_MODEL)

    bad = {
        "pages": [
            {
                "key": "home",
                "title": "首页",
                "components": [
                    {"type": "DataTable", "model": "customer", "columns": [{"field": "nope"}]}
                ],
            }
        ]
    }
    response = client.put(f"/api/applications/{application_id}/ui", json=bad)
    assert response.status_code == 422
    assert "nope" in json.dumps(response.json(), ensure_ascii=False)

    good = {
        "pages": [
            {
                "key": "home",
                "title": "首页",
                "components": [
                    {"type": "DataTable", "model": "customer", "columns": [{"field": "name"}]},
                    {"type": "StatCard", "model": "customer", "props": {"aggregate": {"function": "count"}}},
                ],
            }
        ]
    }
    assert client.put(f"/api/applications/{application_id}/ui", json=good).status_code == 200
    overview = client.get(f"/api/applications/{application_id}").json()
    assert overview["ui"]["pages"][0]["key"] == "home"


def test_action_endpoint_runs_workflow(client: TestClient) -> None:
    application_id = client.post("/api/applications", json={"name": "测试"}).json()["application"]["id"]
    client.post(f"/api/applications/{application_id}/models", json=CUSTOMER_MODEL)

    action = {
        "key": "add_debt",
        "name": "增加欠款",
        "inputs": [
            {"key": "customer", "name": "客户", "type": "relation", "target_model": "customer", "required": True},
            {"key": "amount", "name": "金额", "type": "decimal", "scale": 2, "required": True},
        ],
        "steps": [
            {
                "id": "load",
                "type": "query",
                "model": "customer",
                "output": "customer",
                "filters": [{"field": "id", "operator": "eq", "value": {"ref": "input.customer"}}],
            },
            {
                "id": "total",
                "type": "compute",
                "output": "balance",
                "expression": "$steps.customer.items[0].data.balance + $input.amount",
            },
            {
                "id": "save",
                "type": "update",
                "model": "customer",
                "record_id": {"ref": "input.customer"},
                "values": {"balance": {"ref": "steps.balance"}},
                "output": "customer",
            },
        ],
    }
    assert client.post(f"/api/applications/{application_id}/actions", json=action).status_code == 201

    record = client.post(
        f"/api/applications/{application_id}/models/customer/records",
        json={"values": {"name": "王五", "balance": "10"}},
    ).json()["record"]

    run = client.post(
        f"/api/applications/{application_id}/actions/add_debt/run",
        json={"input": {"customer": record["id"], "amount": "5.5"}},
    )
    assert run.status_code == 200, run.text
    assert run.json()["output"]["data"]["balance"] == "15.50"

    # the action registry is part of the app manifest
    assert any(
        action["key"] == "add_debt"
        for action in client.get(f"/api/applications/{application_id}/actions").json()["actions"]
    )


def test_failed_action_returns_snapshot_and_schedules_primary_diagnosis(
    client: TestClient,
) -> None:
    state = client.state  # type: ignore[attr-defined]
    application_id = client.post(
        "/api/applications", json={"name": "测试"}
    ).json()["application"]["id"]
    client.post(f"/api/applications/{application_id}/models", json=CUSTOMER_MODEL)
    broken = {
        "key": "add_debt",
        "name": "增加欠款",
        "inputs": [
            {"key": "amount", "name": "金额", "type": "decimal", "required": True},
        ],
        "steps": [
            {
                "id": "save",
                "type": "create",
                "model": "customer",
                # `name` is the real field; `nmae` fails on every run
                "values": {"nmae": "x", "balance": {"ref": "input.amount"}},
                "output": "record",
            }
        ],
    }
    assert client.post(
        f"/api/applications/{application_id}/actions", json=broken
    ).status_code == 201

    response = client.post(
        f"/api/applications/{application_id}/actions/add_debt/run",
        json={"input": {"amount": "5"}},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "action_failed"
    assert error["details"]["rolled_back"] is True
    assert error["details"]["action"]["key"] == "add_debt"
    assert error["details"]["step"]["id"] == "save"
    assert error["details"]["diagnosis"]["status"] == "pending"

    # the diagnosis runs detached; wait for the report to land
    import time

    primary = state.service.get_primary_agent()
    conversation = state.conversations.get_or_create(primary.id)
    deadline = time.time() + 5
    report = None
    while time.time() < deadline:
        messages = state.conversations.messages(conversation.id)
        if messages:
            report = messages[-1]
            break
        time.sleep(0.05)

    assert report is not None, "diagnosis report never reached the primary conversation"
    assert report["metadata"]["kind"] == "action_diagnosis"
    assert "nmae" in report["content"]
    assert "name" in report["content"]

    # nothing was written by the failed run
    model = client.get(
        f"/api/applications/{application_id}/models"
    ).json()["models"][0]
    assert model["key"] == "customer"
    assert client.post(
        f"/api/applications/{application_id}/models/customer/query", json={}
    ).json()["total"] == 0


def test_chat_stream_and_settings(client: TestClient) -> None:
    response = client.post("/api/chat/primary", json={"message": "帮我做一个记账应用"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    types = [event["type"] for event in events]
    # 第一个事件就带会话 id：客户端中止后仍能在这条会话里继续
    assert types[0] == "start"
    assert events[0]["conversation_id"]
    assert "tool_call" in types and types[-1] == "done"
    assert events[-1]["refresh"] is True

    conversation = client.get("/api/chat/primary").json()
    assert [message["role"] for message in conversation["messages"]] == ["user", "assistant"]

    settings = client.get("/api/settings").json()
    assert settings["active_model_profile_id"] == "profile_offline"
    assert any(provider["key"] == "agentscope-deepseek" for provider in settings["providers"])
    offline_profile = settings["profiles"][0]
    assert "api_key" not in offline_profile
    assert offline_profile["api_key_set"] is False
    assert offline_profile["api_key_last4"] == ""

    probe = client.post("/api/settings/test", json={}).json()
    assert probe["ok"] is True

    workspace = client.get("/api/workspace").json()
    assert workspace["offline_mode"] is True


def test_workspace_overview_counts_assistants(client: TestClient) -> None:
    created = client.post("/api/applications", json={"name": "客户管理"}).json()["application"]
    client.post(f"/api/applications/{created['id']}/models", json=CUSTOMER_MODEL)
    client.post(f"/api/applications/{created['id']}/models", json=ORDER_MODEL)

    overview = client.get("/api/workspace/overview").json()
    assert overview["totals"]["assistants"] == 1
    assert overview["totals"]["models"] == 2
    row = overview["assistants"][0]
    assert row["id"] == created["id"]
    assert row["counts"] == {"models": 2, "actions": 0, "pages": 0}
    # 结构信息之外不夹带业务数据
    assert "records" not in row


def test_api_key_never_leaves_the_backend(client) -> None:
    secret = "sk-live-1234567890abcdefWXYZ"
    created = client.post(
        "/api/settings/profiles",
        json={
            "name": "DeepSeek 测试",
            "provider": "agentscope",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat",
            "api_key": secret,
        },
    )
    assert created.status_code == 200
    assert secret not in created.text

    payload = client.get("/api/settings")
    assert secret not in payload.text, "接口绝不能返回完整密钥"
    profile = next(
        item for item in payload.json()["profiles"] if item["name"] == "DeepSeek 测试"
    )
    assert "api_key" not in profile
    assert profile["api_key_set"] is True
    assert profile["api_key_last4"] == secret[-4:]

    # saving again with an empty key must keep the stored secret
    again = client.post(
        "/api/settings/profiles",
        json={
            "id": profile["id"],
            "name": "DeepSeek 测试",
            "provider": "agentscope",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat",
            "api_key": "",
        },
    )
    assert again.status_code == 200
    assert secret not in again.text
    assert again.json()["profile"]["api_key_last4"] == secret[-4:]
