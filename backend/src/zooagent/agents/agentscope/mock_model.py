"""A mock model that speaks AgentScope's interface and generates test data.

Two reasons this exists:

1. **UI testing.** It creates complete applications and fills them with
   hundreds of realistic records (see ``mock_data.py``), so tables, charts,
   pagination and forms can be exercised without any API key.
2. **AgentScope regression testing.** Because it implements
   ``ChatModelBase.__call__``, the *real* AgentScope ReAct loop, Toolkit bridge
   and event translation can be tested offline and deterministically.

It shares its application templates with the offline planner, and only ever
acts through the same tools a real LLM would call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ...config import ModelProfile
from ...ids import new_id
from ..llm.base import AssistantTurn, ChatMessage, ToolCall
from ..templates import GENERIC_SUMMARY, create_args, detect_template
from .convert import text_of, tool_calls_in

try:  # pragma: no cover - depends on the optional extra
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.message import TextBlock, ToolCallBlock
    from agentscope.model import ChatModelBase, ChatResponse

    AGENTSCOPE_MODEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    AGENTSCOPE_MODEL_AVAILABLE = False


MOCK_KEYWORDS = (
    "测试数据",
    "假数据",
    "模拟数据",
    "示例数据",
    "造数据",
    "生成数据",
    "mock",
    "seed data",
)
STATS_KEYWORDS = ("统计", "合计", "一共", "总共", "多少", "总支出", "总收入", "汇总")
RECORD_KEYWORDS = ("花了", "支出", "收入", "收到", "买了", "消费", "付了")
EXPENSE_KEYWORDS = ("花", "支出", "买", "消费", "付")
INCOME_KEYWORDS = ("收入", "收到", "赚", "工资")

DEFAULT_APP_RECORDS = 120
DEFAULT_MODEL_RECORDS = 60
MAX_RECORDS = 2000

COUNT_RE = re.compile(r"(\d{1,4})")


@dataclass
class MockStep:
    """One tool call the mock wants to make."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutedCall:
    """A tool call that already happened, with its (parsed) payload."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    payload: Any = None
    ok: bool = True

    @property
    def failed(self) -> bool:
        return not self.ok


class MockPlanner:
    """Deterministic, schema-driven planner used by the mock provider."""

    def __init__(
        self,
        *,
        seed: int = 7,
        app_records: int = DEFAULT_APP_RECORDS,
        model_records: int = DEFAULT_MODEL_RECORDS,
    ) -> None:
        self.seed = seed
        self.app_records = app_records
        self.model_records = model_records

    # ------------------------------------------------------------------
    def requested_count(self, text: str, fallback: int) -> int:
        match = COUNT_RE.search(text)
        count = int(match.group(1)) if match else fallback
        return max(1, min(count, MAX_RECORDS))

    @staticmethod
    def wants_mock_data(text: str) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in MOCK_KEYWORDS)

    @staticmethod
    def wants_stats(text: str) -> bool:
        return any(keyword in text for keyword in STATS_KEYWORDS)

    @staticmethod
    def wants_record(text: str) -> bool:
        return any(keyword in text for keyword in RECORD_KEYWORDS) or bool(COUNT_RE.search(text))

    # ------------------------------------------------------------------
    def primary_step(
        self, *, text: str, executed: list[ExecutedCall], available: set[str]
    ) -> MockStep | str:
        """Plan for the primary assistant (creates/evolves applications)."""
        for call in executed:
            if call.failed:
                return f"操作没有完成：{call.name} 失败（{call.payload}）"

        if not executed:
            detected = detect_template(text)
            if detected is None:
                return GENERIC_SUMMARY
            _, meta = detected
            if "create_application" not in available:
                return GENERIC_SUMMARY
            return MockStep("create_application", create_args(meta, text))

        application = next(
            (
                call.payload
                for call in executed
                if call.name == "create_application" and isinstance(call.payload, dict)
            ),
            None,
        )
        application_id = ((application or {}).get("application") or {}).get("id")
        detected = detect_template(text)
        if not application_id or detected is None:
            return GENERIC_SUMMARY

        plan, meta = detected
        # The primary assistant only builds structure: no business-data tools,
        # so it never seeds or generates records (even in mock mode).
        steps: list[tuple[str, dict[str, Any]]] = [
            step for step in plan(application_id) if step[0] in available
        ]

        full: list[tuple[str, dict[str, Any]]] = [("create_application", {})] + steps
        names = [call.name for call in executed]
        if len(names) >= len(full):
            return self._primary_summary(meta, executed)
        if names != [name for name, _ in full[: len(names)]]:
            return self._primary_summary(meta, executed)

        name, arguments = full[len(names)]
        if name not in available:
            return self._primary_summary(meta, executed)
        return MockStep(name, arguments)

    def _primary_summary(self, meta: dict[str, Any], executed: list[ExecutedCall]) -> str:
        return meta["summary"]

    # ------------------------------------------------------------------
    def application_step(
        self, *, text: str, executed: list[ExecutedCall], available: set[str]
    ) -> MockStep | str:
        """Plan for an application assistant (business data + stats)."""
        for call in executed:
            if call.failed:
                return f"操作没有完成：{call.name} 失败（{call.payload}）"

        if not executed:
            if "list_models" not in available:
                return "Mock 模式无法访问应用数据。"
            return MockStep("list_models", {})

        models = self._models(executed)
        picked = self._pick_model(models)

        if len(executed) == 1:
            if self.wants_mock_data(text) and picked:
                count = self.requested_count(text, self.model_records)
                relation_target = self._relation_target(picked["model"], models)
                if relation_target and "query_records" in available:
                    return MockStep(
                        "query_records", {"model_key": relation_target, "limit": 200}
                    )
                if "generate_mock_records" in available:
                    return MockStep(
                        "generate_mock_records",
                        {
                            "model_key": picked["key"],
                            "count": count,
                            "seed": self.seed,
                        },
                    )
            if self.wants_stats(text) and picked and "aggregate" in available:
                return MockStep(
                    "aggregate",
                    {
                        "model_key": picked["key"],
                        "group_by": [],
                        "filters": self._expense_filters(picked["model"], text),
                        "aggregations": [
                            {"function": "sum", "field": picked["numeric"], "alias": "total"}
                        ],
                    },
                )
            if self.wants_record(text) and picked and "create_record" in available:
                values = self._guess_record(text, picked["model"], models)
                if values:
                    return MockStep(
                        "create_record", {"model_key": picked["key"], "values": values}
                    )
            return self._app_help(models)

        if len(executed) == 2 and executed[1].name == "query_records" and picked:
            if "generate_mock_records" in available:
                count = self.requested_count(text, self.model_records)
                return MockStep(
                    "generate_mock_records",
                    {
                        "model_key": picked["key"],
                        "count": count,
                        "seed": self.seed,
                    },
                )

        return self._application_summary(executed, models)

    def _application_summary(
        self, executed: list[ExecutedCall], models: list[dict[str, Any]]
    ) -> str:
        last = executed[-1]
        payload = last.payload if isinstance(last.payload, dict) else {}
        if last.name == "generate_mock_records":
            return (
                f"已生成 {payload.get('created', 0)} 条测试数据"
                f"（模型 {payload.get('model')}，失败 {payload.get('failed', 0)} 条）。"
            )
        if last.name == "aggregate":
            items = payload.get("items") or []
            total = items[0].get("total") if items else 0
            return f"根据当前数据，合计为 {total}。"
        if last.name == "create_record":
            return f"已经帮你记录：{payload.get('title') or payload.get('id')}。"
        return self._app_help(models)

    # ------------------------------------------------------------------
    @staticmethod
    def _models(executed: list[ExecutedCall]) -> list[dict[str, Any]]:
        for call in executed:
            if call.name == "list_models" and isinstance(call.payload, dict):
                return call.payload.get("models") or []
        return []

    @staticmethod
    def _pick_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for model in models:
            numeric = next(
                (
                    field
                    for field in model.get("fields", [])
                    if field.get("type") in {"decimal", "integer"}
                ),
                None,
            )
            if numeric is None:
                continue
            candidate = {"key": model["key"], "model": model, "numeric": numeric["key"]}
            if any(field.get("type") == "date" for field in model.get("fields", [])):
                return candidate
            best = best or candidate
        if best:
            return best
        if models:  # fall back to the widest model
            widest = max(models, key=lambda model: len(model.get("fields", [])))
            return {"key": widest["key"], "model": widest, "numeric": None}
        return None

    @staticmethod
    def _relation_target(
        model: dict[str, Any], models: list[dict[str, Any]]
    ) -> str | None:
        known = {item["key"] for item in models}
        for field_def in model.get("fields", []):
            target = field_def.get("target_model")
            if field_def.get("type") == "relation" and target in known:
                return target
        return None

    @staticmethod
    def _expense_filters(model: dict[str, Any], text: str) -> list[dict[str, Any]]:
        if not any(keyword in text for keyword in EXPENSE_KEYWORDS):
            return []
        for field_def in model.get("fields", []):
            if field_def.get("type") != "enum":
                continue
            options = [
                str(option.get("value") if isinstance(option, dict) else option)
                for option in field_def.get("options") or []
            ]
            value = next((item for item in ("expense", "out") if item in options), None)
            if value:
                return [{"field": field_def["key"], "operator": "eq", "value": value}]
        return []

    def _guess_record(
        self, text: str, model: dict[str, Any], models: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Best-effort single record from a sentence like "餐饮 午餐花了 38 元".

        ``models`` is accepted so the caller can pass the sibling models when a
        relation must be resolved; the actual id lookup happens inside the tool,
        which has database access.
        """
        import datetime as dt

        values: dict[str, Any] = {}
        match = COUNT_RE.search(text)
        amount = match.group(1) if match else None
        is_income = any(keyword in text for keyword in INCOME_KEYWORDS) and not any(
            keyword in text for keyword in EXPENSE_KEYWORDS
        )
        for field_def in model.get("fields", []):
            key, type_ = field_def["key"], field_def["type"]
            if type_ == "decimal" and amount and "amount" not in values:
                values[key] = amount
            elif type_ == "integer" and amount:
                values[key] = amount
            elif type_ == "date":
                values[key] = dt.date.today().isoformat()
            elif type_ == "datetime":
                values[key] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            elif type_ == "enum":
                options = [
                    str(option.get("value") if isinstance(option, dict) else option)
                    for option in field_def.get("options") or []
                ]
                preferred = ["income", "in"] if is_income else ["expense", "out"]
                picked = next((item for item in preferred if item in options), None)
                if picked is None and field_def.get("required") and options:
                    picked = options[0]
                if picked:
                    values[key] = picked
            elif type_ == "text" and field_def.get("required"):
                values[key] = text.strip()[:200]
            elif type_ == "text" and any(
                hint in key.lower() for hint in ("note", "remark", "备注", "memo")
            ):
                values[key] = text.strip()[:200]
        return values

    @staticmethod
    def _app_help(models: list[dict[str, Any]]) -> str:
        if not models:
            return "这个应用目前还没有数据模型。可以让主助手先创建模型。"
        names = "、".join(f"{model['name']}({model['key']})" for model in models)
        return (
            f"当前应用包含这些模型：{names}。\n"
            "Mock 模式下我可以：\n"
            "1. 生成测试数据，例如「给交易生成 200 条测试数据」；\n"
            "2. 统计，例如「这个月一共花了多少」；\n"
            "3. 录入单条记录，例如「餐饮 今天午餐花了 38 元」。"
        )


