"""Offline planner: a deterministic, template-based "model".

It exists so that the product is usable and testable with zero configuration:
no API key, no network. It covers the acceptance scenario from the design
document (create a bookkeeping app, then record an expense and ask for totals)
and otherwise asks the user to configure a real model.

Everything it does goes through the same tools a real LLM would call, so it
doubles as an integration test of the tool layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ...ids import new_id
from .base import AssistantTurn, ChatMessage, ToolCall

RECORD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(元|块|块钱|rmb|RMB)?")

STATS_KEYWORDS = ("统计", "合计", "一共", "总共", "多少", "总支出", "总收入", "汇总", "花了多少")
RECORD_KEYWORDS = ("花了", "支出", "收入", "收到", "买了", "消费", "付了")

EXPENSE_KEYWORDS = ("花", "支出", "买", "消费", "付")
INCOME_KEYWORDS = ("收入", "收到", "赚", "工资")
#: 文本字段被当作"备注"类字段时也会用用户原话填充，即使它不是必填
NOTE_KEYS = {"note", "notes", "remark", "remarks", "comment", "description", "desc"}


# ----------------------------------------------------------------------
# message introspection
# ----------------------------------------------------------------------
def _user_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content:
            return message.content
    return ""


def _calls(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Tool calls made after the latest user message, paired with their results."""
    last_user = 0
    for index, message in enumerate(messages):
        if message.role == "user":
            last_user = index

    calls: list[dict[str, Any]] = []
    for message in messages[last_user + 1 :]:
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                calls.append({"name": call.name, "arguments": call.arguments, "result": None})
        elif message.role == "tool" and calls and calls[-1]["result"] is None:
            try:
                calls[-1]["result"] = json.loads(message.content or "null")
            except json.JSONDecodeError:
                calls[-1]["result"] = {"ok": False, "error": {"message": message.content}}
    return calls


def _tool_call(name: str, arguments: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[ToolCall(id=new_id("call"), name=name, arguments=arguments)]
    )


def _failed(calls: list[dict[str, Any]]) -> bool:
    return any(isinstance(call.get("result"), dict) and not call["result"].get("ok") for call in calls)


def _error_text(calls: list[dict[str, Any]]) -> str:
    for call in reversed(calls):
        result = call.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            return f"{call['name']}: {error.get('message', '未知错误')}"
    return "未知错误"


def _result_of(calls: list[dict[str, Any]], name: str) -> Any:
    for call in calls:
        if call["name"] == name:
            payload = call.get("result") or {}
            return payload.get("result")
    return None


# ----------------------------------------------------------------------
# app templates
# ----------------------------------------------------------------------
def _field(key: str, name: str, type_: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "name": name, "type": type_, **extra}


def _relation(key: str, name: str, target: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "name": name, "type": "relation", "target_model": target, **extra}


