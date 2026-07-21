#!/usr/bin/env python3
"""Application composition root."""

from dataclasses import dataclass

from .app_paths import AppPaths
from .config.config_manager import ConfigManager
from .infrastructure.keyring_secret_store import KeyringSecretStore
from .utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


@dataclass
class AppContext:
    """Configured application services shared by the desktop UI."""

    app_paths: AppPaths
    config_manager: ConfigManager
    secret_store: KeyringSecretStore


def create_app_context() -> AppContext:
    """Create paths, logging, secrets, and configuration services."""
    app_paths = AppPaths.create()
    setup_logging(log_dir=app_paths.log_dir)

    try:
        app_paths.migrate_legacy_config()
    except Exception as exc:
        logger.warning("旧配置迁移失败: %s", exc)

    secret_store = KeyringSecretStore()
    config_manager = ConfigManager(app_paths=app_paths, secret_store=secret_store)
    logger.info("应用上下文初始化完成")
    return AppContext(
        app_paths=app_paths,
        config_manager=config_manager,
        secret_store=secret_store,
    )
