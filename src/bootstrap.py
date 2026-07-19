#!/usr/bin/env python3
"""
Composition Root（组合根）

唯一了解所有具体实现并负责依赖组装的位置。
main.py 调用 create_app_context() 获取所有已配置的服务，
然后将它们传递给 UI 层。

依赖方向：
  bootstrap → infrastructure → domain
  bootstrap → application → domain
  bootstrap → config → domain

bootstrap 不依赖 UI 层。
"""

from dataclasses import dataclass
from pathlib import Path

from .app_paths import AppPaths
from .config.config_manager import ConfigManager
from .domain.edition import EditionCapabilities, detect_edition_capabilities
from .infrastructure.keyring_secret_store import KeyringSecretStore
from .utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


@dataclass
class AppContext:
    """应用上下文：持有所有已配置的服务实例。

    UI 层从此对象获取服务，不再直接实例化 infrastructure 对象。
    """

    app_paths: AppPaths
    config_manager: ConfigManager
    secret_store: KeyringSecretStore
    edition_capabilities: EditionCapabilities


def create_app_context() -> AppContext:
    """组合根入口：创建并组装所有基础服务。

    Returns:
        AppContext: 包含所有已配置服务的上下文对象
    """
    # 1. 路径
    app_paths = AppPaths.create()

    # 2. 日志
    setup_logging(log_dir=app_paths.log_dir)

    # 3. 迁移旧配置（最佳努力，失败不阻塞启动）
    try:
        app_paths.migrate_legacy_config()
    except Exception as exc:
        logger.warning("旧配置迁移失败: %s", exc)

    # 4. SecretStore（密钥环实现）
    secret_store = KeyringSecretStore()

    # 5. ConfigManager（注入 SecretStore）
    config_manager = ConfigManager(
        app_paths=app_paths,
        secret_store=secret_store,
    )

    # P0-2：运行时检测应用版本能力。Text 版本不导入 Manga 模块。
    edition_capabilities = detect_edition_capabilities()
    logger.info(
        "应用上下文初始化完成（edition=%s, manga_enabled=%s）",
        edition_capabilities.edition.value,
        edition_capabilities.manga_enabled,
    )
    return AppContext(
        app_paths=app_paths,
        config_manager=config_manager,
        secret_store=secret_store,
        edition_capabilities=edition_capabilities,
    )


# ── 图片翻译服务组装 ──────────────────────────────────────────


def create_image_translation_registry(
    config_manager: ConfigManager,
    app_paths: AppPaths,
    font_path: Path | None = None,
    edition_capabilities: EditionCapabilities | None = None,
):
    """创建图片翻译 Provider 注册表，按版本能力注册 Provider。

    UI 层应调用此函数获取已配置的注册表，而不是直接创建
    infrastructure 层的 Provider 对象。

    Args:
        config_manager: 配置管理器。
        app_paths: 应用路径。
        font_path: 字体路径（可选，用于 Manga Provider 渲染）。
        edition_capabilities: 版本能力契约。``None`` 时自动检测。
            Text 版本不注册 Manga Provider，避免触碰 Manga 模块。

    Returns:
        ImageTranslationProviderRegistry: 已注册 Provider 的注册表。
    """
    from .infrastructure.image_translation.registry import (
        ImageTranslationProviderRegistry,
    )

    if edition_capabilities is None:
        edition_capabilities = detect_edition_capabilities()

    registry = ImageTranslationProviderRegistry()

    # P0-2：仅在 Full 版本注册 Manga Provider。Text 版本禁止触碰 Manga 模块。
    if edition_capabilities.manga_enabled:
        manga_provider = _create_manga_provider(config_manager, app_paths, font_path)
        if manga_provider is not None:
            registry.register(manga_provider)
    else:
        # P0-2：Text 版本必须禁用注册表的 Manga 开关，
        # 防止任何调用方通过 registry.get(MANGA) 触发 Manga 路径。
        registry.manga_provider_available = False
        logger.info("Text Edition：Manga Provider 未注册（能力契约禁用）")

    # 注册火山 AI Provider（Text/Full 共用）
    from .infrastructure.image_translation.volcengine_provider import (
        VolcengineImageTranslationProvider,
    )

    registry.register(VolcengineImageTranslationProvider(config_manager))

    return registry


def create_image_translation_service(
    config_manager: ConfigManager,
    app_paths: AppPaths,
    font_path: Path | None = None,
    edition_capabilities: EditionCapabilities | None = None,
):
    """创建已配置的 ImageTranslationService。

    UI 层应调用此函数获取已配置的服务，而不是直接创建
    infrastructure 层对象和 application 服务。

    Args:
        config_manager: 配置管理器。
        app_paths: 应用路径。
        font_path: 字体路径（可选）。
        edition_capabilities: 版本能力契约。``None`` 时自动检测。

    Returns:
        ImageTranslationService: 已注入所有依赖的服务实例。
    """
    from .application.image_translation_service import ImageTranslationService
    from .infrastructure.image_translation.manifest_repository import (
        ManifestRepository,
    )

    registry = create_image_translation_registry(
        config_manager,
        app_paths,
        font_path,
        edition_capabilities=edition_capabilities,
    )

    def manifest_repository_factory(mapping_dir: Path) -> ManifestRepository:
        return ManifestRepository(mapping_dir)

    return ImageTranslationService(
        config_manager=config_manager,
        registry=registry,
        manifest_repository_factory=manifest_repository_factory,
    )


def _create_manga_provider(
    config_manager: ConfigManager,
    app_paths: AppPaths,
    font_path: Path | None = None,
):
    """创建 Manga Provider，配置模型目录和质量预设。"""
    from .infrastructure.image_translation.manga_provider import (
        MangaImageTranslationProvider,
    )

    img_config = config_manager.get_image_translation_config()
    manga_cfg = img_config.get("manga", {})

    # 模型目录：AppPaths 用户数据目录下的 models/manga
    model_dir = Path(app_paths.data_dir) / "models" / "manga"
    model_dir.mkdir(parents=True, exist_ok=True)

    return MangaImageTranslationProvider(
        config_manager,
        model_dir=model_dir,
        resource_dir=Path(app_paths.resource_dir),
        font_path=font_path,
        quality_preset=manga_cfg.get("quality_preset", "standard"),
        device=manga_cfg.get("device", "auto"),
        python_executable=manga_cfg.get("python_executable") or None,
    )
