"""Process-wide application state, shared by the HTTP layer and the CLI."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from .actions.engine import ActionEngine
from .agents.diagnosis import ActionDiagnoser
from .agents.factory import AgentFactory
from .agents.messaging import AgentMessenger
from .application.conversations import ConversationStore
from .application.service import ApplicationService
from .config import SettingsService, WorkspacePaths
from .data.engine import DataEngine
from .models.engine import ModelEngine
from .persistence.sqlite import init_system_db
from .ui_schema.service import UISchemaService

SESSION_FILE_NAME = "session.json"


@dataclass
class AppState:
    paths: WorkspacePaths
    token: str
    settings: SettingsService
    models: ModelEngine
    data: DataEngine
    actions: ActionEngine
    ui: UISchemaService
    service: ApplicationService
    conversations: ConversationStore
    agents: AgentFactory
    diagnosis: ActionDiagnoser
    messaging: AgentMessenger

    @classmethod
    def create(cls, root: str | Path | None = None, token: str | None = None) -> AppState:
        paths = WorkspacePaths.resolve(root)
        init_system_db(paths.system_db)

        settings = SettingsService(paths)
        models = ModelEngine()
        data = DataEngine(models)
        actions = ActionEngine(models, data)
        ui = UISchemaService(models, actions)
        service = ApplicationService(paths, models, data, actions, ui)
        conversations = ConversationStore(paths)
        agents = AgentFactory(service, settings, conversations)
        diagnosis = ActionDiagnoser(service, conversations, agents, settings)
        messaging = AgentMessenger(service, conversations, agents)

        state = cls(
            paths=paths,
            token=token or secrets.token_urlsafe(24),
            settings=settings,
            models=models,
            data=data,
            actions=actions,
            ui=ui,
            service=service,
            conversations=conversations,
            agents=agents,
            diagnosis=diagnosis,
            messaging=messaging,
        )
        service.ensure_primary_agent()
        state.write_session()
        return state

    def write_session(self, port: int | None = None) -> None:
        """Persist port/token for the desktop host and for local debugging."""
        import json

        payload: dict[str, object] = {
            "token": self.token,
            "workspace": str(self.paths.root),
        }
        if port is not None:
            payload["port"] = port
            payload["base_url"] = f"http://127.0.0.1:{port}"
        file = self.paths.session_file
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(payload, indent=2), "utf-8")
