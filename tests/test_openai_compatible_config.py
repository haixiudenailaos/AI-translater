import json

import pytest

from src.config.translation_profile import (
    OPENAI_COMPATIBLE_PROVIDER,
    normalize_openai_base_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        (
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1",
        ),
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
    ],
)
def test_normalize_openai_base_url(value, expected):
    assert normalize_openai_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "api.example.com/v1", "ftp://api.example.com/v1", "https:///v1"],
)
def test_normalize_openai_base_url_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_openai_base_url(value)


def test_custom_provider_config_is_saved_without_plaintext_key(
    tmp_config_manager, monkeypatch
):
    from src.config import config_manager as config_module

    stored_keys = {}
    monkeypatch.setattr(
        config_module,
        "store_key",
        lambda identifier, key: stored_keys.__setitem__(identifier, key),
    )
    monkeypatch.setattr(
        config_module, "get_key", lambda identifier: stored_keys.get(identifier, "")
    )

    custom_config = tmp_config_manager.get_api_config()
    custom_config.update({
        "provider": OPENAI_COMPATIBLE_PROVIDER,
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-custom-secret",
        "model_name": "example-chat-model",
    })

    assert tmp_config_manager.save_api_config(custom_config)

    disk_config = json.loads(
        tmp_config_manager.api_config_file.read_text(encoding="utf-8")
    )
    assert "api_key" not in disk_config
    assert "provider_keys" not in disk_config
    assert disk_config["provider_configs"][OPENAI_COMPATIBLE_PROVIDER] == {
        "base_url": "https://api.example.com/v1",
        "model_name": "example-chat-model",
    }
    assert stored_keys[f"provider:{OPENAI_COMPATIBLE_PROVIDER}"] == "sk-custom-secret"

    saved_custom = tmp_config_manager.get_provider_config(
        OPENAI_COMPATIBLE_PROVIDER
    )
    assert saved_custom == {
        "base_url": "https://api.example.com/v1",
        "model_name": "example-chat-model",
        "api_key": "sk-custom-secret",
    }

    reloaded_manager = type(tmp_config_manager)(
        app_paths=type("Paths", (), {"config_dir": tmp_config_manager.config_dir})()
    )
    assert reloaded_manager.get_api_config()["provider"] == OPENAI_COMPATIBLE_PROVIDER
    assert reloaded_manager.get_provider_config(OPENAI_COMPATIBLE_PROVIDER) == saved_custom

    built_in_config = tmp_config_manager.get_api_config()
    built_in_config.update({
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-deepseek",
        "model_name": "deepseek-v4-flash",
    })
    assert tmp_config_manager.save_api_config(built_in_config)
    assert (
        tmp_config_manager.get_provider_config(OPENAI_COMPATIBLE_PROVIDER)["base_url"]
        == "https://api.example.com/v1"
    )


def test_translator_uses_openai_compatible_client():
    from src.api.openai_compatible_api import OpenAICompatibleAPI
    from src.core.translator import TranslatorEngine

    api_config = {
        "provider": OPENAI_COMPATIBLE_PROVIDER,
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "model_name": "example-chat-model",
        "enable_batch": False,
        "enable_cache": False,
    }

    class FakeConfigManager:
        def get_api_config(self):
            return api_config

    translator = TranslatorEngine.__new__(TranslatorEngine)
    translator.api = None
    translator.config_manager = FakeConfigManager()
    translator._init_api()
    try:
        assert isinstance(translator.api, OpenAICompatibleAPI)
        assert translator.api.base_url == "https://api.example.com/v1"
    finally:
        translator.api.close()
