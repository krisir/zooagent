"""Wire an agent (prompt + tools + model profile + loop) for a request.

AgentScope 的导入是惰性的：两边都是真实 LLM 的唯一路径，但只用 offline/mock 的
用户不需要为它付启动时间（未安装 extra 时导入会失败，这里要能容忍）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..application.conversations import ConversationStore
from ..application.service import Agent, ApplicationService
from ..config import ModelProfile, SettingsService
from ..errors import ConfigurationError
from .llm.offline import OfflinePlannerModel
from .messaging import application_message_tool, primary_message_tool
from .mini_loop import AgentLoopProtocol, MiniLoop, to_history_messages
from .prompts import (
    ACTION_DIAGNOSIS_PROMPT,
    application_system_prompt,
    assistant_message_prompt,
    primary_system_prompt,
)
from .tools import build_application_tools, build_primary_tools


def describe_runtime(profile: ModelProfile) -> str:
    """Human-readable name of the runtime a profile will use (shown in the UI)."""
    if profile.provider == "agentscope":
        return "AgentScope"
    if profile.provider == "mock":
        return "AgentScope (mock)" if _agentscope_available() else "mock"
    return "offline"


def _agentscope_available() -> bool:
    from .agentscope.models import AGENTSCOPE_AVAILABLE

    return bool(AGENTSCOPE_AVAILABLE)


@dataclass
class PreparedAgent:
    runtime: AgentLoopProtocol
    agent: Agent
    conversation_rows: list


class AgentFactory:
    def __init__(
        self,
        service: ApplicationService,
        settings: SettingsService,
        conversations: ConversationStore,
    ) -> None:
        self.service = service
        self.settings = settings
        self.conversations = conversations

    # ------------------------------------------------------------------
    def _runtime(
        self,
        *,
        registry,
        system_prompt: str,
        max_iterations: int,
    ) -> AgentLoopProtocol:
        """Pick the loop: AgentScope for every real LLM, MiniLoop for deterministic models."""
        profile = self.settings.active_profile()

        if profile.provider == "mock":
            from .agentscope.mock_model import (
                AGENTSCOPE_MODEL_AVAILABLE,
                MockChatModel,
                MockChatModelLite,
            )

            if _agentscope_available() and AGENTSCOPE_MODEL_AVAILABLE:
                from .agentscope.runtime import AgentScopeRuntime

                return AgentScopeRuntime(
                    model=MockChatModel(),
                    registry=registry,
                    system_prompt=system_prompt,
                    max_iterations=max_iterations,
                )
            return MiniLoop(
                chat_model=MockChatModelLite(profile),
                registry=registry,
                system_prompt=system_prompt,
                max_iterations=max_iterations,
            )

        if profile.provider == "offline":
            return MiniLoop(
                chat_model=OfflinePlannerModel(),
                registry=registry,
                system_prompt=system_prompt,
                max_iterations=max_iterations,
            )

        # 真实 LLM 只有一条路：AgentScope 的 ReAct Agent。
        if not _agentscope_available():
            raise ConfigurationError(
                "该模型配置需要 AgentScope 运行时，请先安装可选依赖：uv sync --extra agentscope"
            )
        from .agentscope.models import build_chat_model as build_agentscope_model
        from .agentscope.runtime import AgentScopeRuntime

        return AgentScopeRuntime(
            model=build_agentscope_model(profile),
            registry=registry,
            system_prompt=system_prompt,
            max_iterations=max_iterations,
        )

    # ------------------------------------------------------------------
    def primary(self) -> PreparedAgent:
        agent = self.service.get_primary_agent()
        registry = build_primary_tools(self.service)
        # 主助手可以主动联系某个应用助手（答复以折叠卡片回来）。
        application_message_tool(registry, self.service)
        applications = [
            {"id": app.id, "name": app.name, "description": app.description}
            for app in self.service.list_applications()
        ]
        runtime = self._runtime(
            registry=registry,
            system_prompt=primary_system_prompt(applications),
            max_iterations=30,
        )
        return PreparedAgent(runtime=runtime, agent=agent, conversation_rows=[])

    def application(
        self, application_id: str, *, with_messaging: bool = True
    ) -> PreparedAgent:
        """Wire one application assistant.

        ``with_messaging=False`` is used when the primary assistant is the one
        talking: a receiver must answer directly, never bounce the message back
        (that would let two assistants ping-pong forever).
        """
        agent = self.service.get_agent(application_id)
        if agent is None:
            raise ValueError(f"应用 {application_id} 没有专属助手")
        with self.service.open_app(application_id) as ctx:
            models = [model.to_prompt_dict() for model in ctx.models.list_models(ctx.conn)]
            actions = [
                action.to_prompt_dict() for action in ctx.actions.list_actions(ctx.conn)
            ]
        registry = build_application_tools(self.service, application_id)
        if with_messaging:
            primary_message_tool(registry)
        prompt = application_system_prompt(
            application_id=application_id,
            application_name=agent.name,
            application_description=agent.greeting,
            models=models,
            actions=actions,
            instructions=agent.instructions,
        )
        runtime = self._runtime(registry=registry, system_prompt=prompt, max_iterations=16)
        return PreparedAgent(runtime=runtime, agent=agent, conversation_rows=[])

    def for_diagnosis(self) -> PreparedAgent:
        """A bounded, read-only primary run for diagnosing a failed Action.

        Only structural read tools are exposed: a diagnosis must never be able
        to repair or write anything. Repairs go through the normal
        ``define_action`` path after the user confirms.
        """
        agent = self.service.get_primary_agent()
        registry = build_primary_tools(self.service).subset(
            {"get_application_overview", "list_applications"}
        )
        runtime = self._runtime(
            registry=registry,
            system_prompt=ACTION_DIAGNOSIS_PROMPT,
            max_iterations=6,
        )
        return PreparedAgent(runtime=runtime, agent=agent, conversation_rows=[])

    def for_messaging(self, sender: dict) -> PreparedAgent:
        """A bounded, **read-only** primary run for a relayed request.

        The primary is contacted by an application assistant, whose context may
        contain business data; it may look at structure and write a plan, but it
        must not change anything here. The user confirms in the primary
        conversation, where the normal write tools live.
        """
        agent = self.service.get_primary_agent()
        registry = build_primary_tools(self.service).subset(
            {"list_applications", "get_application_overview"}
        )
        runtime = self._runtime(
            registry=registry,
            system_prompt=assistant_message_prompt(sender),
            max_iterations=8,
        )
        return PreparedAgent(runtime=runtime, agent=agent, conversation_rows=[])

    def history(self, conversation_id: str) -> list:
        return to_history_messages(self.conversations.history(conversation_id))
