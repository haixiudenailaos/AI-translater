#!/usr/bin/env python3
"""P2-7：AppPaths 启动与耐久性语义测试。

验证：
1. ``AI_TRANSLATOR_*`` 环境变量被读取，测试 fixture 不再触碰真实用户目录。
2. 必要目录创建失败时抛出 :class:`AppPathsInitError`，不再返回无效对象。
3. 显式参数优先级高于环境变量。
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.app_paths import AppPaths, AppPathsInitError


class TestAppPathsEnvInjection:
    """P2-7：环境变量注入测试路径。"""

    def test_env_vars_used_when_no_explicit_args(self, tmp_path, monkeypatch):
        """未显式传参时，读取 AI_TRANSLATOR_* 环境变量。"""
        data_dir = tmp_path / "env_data"
        config_dir = tmp_path / "env_config"
        workspace_dir = tmp_path / "env_workspace"
        log_dir = tmp_path / "env_logs"

        monkeypatch.setenv("AI_TRANSLATOR_DATA_DIR", str(data_dir))
        monkeypatch.setenv("AI_TRANSLATOR_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("AI_TRANSLATOR_WORKSPACE_DIR", str(workspace_dir))
        monkeypatch.setenv("AI_TRANSLATOR_LOG_DIR", str(log_dir))

        paths = AppPaths.create()

        assert paths.data_dir == data_dir
        assert paths.config_dir == config_dir
        assert paths.workspace_dir == workspace_dir
        assert paths.log_dir == log_dir
        for d in (data_dir, config_dir, workspace_dir, log_dir):
            assert d.exists(), f"{d} 应被自动创建"

    def test_explicit_args_override_env_vars(self, tmp_path, monkeypatch):
        """显式参数优先级高于环境变量。"""
        env_data = tmp_path / "env_data"
        explicit_data = tmp_path / "explicit_data"
        monkeypatch.setenv("AI_TRANSLATOR_DATA_DIR", str(env_data))

        paths = AppPaths.create(data_dir=explicit_data)

        assert paths.data_dir == explicit_data
        assert paths.data_dir != env_data

    def test_env_vars_isolate_from_real_user_dir(self, tmp_path, monkeypatch):
        """设置环境变量后，AppPaths 不会触碰真实用户目录。

        P2-7 验收：组合根允许显式注入测试路径并断言用户目录零写入。
        """
        monkeypatch.setenv("AI_TRANSLATOR_DATA_DIR", str(tmp_path / "isolated_data"))
        monkeypatch.setenv("AI_TRANSLATOR_CONFIG_DIR", str(tmp_path / "isolated_config"))
        monkeypatch.setenv("AI_TRANSLATOR_LOG_DIR", str(tmp_path / "isolated_logs"))

        # 把 Path.home() 指向一个我们能监控的临时目录
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        with patch("src.app_paths.Path.home", return_value=fake_home):
            paths = AppPaths.create()

        # fake_home 下不应出现 LightNovelTranslator 目录
        assert not (fake_home / "LightNovelTranslator").exists()
        # 真实路径应全部指向 tmp_path 下
        assert paths.data_dir == tmp_path / "isolated_data"
        assert paths.config_dir == tmp_path / "isolated_config"
        assert paths.log_dir == tmp_path / "isolated_logs"

    def test_no_env_vars_uses_platform_default(self, tmp_path, monkeypatch):
        """无环境变量且无显式参数时，使用平台默认用户目录。"""
        for var in (
            "AI_TRANSLATOR_DATA_DIR",
            "AI_TRANSLATOR_CONFIG_DIR",
            "AI_TRANSLATOR_WORKSPACE_DIR",
            "AI_TRANSLATOR_LOG_DIR",
        ):
            monkeypatch.delenv(var, raising=False)

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        with patch("src.app_paths.Path.home", return_value=fake_home):
            paths = AppPaths.create()

        # data_dir 应位于 fake_home 下的平台目录
        assert "LightNovelTranslator" in str(paths.data_dir)
        assert paths.data_dir.exists()


class TestAppPathsInitError:
    """P2-7：必要目录创建失败抛出结构化错误。"""

    def test_raises_when_data_dir_cannot_be_created(self, tmp_path, monkeypatch):
        """data_dir 父路径是文件时，mkdir 失败抛 AppPathsInitError。"""
        blocking_file = tmp_path / "blocker"
        blocking_file.write_text("i-am-a-file", encoding="utf-8")

        # data_dir 指向 blocking_file 的子路径，mkdir 必失败
        invalid_data = blocking_file / "data"

        with pytest.raises(AppPathsInitError) as exc_info:
            AppPaths.create(data_dir=invalid_data)

        # 错误信息包含失败路径与原因
        message = str(exc_info.value)
        assert "AppPaths" in message or "必要目录" in message

    def test_raises_when_config_dir_cannot_be_created(self, tmp_path):
        """config_dir 父路径是文件时抛 AppPathsInitError。"""
        blocking_file = tmp_path / "blocker"
        blocking_file.write_text("i-am-a-file", encoding="utf-8")

        with pytest.raises(AppPathsInitError):
            AppPaths.create(
                data_dir=tmp_path / "ok_data",
                config_dir=blocking_file / "config",
            )

    def test_does_not_return_invalid_object_on_failure(self, tmp_path):
        """失败时不得返回指向无效路径的 AppPaths 对象。"""
        blocking_file = tmp_path / "blocker"
        blocking_file.write_text("i-am-a-file", encoding="utf-8")

        with pytest.raises(AppPathsInitError):
            AppPaths.create(data_dir=blocking_file / "data")
