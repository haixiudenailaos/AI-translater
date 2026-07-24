#!/usr/bin/env python3
"""Application composition root."""

from dataclasses import dataclass
from typing import Optional

from .app_paths import AppPaths
from .config.config_manager import ConfigManager
from .infrastructure.backup_repository import BackupRepository
from .infrastructure.keyring_secret_store import KeyringSecretStore
from .infrastructure.storage_migration import read_interrupted_migration
from .infrastructure.storage_paths import (
    ResolvedStoragePaths,
    StoragePathError,
    StoragePathResolver,
)
from .utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


@dataclass
class AppContext:
    """Configured application services shared by the desktop UI."""

    app_paths: AppPaths
    config_manager: ConfigManager
    secret_store: KeyringSecretStore
    storage_paths: ResolvedStoragePaths
    storage_resolver: StoragePathResolver
    backup_repository: Optional[BackupRepository]
    #: 启动时检测到的中断迁移清单（无则 None），供主窗口提示用户。
    interrupted_migration: Optional[dict]


def _resolve_storage_paths(
    resolver: StoragePathResolver, config_manager: ConfigManager
) -> ResolvedStoragePaths:
    """STORAGE-2/3：启动第二阶段——读取 storage 配置并解析最终路径。

    分阶段初始化的原因（§5 第一步）：ConfigManager 需要先找到平台默认
    配置目录下的 app_config.json，AppPaths.create 不能反过来依赖配置，
    否则形成启动循环。

    健壮性：storage 配置在保存时已校验，但磁盘状态可能在两次启动间
    变化（目录被删除、盘符拔出）。解析或建目录失败时回退到平台默认
    目录并记录错误，绝不让启动崩溃（旧配置保留，用户可在设置页修复）。
    """
    storage = config_manager.get_storage_config()
    try:
        resolved = resolver.resolve(storage)
        issues = resolver.validate(resolved)
        for issue in issues:
            if issue.is_error:
                logger.error("数据目录配置存在问题 [%s]: %s", issue.field, issue.message)
            else:
                logger.warning("数据目录提示 [%s]: %s", issue.field, issue.message)
        resolver.ensure_directories(resolved)
        return resolved
    except StoragePathError as exc:
        logger.error(
            "数据目录配置不可用（%s），本次启动回退到平台默认目录: %s",
            exc,
            storage,
        )
        fallback = resolver.resolve(None)
        resolver.ensure_directories(fallback)
        return fallback


def create_app_context() -> AppContext:
    """Create paths, logging, secrets, and configuration services."""
    # 阶段 1：平台默认路径 + 日志 + 配置（日志目录固定在 AppPaths.log_dir，
    # setup_logging 一次性初始化，storage.logs_dir 本版本为预留字段）
    app_paths = AppPaths.create()
    setup_logging(log_dir=app_paths.log_dir)

    try:
        app_paths.migrate_legacy_config()
    except Exception as exc:
        logger.warning("旧配置迁移失败: %s", exc)

    secret_store = KeyringSecretStore()
    config_manager = ConfigManager(app_paths=app_paths, secret_store=secret_store)

    # 阶段 2：解析用户自定义数据目录（STORAGE-2）
    storage_resolver = StoragePathResolver(app_paths)
    storage_paths = _resolve_storage_paths(storage_resolver, config_manager)

    # STORAGE-5：识别上次未完成的迁移（staging/failed），交给 UI 提示；
    # 不把新目录当作完整数据目录继续使用——配置在迁移失败时保持旧值，
    # 因此上面的 storage_paths 仍指向完整可用的旧目录。
    interrupted = None
    try:
        interrupted = read_interrupted_migration(storage_paths.data_root)
        if interrupted is not None:
            logger.warning(
                "检测到未完成的数据迁移（status=%s，target=%s）",
                interrupted.get("status"),
                interrupted.get("target_root"),
            )
    except Exception as exc:  # noqa: BLE001 - 迁移检测失败不阻断启动
        logger.warning("迁移状态检测失败: %s", exc)

    # STORAGE-6：统一译文备份仓库（初始化失败不阻断启动，备份功能降级）
    backup_repository = None
    try:
        backup_repository = BackupRepository(storage_paths.translation_backups_dir)
    except Exception as exc:  # noqa: BLE001
        logger.error("初始化译文备份仓库失败，备份功能降级: %s", exc)

    logger.info("应用上下文初始化完成")
    return AppContext(
        app_paths=app_paths,
        config_manager=config_manager,
        secret_store=secret_store,
        storage_paths=storage_paths,
        storage_resolver=storage_resolver,
        backup_repository=backup_repository,
        interrupted_migration=interrupted,
    )
