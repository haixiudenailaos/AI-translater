#!/usr/bin/env python3
"""
图片翻译应用服务

统一负责：
- 校验 mapping_dir/images.json。
- 按显式 provider_id 从注册表取得 Provider（不实现自动 fallback）。
- 组织进度、取消和最终状态。
- 将结果以原子方式写入 manifest（v2）。
- 更新项目的 image_translation_done / image_translation_failed 状态。
- 关闭 Provider 生命周期。

UI、导入器和队列只能调用该 Service，不再直接实例化 ImageTranslator
或 ImageTextTranslator。

P1-1：manifest 写入失败不再被吞掉，Provider 成功但保存失败时抛出
      ImageManifestPersistenceError，UI 不得报告整体成功，并提供重试入口。
P2-2：构造函数只接收 Protocol（registry / manifest_repository_factory），
      不再依赖全局 get_registry() 单例；具体实现在 bootstrap.py 注入。
"""

import datetime
import threading
import uuid
from pathlib import Path
from typing import Callable

from ..domain.errors import (
    ImageManifestPersistenceError,
    ImageTranslationCancelled,
    ImageTranslationConfigError,
)
from ..domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ..domain.translation import OperationStatus
from ..infrastructure.mapping_repository import resolve_mapping_file
from ..utils.logger import get_logger
from .ports import ImageManifestRepository, ImageProviderRegistry

logger = get_logger(__name__)


