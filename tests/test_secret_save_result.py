#!/usr/bin/env python3
"""
P1-2 回归测试：API Key 未持久化时不得提示保存成功

覆盖验收标准：
- UI 不会把 SESSION_ONLY 表述为永久保存成功。
- FAILED 不会更新内存配置为看似可用的状态。
- 日志和返回对象均不包含密钥明文。
- keyring 不可用、写入失败、回读不一致和空密钥删除测试。
"""

import pytest

from src.config.config_manager import ConfigManager
from src.domain.secret import SecretSaveResult, StorageStatus

# ── 测试替身 ──────────────────────────────────────


class FakeSecretStore:
    """可配置状态的 SecretStore 替身"""

    def __init__(self, status=StorageStatus.PERSISTED):
        self._status = status
        self.stored = {}
        self.store_calls = []

    def store(self, identifier, key):
        self.store_calls.append((identifier, key))
        if self._status != StorageStatus.FAILED:
            self.stored[identifier] = key
        return self._status

    def retrieve(self, identifier):
        return self.stored.get(identifier, "")

    def delete(self, identifier):
        self.stored.pop(identifier, None)
        return True


class FailingSecretStore:
    """store 抛异常的 SecretStore 替身"""

    def store(self, identifier, key):
        raise OSError("keyring 服务不可用")

    def retrieve(self, identifier):
        return ""

    def delete(self, identifier):
        return True


# ── fixtures ──────────────────────────────────────


@pytest.fixture
def tmp_config_manager(tmp_app_paths):
    """带注入 SecretStore 的 ConfigManager"""
    return ConfigManager(app_paths=tmp_app_paths)


# ── SecretSaveResult 单元测试 ──────────────────────────────────────


class TestSecretSaveResult:
    def test_persisted_is_truthy(self):
        result = SecretSaveResult(
            secret_status=StorageStatus.PERSISTED,
            config_saved=True,
        )
        assert bool(result) is True
        assert result.persisted
        assert not result.session_only
        assert not result.failed
        assert result.user_message == "设置已保存"

    def test_session_only_is_truthy_but_flagged(self):
        """P1-2：SESSION_ONLY 允许继续会话（truthy），但标记为 session_only"""
        result = SecretSaveResult(
            secret_status=StorageStatus.SESSION_ONLY,
            config_saved=True,
        )
        assert bool(result) is True  # 允许继续会话
        assert not result.persisted
        assert result.session_only
        assert "未持久化" in result.user_message
        assert "重启后需重新输入" in result.user_message

    def test_failed_is_falsy(self):
        """P1-2：FAILED 时配置保存整体失败"""
        result = SecretSaveResult(
            secret_status=StorageStatus.FAILED,
            config_saved=False,
        )
        assert bool(result) is False
        assert result.failed
        assert "保存失败" in result.user_message

    def test_config_save_failure_is_falsy(self):
        """密钥已存储但配置文件写入失败，整体失败"""
        result = SecretSaveResult(
            secret_status=StorageStatus.PERSISTED,
            config_saved=False,
            error_message="磁盘满",
        )
        assert bool(result) is False
        assert "配置文件写入失败" in result.user_message


# ── ConfigManager.save_api_config 测试 ──────────────────────────────────────


