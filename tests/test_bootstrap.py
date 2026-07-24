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


class TestStorageWiring:
    """STORAGE-2/3：组合根的数据目录装配。"""

    def test_storage_paths_resolved_from_default_config(self, app_context):
        """默认配置下 storage_paths 与平台默认布局一致"""
        assert app_context.storage_paths is not None
        assert app_context.storage_paths.is_default is True
        assert (
            app_context.storage_paths.projects_dir
            == (app_context.app_paths.data_dir / "projects").resolve()
        )

    def test_storage_resolver_available(self, app_context):
        assert app_context.storage_resolver is not None

    def test_backup_repository_created(self, app_context):
        """备份仓库指向解析后的备份目录"""
        assert app_context.backup_repository is not None
        assert (
            app_context.backup_repository.backups_dir
            == app_context.storage_paths.translation_backups_dir
        )
        assert app_context.storage_paths.translation_backups_dir.is_dir()

    def test_no_interrupted_migration_by_default(self, app_context):
        assert app_context.interrupted_migration is None

    def test_custom_data_root_honored(self, tmp_path, monkeypatch):
        """STORAGE-3：配置中的 data_root 在组合根装配后生效"""
        from src.config.config_manager import ConfigManager
        from src.app_paths import AppPaths
        from src.infrastructure.keyring_secret_store import KeyringSecretStore

        monkeypatch.setenv("AI_TRANSLATOR_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("AI_TRANSLATOR_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("AI_TRANSLATOR_LOG_DIR", str(tmp_path / "logs"))

        # 先写入自定义 storage 配置
        seed_paths = AppPaths.create()
        seed_config = ConfigManager(app_paths=seed_paths, secret_store=KeyringSecretStore())
        assert seed_config.update_storage_config(
            {"data_root": str(tmp_path / "custom_root")}
        )

        ctx = create_app_context()
        assert ctx.storage_paths.data_root == (tmp_path / "custom_root").resolve()
        assert ctx.storage_paths.projects_dir == (
            tmp_path / "custom_root" / "translation_records" / "projects"
        ).resolve()
        assert ctx.storage_paths.projects_dir.is_dir()

    def test_broken_storage_config_falls_back_to_default(self, tmp_path, monkeypatch):
        """磁盘状态恶化（目标在文件之下）时回退默认目录，启动不崩溃"""
        from src.config.config_manager import ConfigManager
        from src.app_paths import AppPaths
        from src.infrastructure.keyring_secret_store import KeyringSecretStore

        monkeypatch.setenv("AI_TRANSLATOR_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("AI_TRANSLATOR_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("AI_TRANSLATOR_LOG_DIR", str(tmp_path / "logs"))

        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")

        seed_paths = AppPaths.create()
        seed_config = ConfigManager(app_paths=seed_paths, secret_store=KeyringSecretStore())
        assert seed_config.update_storage_config({"data_root": str(blocker / "sub")})

        ctx = create_app_context()
        # 回退到平台默认目录
        assert ctx.storage_paths.is_default is True
        assert ctx.storage_paths.projects_dir.is_dir()
