#!/usr/bin/env python3
"""LC-01 / LC-02：超长上下文配置的往返、校验与迁移。

对应实现指南 §3.1 与验收表 LC-01、LC-02：

- LC-01：保存 131,072 / 1,048,576 后重新初始化 ``ConfigManager``，
  用户值完整往返，不被旧 token 字段清理删除或裁剪。
- LC-02：旧配置、损坏类型、0/负数/小数/布尔值、未知 schema 按规范校验或
  迁移；写盘失败不发布新配置；配置外其他字段保持兼容。
"""

import json

import pytest

from src.config.config_manager import ConfigManager
from src.config.long_context_config import (
    DEFAULT_LONG_CONTEXT_CONFIG,
    LONG_CONTEXT_CONFIG_KEY,
    normalize_long_context_config,
    read_context_window_tokens,
)
from src.domain.translation_policy import LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS


class TestNormalization:
    def test_defaults_when_section_missing(self):
        assert normalize_long_context_config(None) == DEFAULT_LONG_CONTEXT_CONFIG
        assert normalize_long_context_config({}) == DEFAULT_LONG_CONTEXT_CONFIG

    def test_accepts_large_positive_integers(self):
        for value in (131072, 1_048_576, 32_768, 1):
            normalized = normalize_long_context_config(
                {"schema_version": 1, "context_window_tokens": value}
            )
            assert normalized["context_window_tokens"] == value

    def test_accepts_numeric_strings_from_tk_entry(self):
        normalized = normalize_long_context_config(
            {"schema_version": 1, "context_window_tokens": "  65536  "}
        )
        assert normalized["context_window_tokens"] == 65536

    @pytest.mark.parametrize(
        "value",
        [
            0,
            -1,
            -131072,
            1.5,
            True,
            False,
            "",
            "abc",
            "12.5",
            "1e6",
            None,
            [],
            {},
            "０１２",  # 全角数字：isdigit() 为真但 int() 不接受
        ],
    )
    def test_rejects_invalid_values_with_diagnosable_default(self, value):
        normalized = normalize_long_context_config(
            {"schema_version": 1, "context_window_tokens": value}
        )
        assert normalized["context_window_tokens"] == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS

    def test_does_not_truncate_floats(self):
        """``int(1.5)`` 静默截断必须被拒绝，而不是变成 1。"""
        normalized = normalize_long_context_config({"context_window_tokens": 1.5})
        assert normalized["context_window_tokens"] == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS

    def test_normalization_is_idempotent(self):
        raw = {"schema_version": 1, "context_window_tokens": "131072", "extra": {"a": 1}}
        once = normalize_long_context_config(raw)
        assert normalize_long_context_config(once) == once

    def test_preserves_unknown_fields(self):
        normalized = normalize_long_context_config(
            {"schema_version": 1, "context_window_tokens": 4096, "future_field": 7}
        )
        assert normalized["future_field"] == 7

    def test_newer_schema_is_not_rewritten_downwards(self):
        normalized = normalize_long_context_config(
            {"schema_version": 99, "context_window_tokens": 262144}
        )
        assert normalized["schema_version"] == 99
        assert normalized["context_window_tokens"] == 262144

    def test_corrupt_section_type_falls_back(self):
        assert normalize_long_context_config("not-a-dict") == DEFAULT_LONG_CONTEXT_CONFIG
        assert normalize_long_context_config([1, 2]) == DEFAULT_LONG_CONTEXT_CONFIG

    def test_read_helper_tolerates_corruption(self):
        assert read_context_window_tokens({}) == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS
        assert (
            read_context_window_tokens({LONG_CONTEXT_CONFIG_KEY: "broken"})
            == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS
        )
        assert (
            read_context_window_tokens({LONG_CONTEXT_CONFIG_KEY: {"context_window_tokens": 65536}})
            == 65536
        )