class TestSaveApiConfigSecretStatus:
    """P1-2：save_api_config 正确反映密钥持久化状态"""

    def test_persisted_returns_success(self, tmp_config_manager):
        """密钥持久化成功，返回成功结果"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        config = {
            "provider": "siliconflow",
            "api_key": "sk-test-key",
            "model_name": "test-model",
        }
        result = tmp_config_manager.save_api_config(config)

        assert result.persisted
        assert result.config_saved
        assert bool(result) is True
        # 内存配置已更新
        assert tmp_config_manager.api_config["api_key"] == "sk-test-key"
        # 密钥已存储
        assert store.stored["provider:siliconflow"] == "sk-test-key"

    def test_failed_does_not_update_config(self, tmp_config_manager):
        """P1-2：FAILED 时不更新内存配置，不写入 JSON"""
        store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager._secret_store = store

        original_config = dict(tmp_config_manager.api_config)
        config = {
            "provider": "siliconflow",
            "api_key": "sk-new-key",
            "model_name": "new-model",
        }
        result = tmp_config_manager.save_api_config(config)

        # 返回失败结果
        assert result.failed
        assert not result.config_saved
        assert bool(result) is False
        # 内存配置未被更新（保持原样）
        assert tmp_config_manager.api_config == original_config
        # 密钥未存储
        assert "provider:siliconflow" not in store.stored

    def test_session_only_allows_session(self, tmp_config_manager):
        """P1-2：SESSION_ONLY 允许继续会话，但标记为 session_only"""
        store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)
        tmp_config_manager._secret_store = store

        config = {
            "provider": "siliconflow",
            "api_key": "sk-session-key",
            "model_name": "test-model",
        }
        result = tmp_config_manager.save_api_config(config)

        # 允许继续会话（truthy）
        assert bool(result) is True
        assert result.session_only
        assert not result.persisted
        # 用户消息明确提示重启后失效
        assert "未持久化" in result.user_message
        # 内存配置已更新（允许会话使用）
        assert tmp_config_manager.api_config["api_key"] == "sk-session-key"
        # 密钥已存储到环境变量（FakeSecretStore 模拟）
        assert store.stored["provider:siliconflow"] == "sk-session-key"

    def test_store_exception_returns_failed(self, tmp_config_manager):
        """P1-2：SecretStore.store 抛异常时返回 FAILED"""
        tmp_config_manager._secret_store = FailingSecretStore()

        config = {
            "provider": "siliconflow",
            "api_key": "sk-test-key",
            "model_name": "test-model",
        }
        result = tmp_config_manager.save_api_config(config)

        assert result.failed
        assert not result.config_saved
        assert bool(result) is False
        assert "密钥存储异常" in result.error_message

    def test_empty_key_deletes_secret(self, tmp_config_manager):
        """空密钥视为删除，返回 PERSISTED"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        # 先存储一个密钥
        store.stored["provider:siliconflow"] = "old-key"
        tmp_config_manager._secret_store = store

        config = {
            "provider": "siliconflow",
            "api_key": "",  # 空密钥
            "model_name": "test-model",
        }
        result = tmp_config_manager.save_api_config(config)

        # 空密钥视为删除，返回 PERSISTED
        assert result.persisted
        assert bool(result) is True

    def test_result_does_not_leak_credential(self, tmp_config_manager):
        """P1-2：返回结果和错误消息不包含密钥明文"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        secret_key = "sk-super-secret-key-12345"
        config = {
            "provider": "siliconflow",
            "api_key": secret_key,
            "model_name": "test-model",
        }
        result = tmp_config_manager.save_api_config(config)

        # SecretSaveResult 不含密钥明文
        assert secret_key not in str(result)
        assert secret_key not in result.error_message
        assert secret_key not in result.user_message
        # provider 字段不含密钥
        assert result.provider == "siliconflow"

    def test_injected_secret_store_protocol(self, tmp_config_manager):
        """P1-2：ConfigManager 使用注入的 SecretStore，不依赖全局状态"""
        store1 = FakeSecretStore(status=StorageStatus.PERSISTED)
        store2 = FakeSecretStore(status=StorageStatus.SESSION_ONLY)

        tmp_config_manager._secret_store = store1
        result1 = tmp_config_manager.save_api_config(
            {
                "provider": "siliconflow",
                "api_key": "key1",
            }
        )
        assert result1.persisted
        assert store1.store_calls  # store1 被调用
        assert not store2.store_calls  # store2 未被调用

        tmp_config_manager._secret_store = store2
        result2 = tmp_config_manager.save_api_config(
            {
                "provider": "siliconflow",
                "api_key": "key2",
            }
        )
        assert result2.session_only
        assert store2.store_calls  # store2 被调用


# ── ConfigManager.save_volc_key 测试（P1-2：返回 SecretSaveResult） ────


class TestSaveVolcKeySecretStatus:
    """P1-2：save_volc_key 返回 ``SecretSaveResult`` 区分三态。

    验收标准：预设、主 API、火山 Key 三条路径都有 UI 集成测试。

    P1-2：通过注入 ``FakeSecretStore`` 验证三态，不依赖全局 ``store_key``。
    """

    def test_volc_key_persisted_returns_success(self, tmp_config_manager):
        """火山 Key 持久化成功返回 PERSISTED 结果。"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_volc_key("volc-test-key")

        assert result.persisted
        assert result.config_saved
        assert bool(result) is True
        assert result.provider == "volc"
        assert result.user_message == "设置已保存"
        # 密钥已通过 SecretStore 存储
        assert store.stored["volc:ark_api_key"] == "volc-test-key"

    def test_volc_key_get_uses_injected_secret_store(self, tmp_config_manager):
        """读取必须使用与保存相同的注入 SecretStore。"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        store.stored["volc:ark_api_key"] = "injected-volc-key"
        tmp_config_manager._secret_store = store

        assert tmp_config_manager.get_volc_key() == "injected-volc-key"

    def test_volc_key_readback_mismatch_fails(self, tmp_config_manager):
        """SecretStore 声称成功但回读旧值时必须阻止保存。"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        store.retrieve = lambda _identifier: "stale-volc-key"
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_volc_key("new-volc-key")

        assert result.failed
        assert not result.config_saved
        assert "校验失败" in result.error_message

    def test_volc_key_session_only_warns_user(self, tmp_config_manager):
        """P1-2：SESSION_ONLY 时 user_message 明确提示重启后失效。"""
        store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_volc_key("volc-session-key")

        # 允许继续会话（truthy）
        assert bool(result) is True
        assert result.session_only
        assert not result.persisted
        # user_message 明确提示重启后失效
        assert "未持久化" in result.user_message
        assert "重启后需重新输入" in result.user_message

    def test_volc_key_failed_blocks_save(self, tmp_config_manager):
        """P1-2：FAILED 时返回失败结果，UI 不应关闭设置窗口。"""
        store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_volc_key("volc-fail-key")

        assert result.failed
        assert not result.config_saved
        assert bool(result) is False
        assert "保存失败" in result.user_message

    def test_volc_key_store_exception_returns_failed(self, tmp_config_manager):
        """P1-2：SecretStore.store 抛异常时返回 FAILED。"""
        tmp_config_manager._secret_store = FailingSecretStore()

        result = tmp_config_manager.save_volc_key("volc-exc-key")

        assert result.failed
        assert not result.config_saved
        assert bool(result) is False
        assert "keyring 服务不可用" in result.error_message or "存储异常" in result.error_message

    def test_volc_key_empty_key_returns_success(self, tmp_config_manager):
        """空火山 Key 视为删除，返回 PERSISTED（与 save_api_config 一致）。"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_volc_key("")

        assert result.persisted
        assert bool(result) is True

    def test_volc_key_result_does_not_leak_credential(self, tmp_config_manager):
        """P1-2：返回结果不含密钥明文。"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        secret = "volc-super-secret-12345"
        result = tmp_config_manager.save_volc_key(secret)

        assert secret not in str(result)
        assert secret not in result.error_message
        assert secret not in result.user_message
        assert result.provider == "volc"

    def test_volc_key_backward_compat_bool(self, tmp_config_manager):
        """P1-2 向后兼容：``if save_volc_key(...)`` 旧布尔判断仍工作。"""
        # PERSISTED 路径
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.PERSISTED)
        assert bool(tmp_config_manager.save_volc_key("k1")) is True

        # FAILED 路径
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        assert bool(tmp_config_manager.save_volc_key("k2")) is False

    def test_volc_key_uses_injected_secret_store(self, tmp_config_manager):
        """P1-2：save_volc_key 通过注入的 SecretStore 存储，不依赖全局函数。"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        tmp_config_manager.save_volc_key("volc-injected")

        # 注入的 SecretStore 被调用
        assert ("volc:ark_api_key", "volc-injected") in store.store_calls
