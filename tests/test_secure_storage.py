#!/usr/bin/env python3
"""
R2-BUG-002 / R2-BUG-003：密钥迁移安全性测试

验证：
- 密钥环不可用时迁移仍保留旧明文 Key
- 密钥环写入成功但回读不一致时不删除旧 Key
- 正常密钥环迁移后磁盘 JSON 中不存在 api_key
- 返回给 UI 的预设仍能临时包含运行时 Key
- 搜索配置目录和日志找不到完整测试 Key
"""

import json
import os
from unittest.mock import MagicMock, patch

from src.utils.secure_storage import (
    StorageStatus,
    get_key,
    mask_key,
    store_key,
)

# ── StorageStatus 基础测试 ───────────────────────────


class TestStorageStatus:
    """R2-BUG-002：StorageStatus 枚举与 store_key 返回值。"""

    def test_status_values(self):
        assert StorageStatus.PERSISTED == "persisted"
        assert StorageStatus.SESSION_ONLY == "session_only"
        assert StorageStatus.FAILED == "failed"

    def test_store_empty_key_returns_persisted(self, reset_secure_storage_state):
        """空密钥视为删除，返回 PERSISTED。"""
        status = store_key("provider:test", "")
        assert status == StorageStatus.PERSISTED

    def test_store_key_session_only_when_keyring_unavailable(
        self, reset_secure_storage_state, monkeypatch
    ):
        """密钥环不可用时，写入环境变量返回 SESSION_ONLY。"""
        # 强制 keyring 不可用
        monkeypatch.setattr("src.utils.secure_storage._detect_keyring", lambda: False)
        reset_secure_storage_state._keyring_available = False

        status = store_key("provider:test", "sk-test-key-12345")
        assert status == StorageStatus.SESSION_ONLY

        # 验证环境变量被设置
        assert os.environ.get("AI_TRANSLATOR_KEY_PROVIDER_TEST") == "sk-test-key-12345"

    def test_session_override_wins_over_stale_keyring_value(
        self, reset_secure_storage_state, monkeypatch
    ):
        """会话降级的新 Key 不得被 keyring 中的旧 Key 覆盖。"""
        fake_module = MagicMock()
        fake_module.get_password.return_value = "stale-key"
        monkeypatch.setattr("src.utils.secure_storage._detect_keyring", lambda: True)
        monkeypatch.setenv("AI_TRANSLATOR_KEY_PROVIDER_TEST", "session-key")

        with patch.dict("sys.modules", {"keyring": fake_module}):
            assert get_key("provider:test") == "session-key"

    def test_persisted_write_clears_old_session_override(
        self, reset_secure_storage_state, monkeypatch
    ):
        """keyring 成功写入后应清除旧的会话覆盖。"""
        fake_module = MagicMock()
        fake_module.get_password.return_value = "persisted-key"
        monkeypatch.setattr("src.utils.secure_storage._detect_keyring", lambda: True)
        monkeypatch.setenv("AI_TRANSLATOR_KEY_PROVIDER_TEST", "old-session-key")

        with patch.dict("sys.modules", {"keyring": fake_module}):
            status = store_key("provider:test", "persisted-key")

        assert status == StorageStatus.PERSISTED
        assert "AI_TRANSLATOR_KEY_PROVIDER_TEST" not in os.environ

    def test_store_key_persisted_when_keyring_available(
        self, reset_secure_storage_state, monkeypatch
    ):
        """密钥环可用且回读一致时返回 PERSISTED。"""
        fake_keyring = MagicMock()
        fake_keyring.set_password = MagicMock()
        fake_keyring.get_password = MagicMock(return_value="sk-persisted-key")

        # 模拟 keyring 模块
        fake_module = MagicMock()
        fake_module.set_password = fake_keyring.set_password
        fake_module.get_password = fake_keyring.get_password

        monkeypatch.setattr("src.utils.secure_storage._detect_keyring", lambda: True)
        reset_secure_storage_state._keyring_available = True

        with patch.dict("sys.modules", {"keyring": fake_module}):
            status = store_key("provider:test", "sk-persisted-key")

        assert status == StorageStatus.PERSISTED
        fake_keyring.set_password.assert_called_once()

    def test_store_key_session_only_when_readback_mismatch(
        self, reset_secure_storage_state, monkeypatch
    ):
        """密钥环写入后回读不一致时降级到 SESSION_ONLY。"""
        fake_keyring = MagicMock()
        fake_keyring.set_password = MagicMock()
        # 回读返回不同的值
        fake_keyring.get_password = MagicMock(return_value="different-value")

        fake_module = MagicMock()
        fake_module.set_password = fake_keyring.set_password
        fake_module.get_password = fake_keyring.get_password

        monkeypatch.setattr("src.utils.secure_storage._detect_keyring", lambda: True)
        reset_secure_storage_state._keyring_available = True

        with patch.dict("sys.modules", {"keyring": fake_module}):
            status = store_key("provider:test", "sk-original-key")

        assert status == StorageStatus.SESSION_ONLY
        # 应该同时写入环境变量作为降级
        assert os.environ.get("AI_TRANSLATOR_KEY_PROVIDER_TEST") == "sk-original-key"