# ----------------------------------------------------------------------
# providers
# ----------------------------------------------------------------------
def _step_to_tool_call(step: MockStep) -> ToolCall:
    return ToolCall(id=new_id("call"), name=step.name, arguments=step.arguments)


def executed_from_our_messages(messages: list[ChatMessage]) -> list[ExecutedCall]:
    """Extract executed tool calls from our lightweight message history."""
    calls: list[ExecutedCall] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                calls.append(ExecutedCall(name=call.name, arguments=call.arguments))
        elif message.role == "tool" and calls:
            payload: Any = message.content
            try:
                envelope = json.loads(message.content or "null")
            except (TypeError, ValueError):
                envelope = None
            if isinstance(envelope, dict) and "ok" in envelope:
                calls[-1].ok = bool(envelope.get("ok"))
                payload = envelope.get("result") if envelope.get("ok") else envelope.get("error")
            calls[-1].payload = payload
    return calls


def executed_from_agentscope(messages: list[Any]) -> list[ExecutedCall]:
    """Extract executed tool calls from AgentScope messages."""
    calls: list[ExecutedCall] = []
    for item in tool_calls_in(messages):
        state = str(item.get("state") or "").lower()
        calls.append(
            ExecutedCall(
                name=item.get("name") or "tool",
                arguments=item.get("arguments") or {},
                payload=item.get("result"),
                ok="error" not in state and "denied" not in state,
            )
        )
    return calls


