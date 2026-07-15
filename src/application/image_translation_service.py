#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
"""

import datetime
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from ..domain.errors import ImageTranslationCancelled, ImageTranslationConfigError
from ..domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ..domain.translation import OperationStatus
from ..infrastructure.image_translation.manifest_repository import (
    ManifestRepository,
)
from ..infrastructure.image_translation.registry import (
    ImageTranslationProviderRegistry,
    get_registry,
)
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ImageTranslationService:
    """图片翻译统一编排服务"""

    def __init__(
        self,
        config_manager,
        registry: Optional[ImageTranslationProviderRegistry] = None,
    ) -> None:
        self._config_manager = config_manager
        self._registry = registry or get_registry()
        self._lock = threading.Lock()

    # ── Provider 注册 ──────────────────────────────────

    def register_provider(self, provider) -> None:
        """注册 Provider 到注册表。"""
        self._registry.register(provider)

    @property
    def registry(self) -> ImageTranslationProviderRegistry:
        return self._registry

    # ── 校验 ──────────────────────────────────

    def validate_request(self, request: ImageTranslationRequest) -> list[str]:
        """校验请求，返回错误消息列表（空表示可执行）。"""
        errors: list[str] = []

        mapping_dir = Path(request.mapping_dir)
        if not mapping_dir.exists():
            errors.append(f"映射目录不存在: {mapping_dir}")
            return errors

        images_file = mapping_dir / "images.json"
        if not images_file.exists():
            errors.append("缺少 images.json，请确认已正确导入 EPUB")
            return errors

        provider = self._registry.get(request.provider_id)
        if provider is None:
            if request.provider_id == ImageTranslationProviderId.MANGA:
                errors.append(
                    "Manga 翻译引擎未安装或已被禁用，请安装引擎或联系开发者"
                )
            else:
                errors.append(f"未注册的 Provider: {request.provider_id}")
            return errors

        errors.extend(provider.validate(request))
        return errors

    # ── 执行翻译 ──────────────────────────────────

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress: Optional[Callable[[ImageTranslationProgress], None]] = None,
    ) -> ImageTranslationResult:
        """执行图片翻译，写入 manifest 并返回结果。

        不实现自动 fallback：Manga 失败/无文字/缺模型时均不调用 AI。
        """
        with self._lock:
            errors = self.validate_request(request)
            if errors:
                raise ImageTranslationConfigError(
                    "图片翻译前置校验失败: " + "; ".join(errors)
                )

            provider = self._registry.get(request.provider_id)
            assert provider is not None  # validate 已校验

            run_id = uuid.uuid4().hex
            run_at = datetime.datetime.now().astimezone().isoformat()

            try:
                result = provider.translate(request, on_progress)
            except ImageTranslationCancelled:
                # 取消时写入空 manifest，清除旧结果
                self._write_manifest(
                    request.mapping_dir,
                    ImageTranslationResult(
                        status=OperationStatus.CANCELLED,
                        provider_id=request.provider_id,
                        run_id=run_id,
                    ),
                    request,
                    run_at,
                )
                raise
            except Exception as exc:
                logger.error("图片翻译异常: %s", exc, exc_info=True)
                failed = ImageTranslationResult(
                    status=OperationStatus.FAILED,
                    provider_id=request.provider_id,
                    run_id=run_id,
                    failed_images={"_global": self._sanitize_error(str(exc))},
                )
                self._write_manifest(
                    request.mapping_dir, failed, request, run_at
                )
                raise

            # 写入 manifest（即使空结果也覆盖旧文件）
            self._write_manifest(request.mapping_dir, result, request, run_at)
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

    def close_provider(self, provider_id: ImageTranslationProviderId) -> None:
        """关闭指定 Provider，释放资源。"""
        provider = self._registry.get(provider_id)
        if provider is not None:
            try:
                provider.close()
            except Exception as exc:
                logger.warning("关闭 Provider 失败: %s", exc)

    # ── manifest 写入 ──────────────────────────────────

    def _write_manifest(
        self,
        mapping_dir: Path,
        result: ImageTranslationResult,
        request: ImageTranslationRequest,
        run_at: str,
    ) -> None:
        """原子写入 manifest v2。"""
        repo = ManifestRepository(mapping_dir)
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

    # ── 便捷工厂 ──────────────────────────────────

    def make_request(
        self,
        mapping_dir: Path,
        target_language: str,
        provider_id: ImageTranslationProviderId = ImageTranslationProviderId.MANGA,
        selected_images: Optional[tuple] = None,
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