class ImageTranslationService:
    """图片翻译统一编排服务"""

    def __init__(
        self,
        config_manager,
        registry: ImageProviderRegistry,
        manifest_repository_factory: Callable[[Path], ImageManifestRepository] | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
        id_generator: Callable[[], str] | None = None,
    ) -> None:
        """
        Args:
            config_manager: 配置管理器（读取目标语言、图片翻译配置）。
            registry: Provider 注册表（Protocol），不再使用全局单例。
            manifest_repository_factory: 按 mapping_dir 创建 manifest 仓储的工厂。
                为 None 时使用默认实现（兼容层，待 bootstrap 完成后由调用方注入）。
            clock: 时间源（测试可注入替身），默认系统时间。
            id_generator: 运行 ID 生成器（测试可注入替身），默认 uuid4。
        """
        self._config_manager = config_manager
        self._registry = registry
        self._manifest_repository_factory = manifest_repository_factory
        self._clock = clock or (lambda: datetime.datetime.now().astimezone())
        self._id_generator = id_generator or (lambda: uuid.uuid4().hex)
        self._lock = threading.Lock()

    # ── Provider 注册 ──────────────────────────────────

    def register_provider(self, provider) -> None:
        """注册 Provider 到注册表。"""
        self._registry.register(provider)

    @property
    def registry(self) -> ImageProviderRegistry:
        return self._registry

    # ── 校验 ──────────────────────────────────

    def validate_request(self, request: ImageTranslationRequest) -> list[str]:
        """校验请求，返回错误消息列表（空表示可执行）。"""
        errors: list[str] = []

        mapping_dir = Path(request.mapping_dir)
        if not mapping_dir.exists():
            errors.append(f"映射目录不存在: {mapping_dir}")
            return errors

        images_file = resolve_mapping_file(mapping_dir, "images.json")
        if not images_file.exists():
            errors.append("缺少 images.json，请确认已正确导入 EPUB")
            return errors

        provider = self._registry.get(request.provider_id)
        if provider is None:
            if request.provider_id == ImageTranslationProviderId.MANGA:
                errors.append("Manga 翻译引擎未安装或已被禁用，请安装引擎或联系开发者")
            else:
                errors.append(f"未注册的 Provider: {request.provider_id}")
            return errors

        errors.extend(provider.validate(request))
        return errors

    # ── manifest 仓储 ──────────────────────────────────

    def _get_manifest_repository(self, mapping_dir: Path) -> ImageManifestRepository:
        """取得 manifest 仓储实例。

        优先使用注入的工厂；未注入时延迟导入默认实现（兼容层）。
        bootstrap 完成后所有调用方应注入工厂，移除此处对 infrastructure 的依赖。
        """
        if self._manifest_repository_factory is not None:
            return self._manifest_repository_factory(Path(mapping_dir))
        # 兼容层：bootstrap 尚未接入时使用默认实现
        from ..infrastructure.image_translation.manifest_repository import (
            ManifestRepository,
        )

        return ManifestRepository(Path(mapping_dir))

    def save_manifest(
        self,
        mapping_dir: Path,
        result: ImageTranslationResult,
        request: ImageTranslationRequest,
        run_at: str = "",
    ) -> None:
        """公开入口：保存 manifest（供 UI 在持久化失败后重试调用）。

        P1-1：失败时抛出 ImageManifestPersistenceError，不静默吞掉。
        """
        if not run_at:
            run_at = self._clock().isoformat()
        repo = self._get_manifest_repository(mapping_dir)
        try:
            if result.is_empty_run and result.status == OperationStatus.CANCELLED:
                repo.save_empty(run_at=run_at)
            else:
                repo.save(
                    result,
                    source_fingerprint=request.source_fingerprint,
                    config_fingerprint=request.config_fingerprint,
                    run_at=run_at,
                )
        except Exception as exc:
            logger.error("写入 manifest 失败: %s", exc)
            raise ImageManifestPersistenceError(
                "图片翻译已完成，但结果保存失败",
                partial_result=result,
                mapping_dir=Path(mapping_dir),
                run_at=run_at,
            ) from exc

    # ── 执行翻译 ──────────────────────────────────

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress: Callable[[ImageTranslationProgress], None] | None = None,
    ) -> ImageTranslationResult:
        """执行图片翻译，写入 manifest 并返回结果。

        不实现自动 fallback：Manga 失败/无文字/缺模型时均不调用 AI。

        P1-1：Provider 成功但 manifest 保存失败时，抛出
              ImageManifestPersistenceError（携带 partial_result），不返回 SUCCEEDED。
        """
        with self._lock:
            errors = self.validate_request(request)
            if errors:
                raise ImageTranslationConfigError("图片翻译前置校验失败: " + "; ".join(errors))

            provider = self._registry.get(request.provider_id)
            assert provider is not None  # validate 已校验

            run_id = self._id_generator()
            run_at = self._clock().isoformat()

            try:
                result = provider.translate(request, on_progress)
            except ImageTranslationCancelled:
                # 取消时写入空 manifest，清除旧结果
                cancelled_result = ImageTranslationResult(
                    status=OperationStatus.CANCELLED,
                    provider_id=request.provider_id,
                    run_id=run_id,
                )
                # 取消路径的 manifest 失败仍向上传递（P1-1），
                # 但取消语义优先：抛出 ImageTranslationCancelled。
                try:
                    self.save_manifest(
                        Path(request.mapping_dir),
                        cancelled_result,
                        request,
                        run_at=run_at,
                    )
                except ImageManifestPersistenceError as persist_exc:
                    logger.error("取消后写入 manifest 失败: %s", persist_exc)
                raise
            except Exception as exc:
                logger.error("图片翻译异常: %s", exc, exc_info=True)
                failed = ImageTranslationResult(
                    status=OperationStatus.FAILED,
                    provider_id=request.provider_id,
                    run_id=run_id,
                    failed_images={"_global": self._sanitize_error(str(exc))},
                )
                # 失败路径的 manifest 失败向上传递（P1-1）
                self.save_manifest(Path(request.mapping_dir), failed, request, run_at=run_at)
                raise

            # P1-1：Provider 成功但 manifest 保存失败时，不得返回 SUCCEEDED。
            # 抛出 ImageManifestPersistenceError，携带 partial_result 供 UI 重试。
            self.save_manifest(Path(request.mapping_dir), result, request, run_at=run_at)
            return result

    # ── 取消 ──────────────────────────────────

    def cancel(self, provider_id: ImageTranslationProviderId) -> None:
        """取消指定 Provider 进行中的翻译。"""
        provider = self._registry.get(provider_id)
        if provider is not None:
            try:
                provider.cancel()
            except Exception as exc:
                logger.warning("取消 Provider 失败: %s", exc)

    def close_provider(
        self,
        provider_id: ImageTranslationProviderId,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """关闭指定 Provider，释放资源。"""
        provider = self._registry.get(provider_id)
        if provider is not None:
            try:
                provider.close(timeout_seconds=timeout_seconds)
            except Exception as exc:
                logger.warning("关闭 Provider 失败: %s", exc)

    # ── 便捷工厂 ──────────────────────────────────

    def make_request(
        self,
        mapping_dir: Path,
        target_language: str,
        provider_id: ImageTranslationProviderId = ImageTranslationProviderId.MANGA,
        selected_images: tuple | None = None,
        source_fingerprint: str = "",
        config_fingerprint: str = "",
    ) -> ImageTranslationRequest:
        """构造翻译请求。"""
        return ImageTranslationRequest(
            mapping_dir=Path(mapping_dir),
            target_language=target_language,
            provider_id=provider_id,
            selected_images=selected_images,
            source_fingerprint=source_fingerprint,
            config_fingerprint=config_fingerprint,
        )

    @staticmethod
    def _sanitize_error(msg: str) -> str:
        text = str(msg)
        for token in ("api_key", "apikey", "Bearer ", "authorization"):
            if token.lower() in text.lower():
                return "[filtered: contains credential token]"
        if len(text) > 300:
            text = text[:300] + "...(truncated)"
        return text
