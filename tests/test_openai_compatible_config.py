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
        ("http://localhost:8080/v1", "http://localhost:8080/v1"),
        ("http://[::1]:8080/v1", "http://[::1]:8080/v1"),
        ("http://127.1.2.3/v1", "http://127.1.2.3/v1"),
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


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.com/v1",
        "http://10.0.0.1/v1",
        "http://192.168.1.1/v1",
        "http://169.254.1.1/v1",
        "http://8.8.8.8/v1",
        "http://intranet.corp/v1",
    ],
)
def test_normalize_openai_base_url_rejects_remote_http(value):
    """P1-1：远程明文 HTTP 端点必须被拒绝，避免 Bearer 密钥泄漏。"""
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_openai_base_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://token@example.com/v1",
        "https://user:password@example.com/v1",
    ],
)
def test_normalize_openai_base_url_rejects_embedded_credentials(value):
    with pytest.raises(ValueError, match="用户名或密码"):
        normalize_openai_base_url(value)


def test_save_api_config_rejects_remote_http_outside_settings_ui(tmp_config_manager):
    """Endpoint policy must hold for programmatic/config-file callers too."""
    from src.domain.secret import StorageStatus

    config = tmp_config_manager.get_api_config()
    config.update(
        {
            "provider": OPENAI_COMPATIBLE_PROVIDER,
            "base_url": "http://10.0.0.8/v1",
            "api_key": "sk-test",
            "model_name": "chat",
        }
    )

    result = tmp_config_manager.save_api_config(config)

    assert result.failed
    assert result.secret_status is StorageStatus.FAILED


def test_base_api_rejects_remote_http_before_request():
    from src.api.base_api import BaseAPI

    with pytest.raises(ValueError, match="HTTPS"):
        BaseAPI(
            {
                "base_url": "http://203.0.113.10/v1",
                "api_key": "sk-test",
                "enable_batch": False,
                "enable_cache": False,
            }
        )


def test_custom_provider_config_is_saved_without_plaintext_key(
    tmp_config_manager,
):
    """P1-2：通过注入 SecretStore 替身验证密钥与配置分离存储。"""
    from src.domain.secret import StorageStatus

    # P1-2：注入 SecretStore 替身，替代旧的 monkeypatch 模块级函数
    stored_keys = {}

    class FakeSecretStore:
        def store(self, identifier, key):
            stored_keys[identifier] = key
            return StorageStatus.PERSISTED

        def retrieve(self, identifier):
            return stored_keys.get(identifier, "")

        def delete(self, identifier):
            stored_keys.pop(identifier, None)
            return True

    tmp_config_manager._secret_store = FakeSecretStore()

    custom_config = tmp_config_manager.get_api_config()
    custom_config.update(
        {
            "provider": OPENAI_COMPATIBLE_PROVIDER,
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-custom-secret",
            "model_name": "example-chat-model",
            "max_tokens": 999999,
        }
    )

    result = tmp_config_manager.save_api_config(custom_config)
    assert result  # SecretSaveResult __bool__：PERSISTED → True
    assert result.persisted

    disk_config = json.loads(tmp_config_manager.api_config_file.read_text(encoding="utf-8"))
    assert "api_key" not in disk_config
    assert "provider_keys" not in disk_config
    assert "max_tokens" not in disk_config
    assert "max_tokens" not in tmp_config_manager.get_api_config()
    assert disk_config["provider_configs"][OPENAI_COMPATIBLE_PROVIDER] == {
        "base_url": "https://api.example.com/v1",
        "model_name": "example-chat-model",
    }
    secret_reference = disk_config["active_secret_ref"]
    assert stored_keys[secret_reference] == "sk-custom-secret"

    saved_custom = tmp_config_manager.get_provider_config(OPENAI_COMPATIBLE_PROVIDER)
    assert saved_custom == {
        "base_url": "https://api.example.com/v1",
        "model_name": "example-chat-model",
        "api_key": "sk-custom-secret",
    }

    reloaded_manager = type(tmp_config_manager)(
        app_paths=type("Paths", (), {"config_dir": tmp_config_manager.config_dir})()
    )
    # P1-2：reloaded_manager 也注入相同的 FakeSecretStore
    reloaded_manager._secret_store = FakeSecretStore()
    # 恢复已存储的密钥到新实例（模拟 keyring 持久化）
    reloaded_manager._secret_store.store(secret_reference, "sk-custom-secret")
    assert reloaded_manager.get_api_config()["provider"] == OPENAI_COMPATIBLE_PROVIDER
    assert reloaded_manager.get_provider_config(OPENAI_COMPATIBLE_PROVIDER) == saved_custom

    built_in_config = tmp_config_manager.get_api_config()
    built_in_config.update(
        {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-deepseek",
            "model_name": "deepseek-flash",
        }
    )
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
