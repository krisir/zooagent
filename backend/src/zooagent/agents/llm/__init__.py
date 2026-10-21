from ...config import ModelProfile
from .base import AssistantTurn, ChatMessage, ChatModel, ToolCall
from .offline import OfflinePlannerModel
from .openai_compatible import OpenAICompatibleModel


def build_chat_model(profile: ModelProfile) -> ChatModel:
    if profile.provider == "offline":
        return OfflinePlannerModel()
    return OpenAICompatibleModel(profile)


__all__ = [
    "AssistantTurn",
    "ChatMessage",
    "ChatModel",
    "OfflinePlannerModel",
    "OpenAICompatibleModel",
    "ToolCall",
    "build_chat_model",
]
