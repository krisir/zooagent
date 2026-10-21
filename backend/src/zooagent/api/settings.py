"""Settings: model profiles, provider presets and connectivity test."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from ..config import ModelProfile
from ..errors import ProviderError, ValidationFailure
from ..ids import new_id
from ..state import AppState
from .deps import get_state, require_token

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])

SECRET_MASK = "***"

PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "key": "agentscope-deepseek",
        "label": "DeepSeek（AgentScope）",
        "provider": "agentscope",
        "model_family": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    {
        "key": "agentscope-openai",
        "label": "OpenAI（AgentScope）",
        "provider": "agentscope",
        "model_family": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
    },
    {
        "key": "agentscope-moonshot",
        "label": "Moonshot / Kimi（AgentScope）",
        "provider": "agentscope",
        "model_family": "moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-32k"],
    },
    {
        "key": "agentscope-dashscope",
        "label": "通义千问（AgentScope）",
        "provider": "agentscope",
        "model_family": "dashscope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max"],
    },
    {
        "key": "agentscope-ollama",
        "label": "Ollama（AgentScope，本机）",
        "provider": "agentscope",
        "model_family": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
    },
    {
        "key": "agentscope-anthropic",
        "label": "Anthropic（AgentScope）",
        "provider": "agentscope",
        "model_family": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-sonnet-4-5"],
    },
    {
        "key": "openai",
        "label": "OpenAI 兼容（内置运行时）",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
    },
    {
        "key": "deepseek",
        "label": "DeepSeek（内置运行时）",
        "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat"],
    },
    {
        "key": "ollama",
        "label": "Ollama（内置运行时，本机）",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
    },
    {
        "key": "mock",
        "label": "Mock（AgentScope 接口，生成测试数据）",
        "provider": "mock",
        "base_url": "",
        "models": ["mock-agent"],
    },
    {
        "key": "offline",
        "label": "离线模式（内置模板规划器）",
        "provider": "offline",
        "base_url": "",
        "models": ["offline-planner"],
    },
]


def _key_hint(key: str | None) -> str:
    """Last 4 characters of a saved key — enough to recognise it, nothing more."""
    if not key:
        return ""
    return key[-4:] if len(key) >= 4 else "…"


def _masked(profile: ModelProfile) -> dict[str, Any]:
    """Serialize a profile without ever putting the secret on the wire."""
    payload = profile.model_dump()
    payload.pop("api_key", None)
    payload["api_key_set"] = bool(profile.api_key)
    payload["api_key_last4"] = _key_hint(profile.api_key)
    return payload


@router.get("")
def get_settings(state: AppState = Depends(get_state)) -> dict[str, Any]:
    settings = state.settings.load()
    return {
        "active_model_profile_id": settings.active_model_profile_id,
        "language": settings.language,
        "theme": settings.theme,
        "profiles": [_masked(profile) for profile in settings.model_profiles],
        "providers": PROVIDER_PRESETS,
    }


@router.post("/profiles")
def upsert_profile(
    payload: dict[str, Any] = Body(...), state: AppState = Depends(get_state)
) -> dict[str, Any]:
    settings = state.settings.load()
    profile_id = payload.get("id") or new_id("profile")
    existing = next(
        (profile for profile in settings.model_profiles if profile.id == profile_id), None
    )

    api_key = payload.get("api_key")
    if api_key in (None, "", SECRET_MASK):
        api_key = existing.api_key if existing else None

    body = {
        "id": profile_id,
        "name": payload.get("name") or (existing.name if existing else "新模型"),
        "provider": payload.get("provider") or (existing.provider if existing else "openai"),
        "base_url": payload.get("base_url")
        if payload.get("base_url") is not None
        else (existing.base_url if existing else ""),
        "model_name": payload.get("model_name")
        or (existing.model_name if existing else ""),
        "api_key": api_key,
        "temperature": payload.get("temperature")
        if payload.get("temperature") is not None
        else (existing.temperature if existing else 0.2),
        "max_tokens": payload.get("max_tokens")
        if payload.get("max_tokens") is not None
        else (existing.max_tokens if existing else 4096),
        "model_family": payload.get("model_family")
        if payload.get("model_family") is not None
        else (existing.model_family if existing else None),
    }
    profile = ModelProfile.model_validate(body)
    state.settings.upsert_profile(profile)
    return {"profile": _masked(profile)}


@router.post("/profiles/{profile_id}/activate")
def activate_profile(
    profile_id: str, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    settings = state.settings.load()
    if not any(profile.id == profile_id for profile in settings.model_profiles):
        raise ValidationFailure(f"模型配置 '{profile_id}' 不存在")
    settings.active_model_profile_id = profile_id
    state.settings.save(settings)
    return {"active_model_profile_id": profile_id}


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: str, state: AppState = Depends(get_state)) -> None:
    state.settings.delete_profile(profile_id)


@router.post("/test")
def test_profile(
    payload: dict[str, Any] = Body(default_factory=dict),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Send a single tiny completion to verify the configuration."""
    from ..agents.llm import build_chat_model
    from ..agents.llm.base import ChatMessage

    settings = state.settings.load()
    profile_id = payload.get("profile_id") or settings.active_model_profile_id
    profile = next(
        (item for item in settings.model_profiles if item.id == profile_id), None
    )
    if profile is None:
        raise ValidationFailure(f"模型配置 '{profile_id}' 不存在")
    if profile.provider in {"offline", "mock"}:
        return {
            "ok": True,
            "message": "该模式不联网，可直接使用（可用「生成测试数据」验证界面）。",
        }

    import asyncio

    if profile.provider == "agentscope":
        from ..agents.agentscope.convert import UserMsg, text_of
        from ..agents.agentscope.models import build_chat_model as build_agentscope_model

        model = build_agentscope_model(profile)

        async def _probe_agentscope() -> str:
            response = await model(
                messages=[UserMsg(name="user", content="只回复两个字：可用")],
                tools=None,
            )
            return text_of(response).strip()

    else:
        model = build_chat_model(profile)  # type: ignore[assignment]

        async def _probe_agentscope() -> str:
            turn = await model.complete(  # type: ignore[attr-defined]
                [
                    ChatMessage(role="system", content="你是连通性测试助手。"),
                    ChatMessage(role="user", content="只回复两个字：可用"),
                ],
                [],
            )
            return (turn.content or "").strip()

    try:
        text = asyncio.run(_probe_agentscope())
    except ProviderError as exc:
        return {"ok": False, "message": exc.message, "details": exc.details}
    except Exception as exc:  # pragma: no cover - network surprises
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"连接成功：{text[:60]}"}
