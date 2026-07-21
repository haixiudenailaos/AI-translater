#!/usr/bin/env python3
"""bootstrap.py 组合根测试

验证：
1. create_app_context() 正确组装所有基础服务。
2. ConfigManager 通过 SecretStore 协议注入，不再依赖模块级函数。
"""

import pytest

from src.bootstrap import AppContext, create_app_context


@pytest.fixture
def app_context(tmp_path, monkeypatch):
    """创建使用临时目录的 AppContext。"""
    monkeypatch.setenv("AI_TRANSLATOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AI_TRANSLATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_TRANSLATOR_LOG_DIR", str(tmp_path / "logs"))
    return create_app_context()


class TestCreateAppContext:
    """组合根入口测试"""

    def test_returns_app_context_with_all_fields(self, app_context):
        """create_app_context 返回的 AppContext 包含所有必要字段"""
        assert isinstance(app_context, AppContext)
        assert app_context.app_paths is not None
        assert app_context.config_manager is not None
        assert app_context.secret_store is not None

    def test_config_manager_has_secret_store_injected(self, app_context):
        """ConfigManager 的 SecretStore 已被注入（P1-2 验证）"""
        assert app_context.config_manager._secret_store is app_context.secret_store

    def test_config_dir_created(self, app_context, tmp_path):
        """配置目录已创建"""
        assert app_context.app_paths.config_dir.exists()

    def test_log_dir_created(self, app_context, tmp_path):
        """日志目录已创建"""
        assert app_context.app_paths.log_dir.exists()
