#!/usr/bin/env python3
"""
ENG-1 回归测试：配置保存聚合结果与关闭流程一致性

覆盖验收标准：
- 模拟磁盘满、只读目录、keyring session-only/failed 时窗口不会静默退出。
- 用户选择与最终持久化一致：重试 / 不保存退出 / 取消。
- 各子部分（API/应用配置/术语表）状态独立聚合，不互相覆盖。
- 错误信息脱敏，不含密钥明文。
"""

import json
import threading
from types import SimpleNamespace

from src.domain.secret import ConfigSaveResult, SecretSaveResult, StorageStatus

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


# ── ConfigSaveResult 单元测试 ──────────────────────────────────────


class TestConfigSaveResultDataclass:
    def test_all_succeed_is_truthy(self):
        result = ConfigSaveResult(
            api=SecretSaveResult(
                secret_status=StorageStatus.PERSISTED,
                config_saved=True,
            ),
            app_config_saved=True,
            glossary_saved=True,
        )
        assert bool(result) is True
        assert not result.failed
        assert not result.session_only
        assert result.user_message == "设置已保存"

    def test_api_failed_makes_result_falsy(self):
        result = ConfigSaveResult(
            api=SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message="keyring 不可用",
            ),
        )
        assert result.failed
        assert bool(result) is False
        assert "API 配置" in result.user_message
        assert "keyring 不可用" in result.user_message

    def test_app_config_failure_is_aggregated(self):
        result = ConfigSaveResult(
            app_config_saved=False,
            app_config_error="磁盘满",
        )
        assert result.failed
        assert bool(result) is False
        assert "应用配置: 磁盘满" in result.user_message

    def test_glossary_failure_is_aggregated(self):
        result = ConfigSaveResult(
            glossary_saved=False,
            glossary_error="权限拒绝",
        )
        assert result.failed
        assert "术语表: 权限拒绝" in result.user_message

    def test_session_only_does_not_fail(self):
        """SESSION_ONLY 不视为失败，但 user_message 提示重启后失效"""
        result = ConfigSaveResult(
            api=SecretSaveResult(
                secret_status=StorageStatus.SESSION_ONLY,
                config_saved=True,
            ),
        )
        assert not result.failed
        assert result.session_only
        assert bool(result) is True
        assert "未持久化" in result.user_message

    def test_multiple_failures_all_listed(self):
        result = ConfigSaveResult(
            api=SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message="keyring 不可用",
            ),
            app_config_saved=False,
            app_config_error="磁盘满",
            glossary_saved=False,
            glossary_error="权限拒绝",
        )
        assert result.failed
        msg = result.user_message
        assert "API 配置" in msg
        assert "应用配置" in msg
        assert "术语表" in msg

    def test_default_values_succeed(self):
        """默认构造（无失败）应视为成功"""
        result = ConfigSaveResult()
        assert bool(result) is True
        assert not result.failed


# ── ConfigManager.save_config 集成测试 ──────────────────────────────────────