class TestConfigManagerRoundTrip:
    """LC-01：不经旧字段清理、不被裁剪的磁盘往返。"""

    @pytest.mark.parametrize("value", [131072, 1_048_576])
    def test_round_trip_survives_reinitialization(self, tmp_app_paths, value):
        from src.config.config_manager import ConfigManager

        class InMemorySecretStore:
            def store(self, identifier, key):  # pragma: no cover - 未使用
                from src.domain.secret import StorageStatus

                return StorageStatus.PERSISTED

            def retrieve(self, identifier):  # pragma: no cover - 未使用
                return ""

            def delete(self, identifier):  # pragma: no cover - 未使用
                return True

        manager = ConfigManager(app_paths=tmp_app_paths, secret_store=InMemorySecretStore())
        config = manager.get_app_config()
        config[LONG_CONTEXT_CONFIG_KEY] = {
            "schema_version": 1,
            "context_window_tokens": value,
        }
        assert manager.save_app_config(config) is True

        # 重新初始化（模拟重启应用）后用户值必须完整保留。
        reloaded = ConfigManager(app_paths=tmp_app_paths, secret_store=InMemorySecretStore())
        assert reloaded.get_app_config()[LONG_CONTEXT_CONFIG_KEY]["context_window_tokens"] == value
        assert read_context_window_tokens(reloaded.get_app_config()) == value

    def test_legacy_config_without_section_gets_default(self, tmp_app_paths):
        app_config_file = tmp_app_paths.config_dir / "app_config.json"
        app_config_file.parent.mkdir(parents=True, exist_ok=True)
        app_config_file.write_text(
            json.dumps({"target_language": "英文", "batch_lines": 12}), encoding="utf-8"
        )
        manager = ConfigManager(app_paths=tmp_app_paths)
        config = manager.get_app_config()
        assert config["target_language"] == "英文"
        assert config["batch_lines"] == 12
        assert (
            config[LONG_CONTEXT_CONFIG_KEY]["context_window_tokens"]
            == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS
        )

    def test_corrupt_section_on_disk_is_normalized(self, tmp_app_paths):
        app_config_file = tmp_app_paths.config_dir / "app_config.json"
        app_config_file.parent.mkdir(parents=True, exist_ok=True)
        app_config_file.write_text(
            json.dumps({LONG_CONTEXT_CONFIG_KEY: {"context_window_tokens": -5}}),
            encoding="utf-8",
        )
        manager = ConfigManager(app_paths=tmp_app_paths)
        assert (
            manager.get_app_config()[LONG_CONTEXT_CONFIG_KEY]["context_window_tokens"]
            == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS
        )

    def test_other_fields_stay_compatible(self, tmp_app_paths):
        manager = ConfigManager(app_paths=tmp_app_paths)
        config = manager.get_app_config()
        config[LONG_CONTEXT_CONFIG_KEY] = {"schema_version": 1, "context_window_tokens": 65536}
        assert manager.save_app_config(config)
        reloaded = ConfigManager(app_paths=tmp_app_paths).get_app_config()
        assert reloaded["target_language"] == config["target_language"]
        assert reloaded["queue_batch_lines"] == config["queue_batch_lines"]
        assert reloaded["storage"] == config["storage"]

    def test_save_failure_keeps_previous_file(self, tmp_app_paths, monkeypatch):
        """LC-02：写盘失败不发布新配置、不破坏旧文件。"""
        manager = ConfigManager(app_paths=tmp_app_paths)
        config = manager.get_app_config()
        config[LONG_CONTEXT_CONFIG_KEY] = {"schema_version": 1, "context_window_tokens": 4096}
        assert manager.save_app_config(config)

        new_config = manager.get_app_config()
        new_config[LONG_CONTEXT_CONFIG_KEY] = {
            "schema_version": 1,
            "context_window_tokens": 1_048_576,
        }

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("src.config.config_manager.write_json_atomic", boom)
        assert manager.save_app_config(new_config) is False
        # 内存中的已发布配置不得被失败候选覆盖。
        assert read_context_window_tokens(manager.get_app_config()) == 4096
        reloaded = ConfigManager(app_paths=tmp_app_paths).get_app_config()
        assert read_context_window_tokens(reloaded) == 4096

    def test_offline_manager_reports_limit_not_clamp(self, tmp_app_paths):
        """保存大于模型容量的预算：不改写用户值，由预算解析展示限制原因。"""
        from src.application.context_budget import describe_budget, resolve_long_budget

        manager = ConfigManager(app_paths=tmp_app_paths)
        config = manager.get_app_config()
        config[LONG_CONTEXT_CONFIG_KEY] = {"schema_version": 1, "context_window_tokens": 1_048_576}
        assert manager.save_app_config(config)

        reloaded = ConfigManager(app_paths=tmp_app_paths)
        api_config = reloaded.get_api_config(load_secret=False)
        requested = read_context_window_tokens(reloaded.get_app_config())
        budget = resolve_long_budget(
            requested_context_tokens=requested,
            model_context_tokens=api_config["context_window_tokens"],
        )
        assert requested == 1_048_576
        assert budget.requested_context_tokens == 1_048_576
        assert budget.effective_context_tokens == api_config["context_window_tokens"]
        narrative = describe_budget(
            budget, model_context_tokens=api_config["context_window_tokens"]
        )
        assert "1,048,576" in narrative.text
        assert "有效预算" in narrative.text
