"""Workspace layout and settings.

zooagent is local-first: everything lives under a single workspace directory
that is resolved per-platform (never hard-coded), so the same business core
runs on macOS, Windows and Linux.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .errors import ConfigurationError

APP_NAME = "zooagent"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OFFLINE_PROFILE_ID = "profile_offline"
MOCK_PROFILE_ID = "profile_mock"
DEFAULT_PROFILE_ID = OFFLINE_PROFILE_ID


def default_workspace_root() -> Path:
    override = os.environ.get("ZOOAGENT_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":  # pragma: no cover - windows only
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


class ModelProfile(BaseModel):
    """A configured LLM endpoint.

    ``provider`` selects *how* the model is driven:

    * ``agentscope`` - AgentScope 的 ReAct Agent + LLM 接入层（真实 LLM 的唯一路径，
      openai/deepseek/moonshot/dashscope/ollama/anthropic 都由 ``model_family`` 区分）
    * ``mock`` - AgentScope 接口的 mock 模型，产出测试数据，不联网
    * ``offline`` - 内置模板规划器（确定性规则，不联网）

    ``api_key`` is kept in the workspace settings file (mode 0600) for the
    first version. Moving it to the OS keychain is tracked in docs/adr.
    """

    id: str
    name: str
    provider: Literal["agentscope", "offline", "mock"] = "agentscope"
    base_url: str = DEFAULT_OPENAI_BASE_URL
    model_name: str = "gpt-4o-mini"
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    #: AgentScope provider family (openai/deepseek/moonshot/dashscope/ollama/anthropic).
    #: Empty means "infer from base_url and model name".
    model_family: str | None = None
    #: AgentScope uses this for context compression.
    context_size: int = 32768

    @property
    def is_offline(self) -> bool:
        return self.provider == "offline"

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"


class Settings(BaseModel):
    active_model_profile_id: str = DEFAULT_PROFILE_ID
    model_profiles: list[ModelProfile] = Field(default_factory=list)
    language: str = "zh-CN"
    theme: str = "system"
    telemetry_enabled: bool = False


#: 旧版本里 ``openai`` / ``ollama`` 走的是自研循环，现在真实 LLM 一律经 AgentScope。
#: 读到这种配置就就地改写，用户不用重新填一遍（密钥、base_url、模型名全部保留）。
LEGACY_PROVIDER_ALIASES = {"openai": "agentscope", "ollama": "agentscope"}


def _migrate_legacy_providers(raw: dict[str, object]) -> bool:
    """把老配置里的 provider 改成 ``agentscope``，返回是否改动过。"""
    profiles = raw.get("model_profiles")
    if not isinstance(profiles, list):
        return False
    changed = False
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        legacy = profile.get("provider")
        target = LEGACY_PROVIDER_ALIASES.get(legacy) if isinstance(legacy, str) else None
        if target is None:
            continue
        profile["provider"] = target
        # ollama 的 base_url 在旧预设里带 /v1，靠 family 显式声明最稳。
        if legacy == "ollama" and not profile.get("model_family"):
            profile["model_family"] = "ollama"
        changed = True
    return changed


def default_settings() -> Settings:
    return Settings(
        active_model_profile_id=OFFLINE_PROFILE_ID,
        model_profiles=[
            ModelProfile(
                id=OFFLINE_PROFILE_ID,
                name="离线模式（内置模板规划器）",
                provider="offline",
                base_url="",
                model_name="offline-planner",
            ),
            ModelProfile(
                id=MOCK_PROFILE_ID,
                name="Mock 模式（AgentScope 接口，生成测试数据）",
                provider="mock",
                base_url="",
                model_name="mock-agent",
            ),
        ],
    )


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def system_dir(self) -> Path:
        return self.root / "system"

    @property
    def system_db(self) -> Path:
        return self.system_dir / "platform.db"

    @property
    def applications_dir(self) -> Path:
        return self.root / "applications"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def session_file(self) -> Path:
        return self.runtime_dir / "session.json"

    def application_dir(self, application_id: str) -> Path:
        return self.applications_dir / application_id

    def application_db(self, application_id: str) -> Path:
        return self.application_dir(application_id) / "data.db"

    def application_config(self, application_id: str) -> Path:
        return self.application_dir(application_id) / "application.json"

    def ensure(self) -> None:
        for path in (
            self.root,
            self.config_dir,
            self.system_dir,
            self.applications_dir,
            self.logs_dir,
            self.runtime_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resolve(cls, root: str | Path | None = None) -> WorkspacePaths:
        paths = cls(Path(root).expanduser() if root else default_workspace_root())
        paths.ensure()
        return paths


class SettingsService:
    def __init__(self, paths: WorkspacePaths) -> None:
        self._paths = paths
        self._cached: Settings | None = None

    def load(self) -> Settings:
        if self._cached is not None:
            return self._cached
        file = self._paths.settings_file
        if not file.exists():
            settings = default_settings()
            self.save(settings)
            return settings
        try:
            raw = json.loads(file.read_text("utf-8"))
        except (OSError, ValueError) as exc:  # pragma: no cover - corrupted file
            raise ConfigurationError(f"无法解析配置文件: {file}") from exc
        migrated = isinstance(raw, dict) and _migrate_legacy_providers(raw)
        settings = Settings.model_validate(raw)
        if migrated:  # 把文件一次性升级成新写法，避免每次启动都改写
            return self.save(settings)
        self._cached = settings
        return settings

    def save(self, settings: Settings) -> Settings:
        file = self._paths.settings_file
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), "utf-8"
        )
        try:
            os.chmod(file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - best effort
            pass
        self._cached = settings
        return settings

    def active_profile(self) -> ModelProfile:
        settings = self.load()
        for profile in settings.model_profiles:
            if profile.id == settings.active_model_profile_id:
                return profile
        if not settings.model_profiles:
            raise ConfigurationError("尚未配置任何模型")
        return settings.model_profiles[0]

    def upsert_profile(self, profile: ModelProfile) -> Settings:
        settings = self.load()
        profiles = [p for p in settings.model_profiles if p.id != profile.id]
        profiles.append(profile)
        settings.model_profiles = profiles
        return self.save(settings)

    def delete_profile(self, profile_id: str) -> Settings:
        settings = self.load()
        if len([p for p in settings.model_profiles if p.id != profile_id]) == 0:
            raise ConfigurationError("至少需要保留一个模型配置")
        settings.model_profiles = [p for p in settings.model_profiles if p.id != profile_id]
        if settings.active_model_profile_id == profile_id:
            settings.active_model_profile_id = settings.model_profiles[0].id
        return self.save(settings)