class TestSaveConfigAggregatedResult:
    """ENG-1：save_config 返回聚合结果，正确反映各部分状态。"""

    def test_save_config_returns_config_save_result(self, tmp_config_manager):
        """正常保存返回成功的 ConfigSaveResult"""
        result = tmp_config_manager.save_config()
        assert isinstance(result, ConfigSaveResult)
        assert bool(result) is True
        assert not result.failed
        # 配置文件已写入
        assert tmp_config_manager.api_config_file.exists()
        assert tmp_config_manager.app_config_file.exists()
        assert tmp_config_manager.glossary_file.exists()

    def test_save_config_aggregates_api_failure(self, tmp_config_manager):
        """API 密钥保存失败时，整体结果标记为 failed"""
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        # 预先存储一个密钥，让 save_api_config 真正尝试 store
        tmp_config_manager.api_config["api_key"] = "sk-existing-key"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        result = tmp_config_manager.save_config()

        assert result.failed
        assert bool(result) is False
        assert "API 配置" in result.user_message

    def test_save_config_aggregates_app_config_failure(self, tmp_config_manager, monkeypatch):
        """应用配置写入失败（磁盘满）时，整体结果标记为 failed"""
        # 让 app_config_file 的写入抛 OSError
        from src.config import config_manager as cm_mod

        original_write = cm_mod.write_json_atomic
        failing_paths = {tmp_config_manager.app_config_file}

        def fake_write(path, data):
            if path in failing_paths:
                raise OSError("模拟磁盘满")
            return original_write(path, data)

        monkeypatch.setattr(cm_mod, "write_json_atomic", fake_write)

        result = tmp_config_manager.save_config()

        assert result.failed
        assert not result.app_config_saved
        assert "磁盘满" in result.app_config_error
        assert "应用配置" in result.user_message

    def test_save_config_aggregates_glossary_failure(self, tmp_config_manager, monkeypatch):
        """术语表写入失败时，整体结果标记为 failed"""
        from src.config import config_manager as cm_mod

        original_write = cm_mod.write_json_atomic
        failing_paths = {tmp_config_manager.glossary_file}

        def fake_write(path, data):
            if path in failing_paths:
                raise OSError("术语表只读")
            return original_write(path, data)

        monkeypatch.setattr(cm_mod, "write_json_atomic", fake_write)

        result = tmp_config_manager.save_config()

        assert result.failed
        assert not result.glossary_saved
        assert "术语表只读" in result.glossary_error

    def test_save_config_session_only_does_not_fail(self, tmp_config_manager):
        """密钥 SESSION_ONLY 不视为失败，但 result 标记 session_only"""
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)
        tmp_config_manager.api_config["api_key"] = "sk-session-key"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        result = tmp_config_manager.save_config()

        assert not result.failed
        assert result.session_only
        assert bool(result) is True
        assert "未持久化" in result.user_message

    def test_save_config_result_does_not_leak_credentials(self, tmp_config_manager):
        """ENG-1：聚合结果不含密钥明文"""
        secret = "sk-super-secret-12345"
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.PERSISTED)
        tmp_config_manager.api_config["api_key"] = secret
        tmp_config_manager.api_config["provider"] = "siliconflow"

        result = tmp_config_manager.save_config()

        assert secret not in str(result)
        assert secret not in result.user_message
        assert secret not in result.api.error_message

    def test_save_config_partial_failure_keeps_other_results(self, tmp_config_manager, monkeypatch):
        """API 失败但应用配置/术语表仍成功时，结果正确反映各部分状态"""
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager.api_config["api_key"] = "sk-existing"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        result = tmp_config_manager.save_config()

        # API 失败
        assert result.api.failed
        # 应用配置和术语表仍成功
        assert result.app_config_saved
        assert result.glossary_saved
        # 整体失败
        assert result.failed


