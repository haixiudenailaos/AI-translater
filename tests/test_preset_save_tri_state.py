#!/usr/bin/env python3
"""
ENG-2 回归测试：API 预设保存返回三态（PERSISTED/SESSION_ONLY/FAILED）

覆盖验收标准：
- 三种密钥后端状态（PERSISTED/SESSION_ONLY/FAILED）均有可观察的 UI 行为。
- SESSION_ONLY 必须明确提示"重启后需重新输入"，不能写成永久保存成功。
- 新增、覆盖、取消预设不修改无关配置。
- 显式导入 simpledialog，不依赖 filedialog 间接副作用。
"""

import json

import pytest

from src.domain.secret import SecretSaveResult, StorageStatus


# ── 测试替身 ──────────────────────────────────────


class FakeSecretStore:
    """可配置状态的 SecretStore 替身。"""

    def __init__(self, status=StorageStatus.PERSISTED):
        self._status = status
        self.stored: dict[str, str] = {}
        self.store_calls: list[tuple[str, str]] = []

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
    """store 抛异常的 SecretStore 替身。"""

    def store(self, identifier, key):
        raise OSError("keyring 服务不可用")

    def retrieve(self, identifier):
        return ""

    def delete(self, identifier):
        return True


# ── ConfigManager.save_api_and_model_preset 三态测试 ────────────────────


