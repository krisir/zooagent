"""Map zooagent model profiles onto AgentScope models and credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...config import ModelProfile
from ...errors import ConfigurationError

try:  # pragma: no cover - exercised only without the extra installed
    from agentscope.credential import (
        AnthropicCredential,
        DashScopeCredential,
        DeepSeekCredential,
        MoonshotCredential,
        OllamaCredential,
        OpenAICredential,
    )
    from agentscope.model import (
        AnthropicChatModel,
        DashScopeChatModel,
        DeepSeekChatModel,
        MoonshotChatModel,
        OllamaChatModel,
        OpenAIChatModel,
    )

    AGENTSCOPE_AVAILABLE = True
    AGENTSCOPE_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover
    AGENTSCOPE_AVAILABLE = False
    AGENTSCOPE_IMPORT_ERROR = str(exc)


def require_agentscope() -> None:
    if not AGENTSCOPE_AVAILABLE:
        raise ConfigurationError(
            "使用 AgentScope 需要安装可选依赖：uv sync --extra agentscope"
            f"（导入失败：{AGENTSCOPE_IMPORT_ERROR}）"
        )


@dataclass(frozen=True)
class ModelFamily:
    """One AgentScope provider: its credential, model class and defaults."""

    key: str
    label: str
    credential: Any
    model: Any
    default_base_url: str
    example_models: tuple[str, ...]


def _families() -> dict[str, ModelFamily]:
    if not AGENTSCOPE_AVAILABLE:  # pragma: no cover
        return {}
    return {
        "openai": ModelFamily(
            key="openai",
            label="OpenAI",
            credential=OpenAICredential,
            model=OpenAIChatModel,
            default_base_url="https://api.openai.com/v1",
            example_models=("gpt-4o-mini", "gpt-4o"),
        ),
        "deepseek": ModelFamily(
            key="deepseek",
            label="DeepSeek",
            credential=DeepSeekCredential,
            model=DeepSeekChatModel,
            default_base_url="https://api.deepseek.com/v1",
            example_models=("deepseek-chat", "deepseek-reasoner"),
        ),
        "moonshot": ModelFamily(
            key="moonshot",
            label="Moonshot / Kimi",
            credential=MoonshotCredential,
            model=MoonshotChatModel,
            default_base_url="https://api.moonshot.cn/v1",
            example_models=("moonshot-v1-32k",),
        ),
        "dashscope": ModelFamily(
            key="dashscope",
            label="通义千问 (DashScope)",
            credential=DashScopeCredential,
            model=DashScopeChatModel,
            default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            example_models=("qwen-plus", "qwen-max"),
        ),
        "ollama": ModelFamily(
            key="ollama",
            label="Ollama（本机）",
            credential=OllamaCredential,
            model=OllamaChatModel,
            default_base_url="http://127.0.0.1:11434",
            example_models=("qwen2.5:7b", "llama3.1:8b"),
        ),
        "anthropic": ModelFamily(
            key="anthropic",
            label="Anthropic",
            credential=AnthropicCredential,
            model=AnthropicChatModel,
            default_base_url="https://api.anthropic.com",
            example_models=("claude-sonnet-4-5",),
        ),
    }


MODEL_FAMILIES: dict[str, ModelFamily] = _families()

_BASE_URL_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deepseek", ("deepseek",)),
    ("moonshot", ("moonshot", "kimi")),
    ("dashscope", ("dashscope", "aliyuncs")),
    ("anthropic", ("anthropic",)),
    ("ollama", ("ollama", "11434")),
)

_MODEL_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deepseek", ("deepseek",)),
    ("moonshot", ("moonshot", "kimi")),
    ("anthropic", ("claude",)),
    ("dashscope", ("qwen",)),
)


def resolve_family(profile: ModelProfile) -> str:
    """Pick the AgentScope family from the profile.

    Order matters: an explicit ``model_family`` wins, then endpoint hints, then
    model-name hints, and finally an OpenAI-compatible endpoint - which is the
    safest default because most vendors expose that API shape.
    """
    if profile.model_family and profile.model_family in MODEL_FAMILIES:
        return profile.model_family

    base_url = (profile.base_url or "").lower()
    for family, hints in _BASE_URL_HINTS:
        if any(hint in base_url for hint in hints):
            return family

    model_name = (profile.model_name or "").lower()
    for family, hints in _MODEL_HINTS:
        if any(hint in model_name for hint in hints):
            return family

    return "openai"


def _build_credential(family: ModelFamily, profile: ModelProfile) -> Any:
    candidates: dict[str, Any] = {
        "api_key": profile.api_key or "not-needed",
        "base_url": profile.base_url or family.default_base_url,
    }
    allowed = set(family.credential.model_fields)
    return family.credential(**{key: value for key, value in candidates.items() if key in allowed})


def _build_parameters(family: ModelFamily, profile: ModelProfile) -> Any:
    """Only pass the parameters this provider actually supports."""
    allowed = set(family.model.Parameters.model_fields)
    wanted = {
        "temperature": profile.temperature,
        "max_tokens": profile.max_tokens,
    }
    return family.model.Parameters(**{key: value for key, value in wanted.items() if key in allowed})


def build_chat_model(profile: ModelProfile, *, stream: bool = False) -> Any:
    """Create the AgentScope chat model for a profile.

    Defaults to ``stream=False`` so callers get one complete ``ChatResponse``
    instead of an async generator.
    """
    require_agentscope()
    family = MODEL_FAMILIES[resolve_family(profile)]
    if not profile.model_name:
        raise ConfigurationError("模型配置缺少 model_name")
    return family.model(
        credential=_build_credential(family, profile),
        model=profile.model_name,
        parameters=_build_parameters(family, profile),
        stream=stream,
        context_size=profile.context_size,
    )


__all__ = [
    "AGENTSCOPE_AVAILABLE",
    "MODEL_FAMILIES",
    "ModelFamily",
    "build_chat_model",
    "require_agentscope",
    "resolve_family",
]