class TestApiConfigRuntimeSnapshot:
    """API 元数据与密钥必须以一个一致快照对外发布。"""

    def test_metadata_only_snapshot_never_exposes_cached_secret(self, tmp_config_manager):
        tmp_config_manager.api_config = {
            **tmp_config_manager.api_config,
            "provider": "siliconflow",
            "api_key": "sk-cached-secret",
        }
        tmp_config_manager._api_key_provider = "siliconflow"

        metadata = tmp_config_manager.get_api_config(load_secret=False)

        assert metadata["provider"] == "siliconflow"
        assert metadata["api_key"] == ""
        assert tmp_config_manager.get_api_config()["api_key"] == "sk-cached-secret"

    def test_concurrent_save_and_read_never_mix_provider_and_secret(self, tmp_config_manager):
        """读取者只能看到同一 Provider 对应的密钥。"""
        secret_store = FakeSecretStore()
        tmp_config_manager._secret_store = secret_store
        expected_keys = {
            "siliconflow": "sk-siliconflow",
            "deepseek": "sk-deepseek",
        }
        tmp_config_manager.api_config = {
            **tmp_config_manager.api_config,
            "provider": "siliconflow",
            "model_name": "initial-model",
            "api_key": expected_keys["siliconflow"],
        }
        tmp_config_manager._api_key_provider = "siliconflow"
        secret_store.stored["provider:siliconflow"] = expected_keys["siliconflow"]

        start = threading.Event()
        writer_done = threading.Event()
        errors: list[tuple[str, str]] = []
        errors_lock = threading.Lock()

        def writer():
            start.wait()
            for index in range(40):
                provider = "siliconflow" if index % 2 == 0 else "deepseek"
                result = tmp_config_manager.save_api_config(
                    {
                        "provider": provider,
                        "model_name": f"{provider}-model-{index}",
                        "api_key": expected_keys[provider],
                    }
                )
                if not result:
                    with errors_lock:
                        errors.append(("save", result.error_message))
            writer_done.set()

        def reader():
            start.wait()
            while not writer_done.is_set():
                snapshot = tmp_config_manager.get_api_config()
                provider = snapshot["provider"]
                key = snapshot["api_key"]
                if key and key != expected_keys.get(provider):
                    with errors_lock:
                        errors.append((provider, key))
                    return

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(timeout=10.0)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []

    def test_json_failure_keeps_the_previous_secret_metadata_pair(
        self, tmp_config_manager, monkeypatch
    ):
        """未发布的新密钥版本不能改变当前或重启后的运行时配置。"""
        from src.config import config_manager as cm_mod

        secret_store = FakeSecretStore()
        tmp_config_manager._secret_store = secret_store
        assert tmp_config_manager.save_api_config(
            {
                "provider": "siliconflow",
                "model_name": "stable-model",
                "api_key": "sk-stable",
            }
        )
        old_snapshot = tmp_config_manager.get_api_config()
        old_reference = old_snapshot["active_secret_ref"]
        disk_before = json.loads(tmp_config_manager.api_config_file.read_text(encoding="utf-8"))

        original_write = cm_mod.write_json_atomic

        def fail_api_write(path, data):
            if path == tmp_config_manager.api_config_file:
                raise OSError("simulated disk full")
            return original_write(path, data)

        monkeypatch.setattr(cm_mod, "write_json_atomic", fail_api_write)

        result = tmp_config_manager.save_api_config(
            {
                "provider": "deepseek",
                "model_name": "new-model",
                "api_key": "sk-new",
            }
        )

        assert result.failed
        assert tmp_config_manager.get_api_config() == old_snapshot
        assert secret_store.stored == {old_reference: "sk-stable"}
        assert (
            json.loads(tmp_config_manager.api_config_file.read_text(encoding="utf-8"))
            == disk_before
        )

        reloaded = cm_mod.ConfigManager(
            app_paths=SimpleNamespace(config_dir=tmp_config_manager.config_dir),
            secret_store=secret_store,
        )
        assert reloaded.get_api_config()["provider"] == "siliconflow"
        assert reloaded.get_api_config()["api_key"] == "sk-stable"

    def test_public_configuration_snapshots_are_deeply_isolated(self, tmp_config_manager):
        """调用方修改返回的嵌套对象不能污染内存中的未保存状态。"""
        api_snapshot = tmp_config_manager.get_api_config(load_secret=False)
        api_snapshot["cache_config"]["max_entries"] = 1
        assert (
            tmp_config_manager.get_api_config(load_secret=False)["cache_config"]["max_entries"] != 1
        )

        app_snapshot = tmp_config_manager.get_app_config()
        app_snapshot["image_translation"]["ai_volcengine"]["model"] = "changed"
        assert (
            tmp_config_manager.get_app_config()["image_translation"]["ai_volcengine"]["model"]
            != "changed"
        )

        glossary_snapshot = tmp_config_manager.get_glossary()
        glossary_snapshot["categories"].append("测试")
        assert "测试" not in tmp_config_manager.get_glossary()["categories"]

    def test_update_provider_failure_does_not_mutate_live_snapshot(
        self, tmp_config_manager, monkeypatch
    ):
        """兼容入口必须复用保存事务，不能在失败前原地修改配置。"""
        from src.config import config_manager as cm_mod

        secret_store = FakeSecretStore()
        tmp_config_manager._secret_store = secret_store
        assert tmp_config_manager.save_api_config(
            {
                "provider": "siliconflow",
                "model_name": "stable-model",
                "api_key": "sk-stable",
            }
        )
        old_snapshot = tmp_config_manager.get_api_config()

        original_write = cm_mod.write_json_atomic

        def fail_api_write(path, data):
            if path == tmp_config_manager.api_config_file:
                raise OSError("simulated read-only disk")
            return original_write(path, data)

        monkeypatch.setattr(cm_mod, "write_json_atomic", fail_api_write)

        result = tmp_config_manager.update_api_provider_config(
            "deepseek",
            {"model_name": "new-model", "api_key": "sk-new"},
        )

        assert result.failed
        assert tmp_config_manager.get_api_config() == old_snapshot


# ── 关闭流程分支测试（main.py on_closing 的逻辑分支） ─────────────────────────