def _finance_plan(app_id: str) -> list[tuple[str, dict[str, Any]]]:
    def model(payload: dict[str, Any]) -> dict[str, Any]:
        return {"application_id": app_id, "model": payload}

    return [
        (
            "define_model",
            model(
                {
                    "key": "account",
                    "name": "账户",
                    "title_field": "name",
                    "description": "资金存放位置",
                    "fields": [
                        _field("name", "账户名称", "text", required=True, unique=True),
                        _field(
                            "kind",
                            "账户类型",
                            "enum",
                            default="cash",
                            options=[
                                {"value": "cash", "label": "现金"},
                                {"value": "bank", "label": "银行卡"},
                                {"value": "wechat", "label": "微信"},
                                {"value": "alipay", "label": "支付宝"},
                            ],
                        ),
                        _field("balance", "余额", "decimal", scale=2, default="0.00"),
                    ],
                }
            ),
        ),
        (
            "define_model",
            model(
                {
                    "key": "category",
                    "name": "分类",
                    "title_field": "name",
                    "fields": [
                        _field("name", "分类名称", "text", required=True, unique=True),
                        _field(
                            "kind",
                            "收支类型",
                            "enum",
                            default="expense",
                            options=[
                                {"value": "expense", "label": "支出"},
                                {"value": "income", "label": "收入"},
                            ],
                        ),
                    ],
                }
            ),
        ),
        (
            "define_model",
            model(
                {
                    "key": "transaction",
                    "name": "交易",
                    "title_field": "note",
                    "description": "每一笔收入或支出",
                    "fields": [
                        _field("date", "日期", "date", required=True),
                        _field("amount", "金额", "decimal", scale=2, required=True),
                        _field(
                            "direction",
                            "收支方向",
                            "enum",
                            required=True,
                            options=[
                                {"value": "expense", "label": "支出"},
                                {"value": "income", "label": "收入"},
                            ],
                        ),
                        _relation("category", "分类", "category"),
                        _relation("account", "账户", "account"),
                        _field("note", "备注", "text"),
                    ],
                }
            ),
        ),
        (
            "define_action",
            {
                "application_id": app_id,
                "action": {
                    "key": "record_transaction",
                    "name": "记录一笔交易",
                    "description": "新增一条交易记录",
                    "inputs": [
                        _field("date", "日期", "date", required=True),
                        _field("amount", "金额", "decimal", scale=2, required=True),
                        _field(
                            "direction",
                            "收支方向",
                            "enum",
                            required=True,
                            options=[
                                {"value": "expense", "label": "支出"},
                                {"value": "income", "label": "收入"},
                            ],
                        ),
                        _relation("category", "分类", "category"),
                        _relation("account", "账户", "account"),
                        _field("note", "备注", "text"),
                    ],
                    "steps": [
                        {
                            "id": "create_transaction",
                            "type": "create",
                            "model": "transaction",
                            "output": "record",
                            "values": {
                                "date": {"ref": "input.date"},
                                "amount": {"ref": "input.amount"},
                                "direction": {"ref": "input.direction"},
                                "category": {"ref": "input.category"},
                                "account": {"ref": "input.account"},
                                "note": {"ref": "input.note"},
                            },
                        }
                    ],
                },
            },
        ),
        (
            "set_pages",
            {
                "application_id": app_id,
                "home_page": "overview",
                "pages": [
                    {
                        "key": "overview",
                        "title": "概览",
                        "icon": "dashboard",
                        "layout": "vertical",
                        "components": [
                            {"type": "Heading", "title": "个人记账", "props": {"level": 3}},
                            {
                                "type": "StatCard",
                                "title": "本月支出",
                                "model": "transaction",
                                "props": {
                                    "aggregate": {"function": "sum", "field": "amount"},
                                    "filters": [{"field": "direction", "operator": "eq", "value": "expense"}],
                                    "period": "this_month",
                                    "date_field": "date",
                                    "format": "currency",
                                },
                            },
                            {
                                "type": "StatCard",
                                "title": "本月收入",
                                "model": "transaction",
                                "props": {
                                    "aggregate": {"function": "sum", "field": "amount"},
                                    "filters": [{"field": "direction", "operator": "eq", "value": "income"}],
                                    "period": "this_month",
                                    "date_field": "date",
                                    "format": "currency",
                                },
                            },
                            {
                                "type": "Chart",
                                "title": "支出分类",
                                "model": "transaction",
                                "props": {
                                    "chart_type": "pie",
                                    "group_by": ["category"],
                                    "aggregate": {"function": "sum", "field": "amount"},
                                    "filters": [{"field": "direction", "operator": "eq", "value": "expense"}],
                                    "expand_labels": True,
                                },
                            },
                            {
                                "type": "Chart",
                                "title": "支出趋势（按月）",
                                "model": "transaction",
                                "props": {
                                    "chart_type": "line",
                                    "group_by": ["date"],
                                    "granularity": "month",
                                    "aggregate": {"function": "sum", "field": "amount"},
                                    "filters": [{"field": "direction", "operator": "eq", "value": "expense"}],
                                },
                            },
                            {
                                "type": "DataTable",
                                "title": "最近交易",
                                "model": "transaction",
                                "columns": [
                                    {"field": "date"},
                                    {"field": "category"},
                                    {"field": "amount"},
                                    {"field": "direction"},
                                    {"field": "note"},
                                ],
                                "props": {"page_size": 8},
                                "actions": [
                                    {
                                        "label": "新增交易",
                                        "kind": "run_action",
                                        "action": "record_transaction",
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "key": "transactions",
                        "title": "交易记录",
                        "icon": "table",
                        "components": [
                            {
                                "type": "DataTable",
                                "title": "全部交易",
                                "model": "transaction",
                                "columns": [
                                    {"field": "date"},
                                    {"field": "direction"},
                                    {"field": "category"},
                                    {"field": "account"},
                                    {"field": "amount"},
                                    {"field": "note"},
                                ],
                                "props": {"page_size": 20, "editable": True},
                                "actions": [
                                    {
                                        "label": "新增交易",
                                        "kind": "run_action",
                                        "action": "record_transaction",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "key": "accounts",
                        "title": "账户",
                        "icon": "credit-card",
                        "components": [
                            {
                                "type": "DataTable",
                                "title": "账户列表",
                                "model": "account",
                                "columns": [{"field": "name"}, {"field": "kind"}, {"field": "balance"}],
                                "actions": [{"label": "新增账户", "kind": "create"}],
                            },
                            {
                                "type": "DataTable",
                                "title": "分类",
                                "model": "category",
                                "columns": [{"field": "name"}, {"field": "kind"}],
                                "actions": [{"label": "新增分类", "kind": "create"}],
                            },
                        ],
                    },
                ],
            },
        ),
    ]


def _crm_plan(app_id: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "define_model",
            {
                "application_id": app_id,
                "model": {
                    "key": "customer",
                    "name": "客户",
                    "title_field": "name",
                    "fields": [
                        _field("name", "客户名称", "text", required=True, unique=True),
                        _field("phone", "联系电话", "text"),
                        _field(
                            "level",
                            "客户等级",
                            "enum",
                            default="normal",
                            options=[
                                {"value": "normal", "label": "普通"},
                                {"value": "vip", "label": "VIP"},
                            ],
                        ),
                        _field("balance", "应收欠款", "decimal", scale=2, default="0.00"),
                        _field("notes", "备注", "text"),
                    ],
                },
            },
        ),
        (
            "define_model",
            {
                "application_id": app_id,
                "model": {
                    "key": "contact_log",
                    "name": "跟进记录",
                    "title_field": "summary",
                    "fields": [
                        _relation("customer", "客户", "customer", required=True),
                        _field("date", "跟进日期", "date", required=True),
                        _field("summary", "跟进内容", "text", required=True),
                    ],
                },
            },
        ),
        (
            "define_action",
            {
                "application_id": app_id,
                "action": {
                    "key": "log_contact",
                    "name": "记录一次跟进",
                    "inputs": [
                        _relation("customer", "客户", "customer", required=True),
                        _field("date", "跟进日期", "date", required=True),
                        _field("summary", "跟进内容", "text", required=True),
                    ],
                    "steps": [
                        {
                            "id": "create_log",
                            "type": "create",
                            "model": "contact_log",
                            "output": "record",
                            "values": {
                                "customer": {"ref": "input.customer"},
                                "date": {"ref": "input.date"},
                                "summary": {"ref": "input.summary"},
                            },
                        }
                    ],
                },
            },
        ),
        (
            "set_pages",
            {
                "application_id": app_id,
                "home_page": "overview",
                "pages": [
                    {
                        "key": "overview",
                        "title": "概览",
                        "icon": "dashboard",
                        "components": [
                            {"type": "Heading", "title": "客户管理", "props": {"level": 3}},
                            {
                                "type": "StatCard",
                                "title": "客户总数",
                                "model": "customer",
                                "props": {"aggregate": {"function": "count"}, "format": "number"},
                            },
                            {
                                "type": "StatCard",
                                "title": "应收合计",
                                "model": "customer",
                                "props": {
                                    "aggregate": {"function": "sum", "field": "balance"},
                                    "format": "currency",
                                },
                            },
                            {
                                "type": "DataTable",
                                "title": "最近跟进",
                                "model": "contact_log",
                                "columns": [
                                    {"field": "date"},
                                    {"field": "customer"},
                                    {"field": "summary"},
                                ],
                                "props": {"page_size": 8},
                                "actions": [
                                    {"label": "记录跟进", "kind": "run_action", "action": "log_contact"}
                                ],
                            },
                        ],
                    },
                    {
                        "key": "customers",
                        "title": "客户",
                        "icon": "team",
                        "components": [
                            {
                                "type": "DataTable",
                                "title": "客户列表",
                                "model": "customer",
                                "columns": [
                                    {"field": "name"},
                                    {"field": "level"},
                                    {"field": "phone"},
                                    {"field": "balance"},
                                ],
                                "props": {"page_size": 20, "editable": True},
                                "actions": [
                                    {"label": "新增客户", "kind": "create"},
                                    {"label": "记录跟进", "kind": "run_action", "action": "log_contact"},
                                ],
                            }
                        ],
                    },
                ],
            },
        ),
    ]


def _list_plan(app_id: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "define_model",
            {
                "application_id": app_id,
                "model": {
                    "key": "item",
                    "name": "条目",
                    "title_field": "title",
                    "fields": [
                        _field("title", "标题", "text", required=True),
                        _field(
                            "status",
                            "状态",
                            "enum",
                            default="todo",
                            options=[
                                {"value": "todo", "label": "待办"},
                                {"value": "doing", "label": "进行中"},
                                {"value": "done", "label": "已完成"},
                            ],
                        ),
                        _field("due_date", "截止日期", "date"),
                        _field("notes", "备注", "text"),
                    ],
                },
            },
        ),
        (
            "set_pages",
            {
                "application_id": app_id,
                "home_page": "overview",
                "pages": [
                    {
                        "key": "overview",
                        "title": "概览",
                        "icon": "dashboard",
                        "components": [
                            {"type": "Heading", "title": "清单", "props": {"level": 3}},
                            {
                                "type": "StatCard",
                                "title": "待办数量",
                                "model": "item",
                                "props": {
                                    "aggregate": {"function": "count"},
                                    "filters": [{"field": "status", "operator": "eq", "value": "todo"}],
                                    "format": "number",
                                },
                            },
                            {
                                "type": "StatCard",
                                "title": "已完成",
                                "model": "item",
                                "props": {
                                    "aggregate": {"function": "count"},
                                    "filters": [{"field": "status", "operator": "eq", "value": "done"}],
                                    "format": "number",
                                },
                            },
                            {
                                "type": "DataTable",
                                "title": "全部条目",
                                "model": "item",
                                "columns": [
                                    {"field": "title"},
                                    {"field": "status"},
                                    {"field": "due_date"},
                                    {"field": "notes"},
                                ],
                                "props": {"page_size": 20, "editable": True},
                                "actions": [{"label": "新增条目", "kind": "create"}],
                            },
                        ],
                    }
                ],
            },
        ),
    ]


TEMPLATES: list[tuple[tuple[str, ...], Callable[[str], list], dict[str, Any]]] = [
    (
        ("记账", "收支", "支出", "收入", "开销", "花销", "账单", "预算", "财务", "存款", "花了"),
        _finance_plan,
        {
            "create": {
                "name": "个人记账",
                "description": "记录家庭收入与支出，按分类统计",
                "icon": "wallet",
            },
            "summary": (
                "已经为你创建「个人记账」应用：\n"
                "- 数据模型：账户、分类、交易\n"
                "- 业务操作：记录一笔交易（可重复执行，不依赖 AI）\n"
                "- 页面：概览（本月收支、支出分类饼图、最近交易）、交易记录、账户\n\n"
                "我只负责搭结构，不碰你的业务数据。接下来打开这个应用，用右下角的"
                "「应用助手」说一句「加几个常用分类和账户」，再直接说「今天午餐花了 38 元」就能记账。"
            ),
        },
    ),
    (
        ("客户", "crm", "CRM", "联系人", "销售", "线索", "跟进"),
        _crm_plan,
        {
            "create": {"name": "客户管理", "description": "管理客户资料与跟进记录", "icon": "team"},
            "summary": (
                "已经为你创建「客户管理」应用：\n"
                "- 数据模型：客户、跟进记录\n"
                "- 业务操作：记录一次跟进\n"
                "- 页面：概览（客户数、应收合计、最近跟进）、客户列表"
            ),
        },
    ),
    (
        ("待办", "任务", "清单", "习惯", "打卡", "计划", "日程"),
        _list_plan,
        {
            "create": {"name": "待办清单", "description": "管理日常待办与进度", "icon": "check"},
            "summary": (
                "已经为你创建「待办清单」应用：\n"
                "- 数据模型：条目（标题、状态、截止日期、备注）\n"
                "- 页面：概览（待办数量、已完成数量、全部条目）"
            ),
        },
    ),
]


GENERIC_SUMMARY = (
    "离线模式下我暂时没有匹配到这个需求的模板。你可以：\n"
    "1. 在「设置」里配置一个云端模型（OpenAI / DeepSeek / Ollama 等），之后我就能按你的描述自由创建应用；\n"
    "2. 或者把需求描述得更具体一些，我会尽量匹配内置模板。"
)


def _detect(text: str) -> tuple[Callable[[str], list], dict[str, Any]] | None:
    lowered = text.lower()
    for keywords, plan, meta in TEMPLATES:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return plan, meta
    return None


def _create_args(template_meta: dict[str, Any], text: str) -> dict[str, Any]:
    args = dict(template_meta["create"])
    if len(text.strip()) > 2:
        args["description"] = text.strip()[:200]
    return args


# ----------------------------------------------------------------------
# app-scoped planner
# ----------------------------------------------------------------------
def _models_from_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = _result_of(calls, "list_models") or {}
    return payload.get("models") or []


def _pick_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for model in models:
        numeric = next(
            (f for f in model.get("fields", []) if f.get("type") in {"decimal", "integer"}),
            None,
        )
        if numeric is None:
            continue
        candidate = {"key": model["key"], "model": model, "numeric": numeric["key"]}
        has_date = any(f.get("type") == "date" for f in model.get("fields", []))
        if has_date:
            return candidate
        best = best or candidate
    return best


def _enum_option(field: dict[str, Any], wanted: set[str]) -> str | None:
    for option in field.get("options") or []:
        value = str(option.get("value") if isinstance(option, dict) else option)
        if value in wanted:
            return value
    return None


def _expense_filters(model: dict[str, Any], text: str) -> list[dict[str, Any]] | None:
    """问"花了多少"时不能把收入也算进去。"""
    if not any(keyword in text for keyword in EXPENSE_KEYWORDS):
        return None
    for field in model.get("fields", []):
        if field.get("type") != "enum":
            continue
        value = _enum_option(field, {"expense", "out"})
        if value:
            return [{"field": field["key"], "operator": "eq", "value": value}]
    return None


def _relation_field(
    model: dict[str, Any], models: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The first relation whose target model we can actually query."""
    known = {item["key"] for item in models}
    for field in model.get("fields", []):
        if field.get("type") == "relation" and field.get("target_model") in known:
            return field
    return None


def _records_from_calls(calls: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    payload = _result_of(calls, name) or {}
    return payload.get("items") or []


def _match_relation(
    field: dict[str, Any], records: list[dict[str, Any]], text: str
) -> str | None:
    """Link a related record only when the user literally named it.

    No keyword guessing: if the message does not mention an existing
    category/account, the field stays empty and the user picks it in the form.
    """
    matched: tuple[int, str] | None = None
    for record in records:
        title = str(record.get("title") or "")
        if len(title) >= 2 and title in text:
            if matched is None or len(title) > matched[0]:
                matched = (len(title), str(record["id"]))
    return matched[1] if matched else None


def _guess_values(text: str, model: dict[str, Any]) -> dict[str, Any]:
    import datetime as dt

    values: dict[str, Any] = {}
    match = RECORD_RE.search(text)
    today = dt.date.today().isoformat()
    wants_income = any(keyword in text for keyword in INCOME_KEYWORDS)
    wants_expense = any(keyword in text for keyword in EXPENSE_KEYWORDS)

    for field in model.get("fields", []):
        key, type_ = field["key"], field["type"]
        if type_ == "decimal" and match and "amount" not in values:
            values[key] = match.group(1)
        elif type_ == "integer" and match and not any(
            f.get("type") == "decimal" for f in model.get("fields", [])
        ):
            values[key] = match.group(1)
        elif type_ == "date":
            values[key] = today
        elif type_ == "datetime":
            values[key] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        elif type_ == "enum":
            raw_options = field.get("options") or []
            options = [
                str(option.get("value") if isinstance(option, dict) else option)
                for option in raw_options
            ]
            if wants_income and {"income", "in"} & set(options):
                values[key] = "income"
            elif wants_expense and {"expense", "out"} & set(options):
                values[key] = "expense"
            elif field.get("required") and options:
                values[key] = options[0]
        elif type_ == "text" and (field.get("required") or key in NOTE_KEYS):
            values[key] = text.strip()[:200]
    return values


def _app_help(models: list[dict[str, Any]]) -> str:
    if not models:
        return (
            "这个应用目前还没有数据模型。你可以对主助手说清楚需要的业务，让它在应用里创建模型。"
        )
    names = "、".join(f"{model['name']}({model['key']})" for model in models)
    return (
        f"当前应用包含这些模型：{names}。\n"
        "离线模式下我用固定规则工作：可以直接告诉我「今天午餐花了 38 元」这类记录，"
        "或者问「这个月一共花了多少」。想完全用自然语言操作，请在「设置」里配置一个云端模型。"
    )


class OfflinePlannerModel:
    """Deterministic planner used when the active profile is ``offline``."""

    name = "offline-planner"

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        names = {spec["function"]["name"] for spec in tools}
        if "create_application" in names:
            return self._primary(messages, names)
        if "list_models" in names:
            return self._application(messages, names)
        return AssistantTurn(
            content="离线模式下我无法回答这个问题。请在「设置」中配置一个模型服务。"
        )

    # ------------------------------------------------------------------
    def _primary(self, messages: list[ChatMessage], names: set[str]) -> AssistantTurn:
        calls = _calls(messages)
        text = _user_text(messages)

        if _failed(calls):
            return AssistantTurn(content=f"创建应用时出错：{_error_text(calls)}")

        if not calls:
            detected = _detect(text)
            if detected is None:
                return AssistantTurn(content=GENERIC_SUMMARY)
            _, meta = detected
            if "create_application" not in names:
                return AssistantTurn(content=GENERIC_SUMMARY)
            return _tool_call("create_application", _create_args(meta, text))

        application = _result_of(calls, "create_application") or {}
        app_id = (application.get("application") or {}).get("id")
        detected = _detect(text)
        if not app_id or detected is None:
            return AssistantTurn(content=GENERIC_SUMMARY)

        plan, meta = detected
        # Only plan steps the primary assistant actually has (it owns structure,
        # not business data), so a missing tool can never stall the workflow.
        steps = [step for step in plan(app_id) if step[0] in names]
        full_plan = [("create_application", {})] + steps
        executed = [call["name"] for call in calls]

        if len(executed) > len(full_plan):
            return AssistantTurn(content=meta["summary"])
        expected = [name for name, _ in full_plan[: len(executed)]]
        if executed != expected or len(executed) == len(full_plan):
            return AssistantTurn(content=meta["summary"])

        name, arguments = full_plan[len(executed)]
        if name not in names:
            return AssistantTurn(content=meta["summary"])
        return _tool_call(name, arguments)

    # ------------------------------------------------------------------
    def _application(
        self, messages: list[ChatMessage], names: set[str]
    ) -> AssistantTurn:
        calls = _calls(messages)
        text = _user_text(messages)
        executed = [call["name"] for call in calls]

        if not executed:
            if "list_models" not in names:
                return AssistantTurn(content="离线模式无法访问应用数据。")
            return _tool_call("list_models", {})

        if _failed(calls):
            return AssistantTurn(content=f"操作没有完成：{_error_text(calls)}")

        models = _models_from_calls(calls)
        wants_stats = any(keyword in text for keyword in STATS_KEYWORDS)
        wants_record = any(keyword in text for keyword in RECORD_KEYWORDS) or bool(
            RECORD_RE.search(text)
        )

        if len(executed) == 1:
            picked = _pick_model(models)
            if wants_stats and picked and "aggregate" in names:
                return _tool_call(
                    "aggregate",
                    {
                        "model_key": picked["key"],
                        "group_by": [],
                        "filters": _expense_filters(picked["model"], text) or [],
                        "aggregations": [
                            {"function": "sum", "field": picked["numeric"], "alias": "total"}
                        ],
                    },
                )
            if wants_record and picked:
                relation = _relation_field(picked["model"], models)
                if relation and "query_records" in names:
                    # Fetch the candidates so the new record can be linked to one.
                    return _tool_call(
                        "query_records",
                        {"model_key": relation["target_model"], "limit": 200},
                    )
                if "create_record" in names:
                    values = _guess_values(text, picked["model"])
                    if values:
                        return _tool_call(
                            "create_record", {"model_key": picked["key"], "values": values}
                        )
            return AssistantTurn(content=_app_help(models))

        if executed == ["list_models", "query_records"]:
            picked = _pick_model(models)
            if picked and wants_record and "create_record" in names:
                relation = _relation_field(picked["model"], models)
                values = _guess_values(text, picked["model"])
                options = _records_from_calls(calls, "query_records")
                matched = _match_relation(relation, options, text) if relation else None
                if relation and matched:
                    values[relation["key"]] = matched
                if values:
                    return _tool_call(
                        "create_record", {"model_key": picked["key"], "values": values}
                    )
            return AssistantTurn(content=_app_help(models))

        last = calls[-1]
        payload = (last.get("result") or {}).get("result") or {}
        if last["name"] == "aggregate":
            items = payload.get("items") or []
            total = items[0].get("total") if items else 0
            return AssistantTurn(content=f"根据当前数据，合计为 {total}。")
        if last["name"] == "create_record":
            return AssistantTurn(
                content=f"已经帮你记录：{payload.get('title') or payload.get('id')}。"
            )
        return AssistantTurn(content=_app_help(models))
