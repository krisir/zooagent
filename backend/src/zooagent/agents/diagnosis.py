"""Automatic diagnosis of failed Actions, handed to the primary assistant.

An Action is composed by the AI and then runs deterministically. When it
breaks, the failure is a *structural* problem the primary assistant owns - so
the platform does this:

1. capture the failure snapshot (definition, inputs, failing step, trace);
2. run the deterministic inspection in ``actions.diagnostics``;
3. optionally ask the primary assistant (read-only tools) for a second opinion;
4. store the report in the primary conversation, and relay a short version to
   the application conversation that hit the failure.

The primary assistant never repairs anything here: the report is a proposal.
The user confirms in the primary conversation, and only then is
``define_action`` called (the normal, validated path).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..actions.diagnostics import (
    Inspection,
    inspect_failure,
    render_application_relay,
    render_primary_report,
)
from ..actions.schema import ActionDef
from ..config import SettingsService
from ..errors import ZooAgentError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..application.conversations import ConversationStore
    from ..application.service import ApplicationService
    from .factory import AgentFactory

logger = logging.getLogger(__name__)

#: Providers that cannot reason about a diagnostic report (and are offline).
OFFLINE_PROVIDERS = {"offline", "mock"}

#: Give a real model a bounded window; a diagnosis is a side task.
DIAGNOSIS_TIMEOUT_SECONDS = 90.0


class ActionDiagnoser:
    def __init__(
        self,
        service: ApplicationService,
        conversations: ConversationStore,
        agents: AgentFactory,
        settings: SettingsService,
    ) -> None:
        self.service = service
        self.conversations = conversations
        self.agents = agents
        self.settings = settings
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # scheduling (page buttons: the response must not wait for a model)
    # ------------------------------------------------------------------
    def schedule(
        self,
        failure: dict[str, Any],
        *,
        relay_application_id: str | None = None,
        relay_conversation_id: str | None = None,
    ) -> None:
        task = asyncio.create_task(
            self.diagnose(
                failure,
                relay_application_id=relay_application_id,
                relay_conversation_id=relay_conversation_id,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Wait for scheduled diagnoses (used by tests and graceful shutdown)."""
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=timeout)

    # ------------------------------------------------------------------
    # diagnosis
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        failure: dict[str, Any],
        *,
        relay_application_id: str | None = None,
        relay_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Inspect a failure, record the report, return the structured result.

        Never raises: a broken diagnosis must not hide the original failure.
        """
        application_id = str(
            (failure.get("application") or {}).get("id") or ""
        )
        try:
            inspection = self._inspect(failure, application_id)
            llm_text = await self._ask_primary(failure, inspection)
            if llm_text:
                proposal = _extract_proposal(llm_text, failure)
                if proposal is not None:
                    inspection.proposal = proposal
                    inspection.suggestions.append(
                        "按主助手给出的完整定义替换后，这个 Action 会以新版本发布，历史数据不受影响。"
                    )
            inspection.finalize()
            primary_message = self._store_primary(failure, inspection, llm_text)
            relay_message = self._store_relay(
                failure,
                inspection,
                application_id=relay_application_id,
                conversation_id=relay_conversation_id,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("action diagnosis failed")
            return {"ok": False, "error": "诊断过程出错", "relay": None}

        return {
            "ok": True,
            "summary": inspection.summary,
            "definition_problem": inspection.is_definition_problem,
            "has_proposal": inspection.proposal is not None,
            "category": inspection.category,
            "findings": [item.to_dict() for item in inspection.findings],
            "suggestions": inspection.suggestions,
            "primary_message_id": primary_message["id"],
            "relay_message_id": relay_message["id"] if relay_message else None,
            "relay": relay_message["content"] if relay_message else None,
        }

    def _inspect(
        self, failure: dict[str, Any], application_id: str
    ) -> Inspection:
        models = []
        if application_id:
            try:
                with self.service.open_app(application_id) as ctx:
                    models = ctx.models.list_models(ctx.conn)
            except ZooAgentError:  # pragma: no cover - app deleted mid-flight
                logger.warning("diagnosis: application %s unavailable", application_id)
        return inspect_failure(failure, models)

    # ------------------------------------------------------------------
    # optional second opinion from the primary assistant
    # ------------------------------------------------------------------
    async def _ask_primary(
        self, failure: dict[str, Any], inspection: Inspection
    ) -> str | None:
        profile = self.settings.active_profile()
        if profile.provider in OFFLINE_PROVIDERS:
            return None
        try:
            prepared = self.agents.for_diagnosis()
            report = _diagnosis_prompt(failure, inspection)

            async def run() -> str | None:
                chunks: list[str] = []
                async for event in prepared.runtime.stream(
                    history=[], user_text=report
                ):
                    if event.get("type") == "assistant_message":
                        chunks.append(str(event.get("content") or ""))
                    elif event.get("type") == "error":
                        logger.warning(
                            "primary diagnosis model error: %s", event.get("message")
                        )
                        return None
                return "\n".join(chunk for chunk in chunks if chunk).strip() or None

            return await asyncio.wait_for(
                run(), timeout=DIAGNOSIS_TIMEOUT_SECONDS
            )
        except TimeoutError:  # pragma: no cover - depends on provider
            logger.warning("primary diagnosis timed out")
            return None
        except Exception:  # pragma: no cover - provider surprises
            logger.exception("primary diagnosis failed")
            return None

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _store_primary(
        self,
        failure: dict[str, Any],
        inspection: Inspection,
        llm_text: str | None,
    ) -> dict[str, Any]:
        agent = self.service.get_primary_agent()
        conversation = self.conversations.get_or_create(agent.id)
        action = failure.get("action") or {}
        application = failure.get("application") or {}
        report = render_primary_report(
            failure,
            inspection,
            model_text=(
                "**主助手结论**\n\n" + llm_text
                if llm_text
                else "**主助手结论**\n\n- 当前模型未配置或为离线模式，以上是自动检查结果。"
            ),
        )
        return self.conversations.append(
            conversation.id,
            "assistant",
            report,
            {
                "kind": "action_diagnosis",
                "application_id": application.get("id"),
                "application_name": application.get("name"),
                "action_key": action.get("key"),
                "action_name": action.get("name"),
                "action_version": action.get("version"),
                "failure": failure,
                "category": inspection.category,
                "definition_problem": inspection.is_definition_problem,
                "proposal": inspection.proposal,
                "source": "automatic",
            },
        )

    def _store_relay(
        self,
        failure: dict[str, Any],
        inspection: Inspection,
        *,
        application_id: str | None,
        conversation_id: str | None,
    ) -> dict[str, Any] | None:
        if not application_id:
            return None
        try:
            if conversation_id:
                conversation = self.conversations.get(conversation_id)
            else:
                agent = self.service.get_agent(application_id)
                if agent is None:
                    return None
                conversation = self.conversations.get_or_create(
                    agent.id, application_id
                )
        except ZooAgentError:  # pragma: no cover - defensive
            return None
        action = failure.get("action") or {}
        return self.conversations.append(
            conversation.id,
            "assistant",
            render_application_relay(failure, inspection),
            {
                "kind": "action_diagnosis_relay",
                "action_key": action.get("key"),
                "category": inspection.category,
            },
        )


# ----------------------------------------------------------------------
# prompt helpers
# ----------------------------------------------------------------------
def _diagnosis_prompt(failure: dict[str, Any], inspection: Inspection) -> str:
    application = failure.get("application") or {}
    return f"""一次 Action 执行失败了，请做一次故障排查（不是创建应用）。