class TestSaveConfigRetryFlow:
    """ENG-1：验证 _save_config_with_retry 在不同结果下的分支决策。

    不实际启动 Tk mainloop，通过 mock messagebox 验证调用。
    """

    def _make_app_without_mainloop(self, tmp_config_manager):
        """构造一个 TranslatorApp 实例但不进入 mainloop。

        通过 mock 掉 Tk() 和 _initialize_main_window 来避免真正启动 GUI。
        """
        import main as main_mod

        # Mock Tk 以避免实际创建窗口
        class FakeRoot:
            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        class FakeAppContext:
            def __init__(self, config_manager):
                self.config_manager = config_manager

        app = main_mod.TranslatorApp.__new__(main_mod.TranslatorApp)
        app.root = FakeRoot()
        app.app_context = FakeAppContext(tmp_config_manager)
        app.main_window = None
        app._loading_frame = None
        return app

    def test_retry_returns_true_when_save_succeeds(self, tmp_config_manager, monkeypatch):
        """保存成功时返回 True，不弹出任何对话框"""
        app = self._make_app_without_mainloop(tmp_config_manager)

        result = app._save_config_with_retry()

        assert result is True
        assert not app.root.destroyed  # destroy 由调用方负责

    def test_cancel_returns_false_when_save_fails(self, tmp_config_manager, monkeypatch):
        """保存失败且用户选择取消时返回 False"""
        # 让 save_config 始终失败
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager.api_config["api_key"] = "sk-existing"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        app = self._make_app_without_mainloop(tmp_config_manager)

        # Mock messagebox.askyesnocancel 返回 None（取消）
        called_args = {}

        def fake_ask(*args, **kwargs):
            # askyesnocancel(title, message, **kwargs)
            called_args["message"] = kwargs.get("message") or (args[1] if len(args) > 1 else "")
            return None  # 用户取消

        import main as main_mod

        monkeypatch.setattr(main_mod.messagebox, "askyesnocancel", fake_ask)

        result = app._save_config_with_retry()

        assert result is False
        assert "配置保存失败" in called_args["message"]

    def test_exit_without_save_returns_true_when_user_chooses_no(
        self, tmp_config_manager, monkeypatch
    ):
        """保存失败且用户选择'否'（不保存退出）时返回 True"""
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager.api_config["api_key"] = "sk-existing"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        app = self._make_app_without_mainloop(tmp_config_manager)

        import main as main_mod

        # Mock：第一次失败后用户选"否"
        monkeypatch.setattr(main_mod.messagebox, "askyesnocancel", lambda *a, **kw: False)

        result = app._save_config_with_retry()

        assert result is True

    def test_retry_loop_calls_save_again_on_yes(self, tmp_config_manager, monkeypatch):
        """用户选'是'重试时，再次调用 save_config"""
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.FAILED)
        tmp_config_manager.api_config["api_key"] = "sk-existing"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        app = self._make_app_without_mainloop(tmp_config_manager)

        # 计数 save_config 调用次数
        original_save = tmp_config_manager.save_config
        call_count = {"n": 0}

        def counting_save():
            call_count["n"] += 1
            return original_save()

        tmp_config_manager.save_config = counting_save

        import main as main_mod

        # 第一次失败 → 选"是"重试；第二次仍然失败 → 选"否"不保存退出
        choices = iter([True, False])
        monkeypatch.setattr(main_mod.messagebox, "askyesnocancel", lambda *a, **kw: next(choices))

        result = app._save_config_with_retry()

        assert result is True
        assert call_count["n"] == 2  # 调用了两次

    def test_session_only_shows_info_dialog_and_returns_true(self, tmp_config_manager, monkeypatch):
        """SESSION_ONLY 时不阻断，但弹提示对话框"""
        tmp_config_manager._secret_store = FakeSecretStore(status=StorageStatus.SESSION_ONLY)
        tmp_config_manager.api_config["api_key"] = "sk-session"
        tmp_config_manager.api_config["provider"] = "siliconflow"

        app = self._make_app_without_mainloop(tmp_config_manager)

        info_called = {"flag": False, "message": ""}

        def fake_info(title, message, **kwargs):
            info_called["flag"] = True
            info_called["title"] = title
            info_called["message"] = message

        import main as main_mod

        monkeypatch.setattr(main_mod.messagebox, "showinfo", fake_info)

        result = app._save_config_with_retry()

        assert result is True
        assert info_called["flag"]
        assert "未持久化" in info_called["message"]


def test_cancelled_config_save_does_not_teardown_main_window(monkeypatch):
    """P0-1：用户取消失败的配置保存后，主窗口服务必须保持可用。"""
    import main as main_mod
    from src.domain.secret import ConfigSaveResult, SecretSaveResult, StorageStatus

    class FakeRoot:
        def __init__(self):
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    class FakeMainWindow:
        def __init__(self):
            self.close_calls = []

        def confirm_save_before_close(self):
            return "proceed"

        def close(self, decision):
            self.close_calls.append(decision)

    failed_result = ConfigSaveResult(
        api=SecretSaveResult(
            secret_status=StorageStatus.PERSISTED,
            config_saved=False,
        )
    )
    app = main_mod.TranslatorApp.__new__(main_mod.TranslatorApp)
    app.root = FakeRoot()
    app.main_window = FakeMainWindow()
    app.app_context = SimpleNamespace(
        config_manager=SimpleNamespace(save_config=lambda: failed_result)
    )

    monkeypatch.setattr(main_mod.messagebox, "askyesnocancel", lambda *args, **kwargs: None)

    app.on_closing()

    assert app.main_window.close_calls == []
    assert app.root.destroyed is False
