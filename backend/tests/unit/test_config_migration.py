"""旧配置迁移与 provider 取值收敛（ADR-0016）。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from zooagent.config import ModelProfile, SettingsService, WorkspacePaths, default_settings


def _write_settings(paths: WorkspacePaths, payload: dict) -> None:
    paths.settings_file.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")


def test_openai_profiles_migrate_to_agentscope(tmp_path) -> None:
    paths = WorkspacePaths.resolve(tmp_path / "ws")
    _write_settings(
        paths,
        {
            "active_model_profile_id": "p1",
            "model_profiles": [
                {
                    "id": "p1",
                    "name": "DeepSeek",
                    "provider": "openai",
                    "base_url": "https://api.deepseek.com/v1",
                    "model_name": "deepseek-chat",
                    "api_key": "sk-keep-me",
                },
                {
                    "id": "p2",
                    "name": "本机 Ollama",
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model_name": "qwen2.5:7b",
                },
            ],
        },
    )

    settings = SettingsService(paths).load()

    assert [profile.provider for profile in settings.model_profiles] == [
        "agentscope",
        "agentscope",
    ]
    # 密钥、地址、模型名都不能在迁移里丢
    assert settings.model_profiles[0].api_key == "sk-keep-me"
    assert settings.model_profiles[0].base_url == "https://api.deepseek.com/v1"
    # ollama 旧 base_url 带 /v1，family 必须显式钉住，别被推断成 openai
    assert settings.model_profiles[1].model_family == "ollama"

    # 迁移结果要写回文件，避免每次启动都改写
    on_disk = json.loads(paths.settings_file.read_text("utf-8"))
    assert {item["provider"] for item in on_disk["model_profiles"]} == {"agentscope"}
    assert on_disk["model_profiles"][1]["model_family"] == "ollama"


def test_migrated_file_is_stable(tmp_path) -> None:
    paths = WorkspacePaths.resolve(tmp_path / "ws")
    _write_settings(
        paths,
        {
            "model_profiles": [
                {"id": "p1", "name": "x", "provider": "openai", "model_name": "gpt-4o-mini"}
            ]
        },
    )
    first = paths.settings_file.read_text("utf-8")
    SettingsService(paths).load()
    second = paths.settings_file.read_text("utf-8")
    assert first != second
    # 再读一次不该再改动（已经迁移过）
    SettingsService(paths).load()
    assert paths.settings_file.read_text("utf-8") == second


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelProfile(id="p", name="x", provider="anthropic", model_name="claude")


def test_default_settings_only_use_surviving_providers(tmp_path) -> None:
    paths = WorkspacePaths.resolve(tmp_path / "ws")
    SettingsService(paths).load()
    settings = default_settings()
    assert {profile.provider for profile in settings.model_profiles} <= {
        "agentscope",
        "offline",
        "mock",
    }