# ── ConfigManager 迁移测试 ───────────────────────────


class TestConfigMigration:
    """R2-BUG-002：ConfigManager._migrate_plaintext_keys 行为。"""

    def test_migration_preserves_plaintext_when_session_only(self, tmp_config_manager, monkeypatch):
        """密钥环不可用时（SESSION_ONLY），磁盘 JSON 保留明文 Key。"""
        api_config_file = tmp_config_manager.api_config_file

        # 预写一个包含明文密钥的配置文件
        api_config_file.write_text(
            json.dumps(
                {
                    "provider": "siliconflow",
                    "api_key": "sk-plaintext-secret-key",
                    "model_name": "test-model",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 强制 keyring 不可用
        from src.utils import secure_storage

        monkeypatch.setattr(secure_storage, "_detect_keyring", lambda: False)
        secure_storage._keyring_available = False

        # 重新加载配置（触发迁移）
        config = tmp_config_manager.load_api_config()

        # 磁盘文件应仍包含明文 Key（因为只是 SESSION_ONLY）
        disk_data = json.loads(api_config_file.read_text(encoding="utf-8"))
        assert "api_key" in disk_data, "SESSION_ONLY 时磁盘应保留明文 Key"
        assert disk_data["api_key"] == "sk-plaintext-secret-key"

        # 但运行时 config 应能从环境变量读取到 Key
        assert config["api_key"] == "sk-plaintext-secret-key"

    def test_migration_removes_plaintext_when_persisted(self, tmp_config_manager, monkeypatch):
        """密钥环持久化成功后，磁盘 JSON 不再包含明文 Key。"""
        api_config_file = tmp_config_manager.api_config_file

        api_config_file.write_text(
            json.dumps(
                {
                    "provider": "siliconflow",
                    "api_key": "sk-plaintext-to-migrate",
                    "model_name": "test-model",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 模拟 keyring 可用且持久化成功
        # 使用 dict 存储，确保 get_password 在 set_password 之前返回 None
        stored = {}

        def fake_set_password(service, identifier, key):
            stored[identifier] = key

        def fake_get_password(service, identifier):
            return stored.get(identifier)

        fake_module = MagicMock()
        fake_module.set_password = fake_set_password
        fake_module.get_password = fake_get_password

        from src.utils import secure_storage

        monkeypatch.setattr(secure_storage, "_detect_keyring", lambda: True)
        secure_storage._keyring_available = True

        with patch.dict("sys.modules", {"keyring": fake_module}):
            config = tmp_config_manager.load_api_config()

        # 磁盘文件不应包含明文 Key
        disk_data = json.loads(api_config_file.read_text(encoding="utf-8"))
        assert "api_key" not in disk_data, "持久化成功后磁盘不应包含明文 Key"
        assert "provider_keys" not in disk_data

        # 运行时 config 应能从密钥环读取到 Key
        assert config["api_key"] == "sk-plaintext-to-migrate"

        # 密钥环中应有该密钥
        assert stored.get("provider:siliconflow") == "sk-plaintext-to-migrate"

    def test_migration_with_provider_keys_dict(self, tmp_config_manager, monkeypatch):
        """迁移 provider_keys 字典中的多个密钥。"""
        api_config_file = tmp_config_manager.api_config_file

        api_config_file.write_text(
            json.dumps(
                {
                    "provider": "siliconflow",
                    "provider_keys": {
                        "siliconflow": "sk-sf-key",
                        "deepseek": "sk-ds-key",
                    },
                    "model_name": "test-model",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 模拟 keyring 持久化成功
        stored = {}

        def fake_set_password(service, identifier, key):
            stored[identifier] = key

        def fake_get_password(service, identifier):
            return stored.get(identifier)

        fake_module = MagicMock()
        fake_module.set_password = fake_set_password
        fake_module.get_password = fake_get_password

        from src.utils import secure_storage

        monkeypatch.setattr(secure_storage, "_detect_keyring", lambda: True)
        secure_storage._keyring_available = True

        with patch.dict("sys.modules", {"keyring": fake_module}):
            tmp_config_manager.load_api_config()

        # 磁盘文件不应包含 provider_keys
        disk_data = json.loads(api_config_file.read_text(encoding="utf-8"))
        assert "provider_keys" not in disk_data
        assert "api_key" not in disk_data

        # 密钥环中应有两个提供商的密钥
        assert stored.get("provider:siliconflow") == "sk-sf-key"
        assert stored.get("provider:deepseek") == "sk-ds-key"


# ── R2-BUG-003：预设迁移测试 ─────────────────────────


class TestPresetMigration:
    """R2-BUG-003：预设迁移不写回明文 Key。"""

    def test_preset_migration_separates_runtime_and_disk(self, tmp_config_manager, monkeypatch):
        """迁移旧预设后磁盘 JSON 中不存在 api_key，运行时仍能使用。"""
        presets_file = tmp_config_manager.config_dir / "api_presets.json"

        # 预写包含明文 api_key 的预设
        presets_file.write_text(
            json.dumps(
                {
                    "preset1": {
                        "model_name": "model-a",
                        "api_key": "sk-preset-secret-key",
                    },
                    "preset2": {
                        "model_name": "model-b",
                        # preset2 没有明文 key
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 模拟 keyring 持久化成功
        stored = {}

        def fake_set_password(service, identifier, key):
            stored[identifier] = key

        def fake_get_password(service, identifier):
            return stored.get(identifier)

        fake_module = MagicMock()
        fake_module.set_password = fake_set_password
        fake_module.get_password = fake_get_password

        from src.utils import secure_storage

        monkeypatch.setattr(secure_storage, "_detect_keyring", lambda: True)
        secure_storage._keyring_available = True

        with patch.dict("sys.modules", {"keyring": fake_module}):
            presets = tmp_config_manager.load_api_presets()

        # 磁盘文件不应包含 api_key
        disk_data = json.loads(presets_file.read_text(encoding="utf-8"))
        assert "api_key" not in disk_data["preset1"]
        assert "api_key" not in disk_data["preset2"]
        assert disk_data["preset1"]["model_name"] == "model-a"

        # 运行时 presets 应能临时包含 api_key
        assert presets["preset1"]["api_key"] == "sk-preset-secret-key"
        assert presets["preset1"]["model_name"] == "model-a"

        # 密钥环中应有 preset1 的密钥
        assert stored.get("preset:preset1") == "sk-preset-secret-key"

    def test_preset_migration_preserves_plaintext_when_session_only(
        self, tmp_config_manager, monkeypatch
    ):
        """密钥环不可用时，预设磁盘 JSON 保留明文 Key。"""
        presets_file = tmp_config_manager.config_dir / "api_presets.json"

        presets_file.write_text(
            json.dumps(
                {
                    "preset1": {
                        "model_name": "model-a",
                        "api_key": "sk-preset-secret",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        from src.utils import secure_storage

        monkeypatch.setattr(secure_storage, "_detect_keyring", lambda: False)
        secure_storage._keyring_available = False

        presets = tmp_config_manager.load_api_presets()

        # 磁盘文件应仍包含明文 Key
        disk_data = json.loads(presets_file.read_text(encoding="utf-8"))
        assert "api_key" in disk_data["preset1"]
        assert disk_data["preset1"]["api_key"] == "sk-preset-secret"

        # 运行时仍能使用
        assert presets["preset1"]["api_key"] == "sk-preset-secret"

    def test_preset_no_plaintext_in_logs(self, tmp_config_manager, monkeypatch, caplog):
        """搜索日志找不到完整测试 Key。"""
        presets_file = tmp_config_manager.config_dir / "api_presets.json"

        test_key = "sk-very-secret-key-12345"
        presets_file.write_text(
            json.dumps(
                {
                    "preset1": {
                        "model_name": "model-a",
                        "api_key": test_key,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 模拟 keyring 持久化成功
        stored = {}

        def fake_set_password(service, identifier, key):
            stored[identifier] = key

        def fake_get_password(service, identifier):
            return stored.get(identifier)

        fake_module = MagicMock()
        fake_module.set_password = fake_set_password
        fake_module.get_password = fake_get_password

        from src.utils import secure_storage

        monkeypatch.setattr(secure_storage, "_detect_keyring", lambda: True)
        secure_storage._keyring_available = True

        import logging

        with caplog.at_level(logging.DEBUG):
            with patch.dict("sys.modules", {"keyring": fake_module}):
                tmp_config_manager.load_api_presets()

        # 日志中不应包含完整测试 Key
        full_log = caplog.text
        assert test_key not in full_log, f"日志中不应包含完整测试 Key，但找到: {full_log}"


# ── mask_key 测试 ────────────────────────────────────


class TestMaskKey:
    """mask_key 脱敏函数。"""

    def test_mask_long_key(self):
        key = "sk-1234567890abcdefghijklmnopqrstuvwxyz"
        masked = mask_key(key)
        assert masked.startswith("sk-1")
        assert masked.endswith("yz")
        assert "*" in masked
        assert len(masked) == len(key)

    def test_mask_short_key(self):
        assert mask_key("short") == "*****"
        assert mask_key("") == ""
        assert mask_key("12345678") == "********"  # 正好8位，全脱敏

    def test_mask_key_not_equal_original(self):
        key = "sk-1234567890abcdefghijklmnopqrstuvwxyz"
        masked = mask_key(key)
        assert masked != key
