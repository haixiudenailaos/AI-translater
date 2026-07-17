#!/usr/bin/env python3
"""bootstrap.py 组合根测试

验证：
1. create_app_context() 正确组装所有基础服务。
2. create_image_translation_service() 正确注入 Provider 和 manifest 仓储。
3. ConfigManager 通过 SecretStore 协议注入，不再依赖模块级函数。
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


class TestCreateImageTranslationService:
    """图片翻译服务组装测试"""

    def test_service_has_registry_with_providers(self, app_context):
        """create_image_translation_service 返回的 Service 包含已注册的 Provider"""
        from src.bootstrap import create_image_translation_service

        service = create_image_translation_service(
            app_context.config_manager,
            app_context.app_paths,
        )

        # registry 应已注册至少火山引擎 Provider（Manga 可能因依赖未安装而跳过）
        available = service._registry.list_available()
        assert "ai_volcengine" in available, f"火山引擎 Provider 未注册，实际: {available}"

    def test_service_has_manifest_repository_factory(self, app_context):
        """Service 的 manifest_repository_factory 已注入"""
        from src.bootstrap import create_image_translation_service

        service = create_image_translation_service(
            app_context.config_manager,
            app_context.app_paths,
        )

        assert service._manifest_repository_factory is not None

        # 验证 factory 能创建 ManifestRepository
        from pathlib import Path

        repo = service._manifest_repository_factory(Path(app_context.app_paths.data_dir))
        assert repo is not None

    def test_registry_no_global_singleton_dependency(self, app_context):
        """create_image_translation_service 不依赖全局 get_registry() 单例"""
        from src.bootstrap import create_image_translation_service
        from src.infrastructure.image_translation.registry import (
            ImageTranslationProviderRegistry,
        )

        service = create_image_translation_service(
            app_context.config_manager,
            app_context.app_paths,
        )

        # Service 的 registry 应是新建实例，而非全局单例
        assert isinstance(service._registry, ImageTranslationProviderRegistry)
        # 验证不是通过 get_registry() 获取的全局实例
        from src.infrastructure.image_translation.registry import get_registry

        global_registry = get_registry()
        assert service._registry is not global_registry