# 失败快照
应用：{application.get('name') or application.get('id')}（{application.get('id')}）
失败步骤：{json.dumps(failure.get('step'), ensure_ascii=False)}
错误：{json.dumps(failure.get('error'), ensure_ascii=False, default=str)}
输入：{json.dumps(failure.get('inputs'), ensure_ascii=False, default=str)}

Action 定义：
```json
{json.dumps(failure.get('action'), ensure_ascii=False, indent=2, default=str)}
```

执行 trace（失败前的步骤）：
```json
{json.dumps(failure.get('trace'), ensure_ascii=False, indent=2, default=str)}
```

# 自动检查已经发现
{json.dumps([item.to_dict() for item in inspection.findings], ensure_ascii=False, indent=2)}

# 你的任务
1. 用 get_application_overview 核对模型字段（key、类型、必填、enum、relation 目标）。
2. 判断这是「Action 定义写错」、「本次输入/数据不满足」还是「环境问题」。
3. 如果有确定的定义修复，给出**完整**的 Action JSON（key 保持不变，步骤完整）。

# 输出格式
结论：定义问题 / 输入数据问题 / 环境问题
原因：一句话说明根因
证据：
- 具体到步骤、字段、引用路径
修复建议：
1. 可执行的改动
ACTION_JSON:
```json
（仅当有确定修复时输出完整 Action JSON；否则写 null）
```

不要调用 define_action，不要修改任何数据。用户确认后才会应用修复。"""


def _extract_proposal(text: str, failure: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a complete Action definition out of the model's answer, if valid.

    Only an Action with the same key is accepted: a diagnosis may repair a
    definition, it may not rename or replace the Action the user's button and
    pages already reference.
    """
    expected = str((failure.get("action") or {}).get("key") or "")
    for match in re.finditer(r"```json\s*(.*?)```", text, re.DOTALL):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or "steps" not in payload:
            continue
        try:
            action = ActionDef.model_validate(payload)
        except ValidationError:
            continue
        if expected and action.key != expected:
            continue
        return action.model_dump()
    return None


__all__ = ["ActionDiagnoser", "OFFLINE_PROVIDERS"]