def last_user_text_from_agentscope(messages: list[Any]) -> str:
    for message in reversed(messages):
        role = getattr(message, "role", None)
        if role == "user":
            text = text_of(message)
            if text.strip():
                return text
    return ""


class MockChatModelLite:
    """Mock provider for the built-in runtime (no AgentScope required)."""

    name = "mock"

    def __init__(self, profile: ModelProfile | None = None, planner: MockPlanner | None = None):
        self.profile = profile
        self.planner = planner or MockPlanner()

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        available = {
            spec["function"]["name"] for spec in tools if "function" in spec
        }
        text = next(
            (
                message.content or ""
                for message in reversed(messages)
                if message.role == "user" and message.content
            ),
            "",
        )
        executed = executed_from_our_messages(messages)
        step = (
            self.planner.primary_step(text=text, executed=executed, available=available)
            if "create_application" in available
            else self.planner.application_step(
                text=text, executed=executed, available=available
            )
        )
        if isinstance(step, str):
            return AssistantTurn(content=step)
        return AssistantTurn(tool_calls=[_step_to_tool_call(step)])


if AGENTSCOPE_MODEL_AVAILABLE:

    class MockChatModel(ChatModelBase):
        """Mock model implementing AgentScope's chat model interface.

        It is deterministic and offline, which makes the whole AgentScope path
        (ReAct loop -> Toolkit -> engines) continuously testable.
        """

        class Parameters(ChatModelBase.Parameters):  # type: ignore[misc]
            seed: int = 7
            app_records: int = DEFAULT_APP_RECORDS
            model_records: int = DEFAULT_MODEL_RECORDS

        def __init__(
            self,
            credential: Any = None,
            model: str = "mock-agent",
            parameters: Any = None,
            stream: bool = False,
            max_retries: int = 1,
            retry_delay: float = 0.0,
            context_size: int = 32768,
            formatter: Any = None,
        ) -> None:
            resolved = parameters or self.Parameters()
            super().__init__(
                credential=credential if credential is not None else _mock_credential(),
                model=model,
                parameters=resolved,
                stream=False,
                max_retries=max_retries,
                retry_delay=retry_delay,
                context_size=context_size,
            )
            # AgentScope validates incoming messages against the model's
            # formatter (e.g. supported media types), so a model must expose one.
            self.formatter = formatter if formatter is not None else OpenAIChatFormatter()
            self.planner = MockPlanner(
                seed=getattr(resolved, "seed", 7),
                app_records=getattr(resolved, "app_records", DEFAULT_APP_RECORDS),
                model_records=getattr(resolved, "model_records", DEFAULT_MODEL_RECORDS),
            )

        async def __call__(
            self,
            messages: list[Any] | None = None,
            tools: list[dict[str, Any]] | None = None,
            tool_choice: Any = None,
            **kwargs: Any,
        ) -> Any:
            available = {
                spec["function"]["name"]
                for spec in (tools or [])
                if isinstance(spec, dict) and "function" in spec
            }
            conversation = list(messages or [])
            text = last_user_text_from_agentscope(conversation)
            executed = executed_from_agentscope(conversation)
            step = (
                self.planner.primary_step(text=text, executed=executed, available=available)
                if "create_application" in available
                else self.planner.application_step(
                    text=text, executed=executed, available=available
                )
            )
            if isinstance(step, str):
                return ChatResponse(content=[TextBlock(type="text", text=step)], is_last=True)
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=new_id("call"),
                        name=step.name,
                        input=json.dumps(step.arguments, ensure_ascii=False),
                    )
                ],
                is_last=True,
            )


def _mock_credential() -> Any:
    """A throwaway credential so AgentScope bookkeeping has something to hold."""
    try:  # pragma: no cover - trivial
        from agentscope.credential import OpenAICredential

        return OpenAICredential(api_key="mock", base_url="http://mock.invalid/v1")
    except Exception:  # pragma: no cover - defensive
        return None


def build_mock_planner(profile: ModelProfile | None = None) -> MockPlanner:
    return MockPlanner()


__all__ = [
    "AGENTSCOPE_MODEL_AVAILABLE",
    "MockChatModel",
    "MockChatModelLite",
    "MockPlanner",
    "MockStep",
    "ExecutedCall",
]