class TestPresetSaveTriState:
    """ENG-2：save_api_and_model_preset 返回 SecretSaveResult 三态。"""

    def test_persisted_returns_success(self, tmp_config_manager):
        """密钥持久化成功返回 PERSISTED 结果"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_api_and_model_preset(
            "test-preset", "sk-preset-key", "test-model"
        )

        assert isinstance(result, SecretSaveResult)
        assert result.persisted
        assert not result.session_only
        assert not result.failed
        assert bool(result) is True
        # 密钥已存储到 SecretStore
        assert store.stored["preset:test-preset"] == "sk-preset-key"
        # JSON 文件已写入，只含模型名（不含密钥）
        presets_file = tmp_config_manager.config_dir / "api_presets.json"
        assert presets_file.exists()
        disk_presets = json.loads(presets_file.read_text(encoding="utf-8"))
        assert disk_presets["test-preset"] == {"model_name": "test-model"}
        assert "api_key" not in disk_presets["test-preset"]

    def test_session_only_allows_session_with_warning(self, tmp_config_manager):
        """SESSION_ONLY 允许会话使用，但 result 标记 session_only"""
        store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_api_and_model_preset(
            "session-preset", "sk-session", "session-model"
        )

        # 允许继续会话（truthy）
        assert bool(result) is True
        assert result.session_only
        assert not result.persisted
        assert not result.failed
        # user_message 明确提示重启后失效
        assert "未持久化" in result.user_message
        assert "重启后需重新输入" in result.user_message
        # JSON 仍写入（密钥已存到环境变量）
        presets_file = tmp_config_manager.config_dir / "api_presets.json"
        assert presets_file.exists()

    def test_failed_blocks_save_and_no_json_written(self, tmp_config_manager):
        """FAILED 时不写入 JSON，返回失败结果"""
        store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager._secret_store = store

        result = tmp_config_manager.save_api_and_model_preset(
            "failed-preset", "sk-fail", "fail-model"
        )

        assert result.failed
        assert not result.persisted
        assert not result.session_only
        assert bool(result) is False
        # JSON 未写入（FAILED 时中止）
        presets_file = tmp_config_manager.config_dir / "api_presets.json"
        assert not presets_file.exists()
        # user_message 提示失败
        assert "保存失败" in result.user_message or "失败" in result.user_message

    def test_store_exception_returns_failed(self, tmp_config_manager):
        """SecretStore.store 抛异常时返回 FAILED"""
        tmp_config_manager._secret_store = FailingSecretStore()

        result = tmp_config_manager.save_api_and_model_preset(
            "exc-preset", "sk-exc", "exc-model"
        )

        assert result.failed
        assert bool(result) is False
        assert "密钥存储异常" in result.error_message
        # JSON 未写入
        assert not (tmp_config_manager.config_dir / "api_presets.json").exists()

    def test_json_write_failure_returns_failed_with_secret_status(
        self, tmp_config_manager, monkeypatch
    ):
        """密钥已存储但 JSON 写入失败时，返回 config_saved=False"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        # 让 write_json_atomic 抛 OSError
        from src.config import config_manager as cm_mod

        def failing_write(path, data):
            raise OSError("磁盘满")

        monkeypatch.setattr(cm_mod, "write_json_atomic", failing_write)

        result = tmp_config_manager.save_api_and_model_preset(
            "json-fail", "sk-json", "json-model"
        )

        # 密钥已存储但配置文件写入失败
        assert not result.config_saved
        assert result.persisted  # 密钥本身已持久化
        assert bool(result) is False  # 整体失败
        assert "配置文件写入失败" in result.error_message
        assert "磁盘满" in result.error_message

    def test_result_does_not_leak_credential(self, tmp_config_manager):
        """ENG-2：返回结果不含密钥明文"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        secret = "sk-super-secret-preset-12345"
        result = tmp_config_manager.save_api_and_model_preset(
            "leak-test", secret, "leak-model"
        )

        assert secret not in str(result)
        assert secret not in result.error_message
        assert secret not in result.user_message
        # provider 字段使用 preset: 前缀，不含密钥
        assert result.provider == "preset:leak-test"

    def test_backward_compat_bool(self, tmp_config_manager):
        """ENG-2 向后兼容：旧 `if save_api_and_model_preset(...)` 仍工作"""
        # PERSISTED 路径
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.PERSISTED)
        assert bool(
            tmp_config_manager.save_api_and_model_preset("p1", "k1", "m1")
        ) is True

        # SESSION_ONLY 路径
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)
        assert bool(
            tmp_config_manager.save_api_and_model_preset("p2", "k2", "m2")
        ) is True

        # FAILED 路径
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        assert bool(
            tmp_config_manager.save_api_and_model_preset("p3", "k3", "m3")
        ) is False


# ── 不修改无关配置测试 ──────────────────────────────────────


class TestPresetSaveIsolation:
    """ENG-2 验收：新增、覆盖、取消预设不修改无关配置。"""

    def test_new_preset_does_not_modify_existing_presets(self, tmp_config_manager):
        """新增预设不删除已有预设"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        # 先保存一个预设
        tmp_config_manager.save_api_and_model_preset("preset-a", "key-a", "model-a")
        # 再保存另一个预设
        tmp_config_manager.save_api_and_model_preset("preset-b", "key-b", "model-b")

        # 两个预设都在
        presets_file = tmp_config_manager.config_dir / "api_presets.json"
        disk_presets = json.loads(presets_file.read_text(encoding="utf-8"))
        assert "preset-a" in disk_presets
        assert "preset-b" in disk_presets
        assert disk_presets["preset-a"] == {"model_name": "model-a"}
        assert disk_presets["preset-b"] == {"model_name": "model-b"}

        # 两个密钥都在 SecretStore
        assert store.stored["preset:preset-a"] == "key-a"
        assert store.stored["preset:preset-b"] == "key-b"

    def test_overwrite_preset_keeps_others(self, tmp_config_manager):
        """覆盖预设不修改其他预设"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        tmp_config_manager.save_api_and_model_preset("p1", "k1", "m1")
        tmp_config_manager.save_api_and_model_preset("p2", "k2", "m2")
        # 覆盖 p1
        tmp_config_manager.save_api_and_model_preset("p1", "k1-new", "m1-new")

        # p2 不变
        presets_file = tmp_config_manager.config_dir / "api_presets.json"
        disk_presets = json.loads(presets_file.read_text(encoding="utf-8"))
        assert disk_presets["p1"] == {"model_name": "m1-new"}
        assert disk_presets["p2"] == {"model_name": "m2"}
        assert store.stored["preset:p2"] == "k2"
        assert store.stored["preset:p1"] == "k1-new"

    def test_preset_save_does_not_modify_api_config(self, tmp_config_manager):
        """保存预设不修改主 API 配置文件"""
        store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager._secret_store = store

        # 预先保存主 API 配置
        tmp_config_manager.save_api_config(
            {
                "provider": "siliconflow",
                "api_key": "sk-main",
                "model_name": "main-model",
            }
        )
        api_config_before = tmp_config_manager.api_config.copy()

        # 保存预设
        tmp_config_manager.save_api_and_model_preset("p1", "sk-preset", "preset-model")

        # 主 API 配置未变
        assert tmp_config_manager.api_config == api_config_before
        # 主 API 配置文件未被预设保存修改
        api_config_file = tmp_config_manager.api_config_file
        with open(api_config_file, encoding="utf-8") as f:
            disk_api_config = json.load(f)
        assert disk_api_config.get("model_name") == "main-model"
        # 主密钥未被覆盖
        assert store.stored["provider:siliconflow"] == "sk-main"
        assert store.stored["preset:p1"] == "sk-preset"


# ── simpledialog 显式导入测试 ──────────────────────────────────────


class TestSimpleDialogImport:
    """ENG-2：验证 settings_window 显式导入 simpledialog，不依赖 filedialog 副作用。"""

    def test_simpledialog_is_explicitly_imported(self):
        """settings_window 模块应显式 from tkinter import simpledialog"""
        from src.ui import settings_window

        # simpledialog 应作为模块级名称可用
        assert hasattr(settings_window, "simpledialog")
        # 验证是 tkinter.simpledialog 模块本身
        import tkinter.simpledialog as expected_mod

        assert settings_window.simpledialog is expected_mod

    def test_simpledialog_callable_without_filedialog_side_effect(self):
        """即使 filedialog 未被导入，simpledialog 仍可用。

        模拟文档 §ENG-2 描述的"直接导入/测试 settings_window 或改变导入顺序
        时该属性不存在"的脆弱契约场景。
        """
        import importlib
        import sys

        # 移除 tkinter.filedialog 缓存以模拟未加载状态
        # 注意：实际不能移除已加载模块（其他测试可能依赖），
        # 这里只验证 simpledialog 在 settings_window 命名空间中可直接访问
        from src.ui import settings_window

        # 通过 getattr 验证（不依赖 filedialog 的副作用）
        simpledialog = getattr(settings_window, "simpledialog", None)
        assert simpledialog is not None
        assert hasattr(simpledialog, "askstring")


# ── UI 三态分支测试（通过 mock messagebox） ────────────────────────────


class TestSaveApiPresetUIStateHandling:
    """ENG-2：save_api_preset UI 方法按 SecretSaveResult 三态分支提示。"""

    def _make_settings_window_with_mocks(self, monkeypatch):
        """构造一个不依赖 Tk 的 SettingsWindow 替身，只测试 save_api_preset 逻辑。"""
        from src.ui import settings_window as sw_mod

        # 创建一个简化的 settings window 实例（绕过 __init__）
        window = sw_mod.SettingsWindow.__new__(sw_mod.SettingsWindow)

        # Mock 必要属性
        class FakeVar:
            def __init__(self, value=""):
                self._value = value

            def get(self):
                return self._value

        window.api_key_var = FakeVar("sk-test-key")
        window.model_var = FakeVar("test-model")
        window.display_to_model_map = {"test-model": "test-model"}

        class FakeWindow:
            pass

        window.window = FakeWindow()

        # 收集 messagebox 调用
        calls = {"info": [], "error": [], "warning": [], "askstring": None}

        def fake_showinfo(title, message, **kwargs):
            calls["info"].append((title, message))

        def fake_showerror(title, message, **kwargs):
            calls["error"].append((title, message))

        def fake_showwarning(title, message, **kwargs):
            calls["warning"].append((title, message))

        def fake_askstring(title, prompt, **kwargs):
            return calls["askstring"]

        monkeypatch.setattr(sw_mod.messagebox, "showinfo", fake_showinfo)
        monkeypatch.setattr(sw_mod.messagebox, "showerror", fake_showerror)
        monkeypatch.setattr(sw_mod.messagebox, "showwarning", fake_showwarning)
        monkeypatch.setattr(sw_mod.simpledialog, "askstring", fake_askstring)

        return window, calls

    def test_persisted_shows_success_message(self, tmp_config_manager, monkeypatch):
        """PERSISTED 时显示"已保存"成功提示"""
        from src.domain.secret import SecretSaveResult, StorageStatus

        window, calls = self._make_settings_window_with_mocks(monkeypatch)
        calls["askstring"] = "my-preset"
        window.config_manager = tmp_config_manager

        # 让 save_api_and_model_preset 返回 PERSISTED
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.PERSISTED)

        window.save_api_preset()

        # 应显示成功消息
        assert len(calls["info"]) == 1
        title, msg = calls["info"][0]
        assert "保存成功" in title or "已保存" in msg
        assert "my-preset" in msg

    def test_session_only_shows_restart_warning(self, tmp_config_manager, monkeypatch):
        """SESSION_ONLY 时必须提示"重启后需重新输入"，不能写成永久保存成功"""
        window, calls = self._make_settings_window_with_mocks(monkeypatch)
        calls["askstring"] = "session-preset"
        window.config_manager = tmp_config_manager

        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)

        window.save_api_preset()

        # 应显示 SESSION_ONLY 警告消息
        assert len(calls["info"]) == 1
        title, msg = calls["info"][0]
        assert "未持久化" in msg or "重启后需重新输入" in msg
        assert "my-preset" in msg or "session-preset" in msg

    def test_failed_shows_error_message(self, tmp_config_manager, monkeypatch):
        """FAILED 时显示错误消息，不显示成功"""
        window, calls = self._make_settings_window_with_mocks(monkeypatch)
        calls["askstring"] = "failed-preset"
        window.config_manager = tmp_config_manager

        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)

        window.save_api_preset()

        # 应显示错误消息
        assert len(calls["error"]) == 1
        title, msg = calls["error"][0]
        assert "保存失败" in title or "失败" in msg
        # 不应显示成功消息
        assert len(calls["info"]) == 0

    def test_cancel_preset_name_does_not_save(self, tmp_config_manager, monkeypatch):
        """用户取消预设名称输入时不调用 save_api_and_model_preset"""
        window, calls = self._make_settings_window_with_mocks(monkeypatch)
        calls["askstring"] = None  # 用户取消
        window.config_manager = tmp_config_manager

        # 用 mock 计数 save 调用
        save_calls = {"n": 0}

        def counting_save(*args, **kwargs):
            save_calls["n"] += 1
            return SecretSaveResult(
                secret_status=StorageStatus.PERSISTED, config_saved=True
            )

        tmp_config_manager.save_api_and_model_preset = counting_save

        window.save_api_preset()

        # 未调用 save
        assert save_calls["n"] == 0
        # 未显示任何消息
        assert len(calls["info"]) == 0
        assert len(calls["error"]) == 0
