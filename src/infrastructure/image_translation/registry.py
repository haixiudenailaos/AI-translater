#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片翻译 Provider 注册表

按显式 provider_id 取得 Provider。注册表本身不决定默认值切换，
不实现自动 fallback——默认 Provider 永远为 Manga。

回滚：保留功能开关 manga_provider_available，用于出现打包级故障时
禁用 Manga 入口并展示错误；该开关不得自动改选 AI。
"""

from typing import Optional

from ...domain.image_translation import ImageTranslationProviderId
from ...utils.logger import get_logger

logger = get_logger(__name__)


class ImageTranslationProviderRegistry:
    """Provider 注册表"""

    def __init__(self) -> None:
        self._providers: dict[str, object] = {}
        self._manga_provider_available: bool = True

    def register(self, provider: object) -> None:
        """注册一个 Provider，按其 provider_id 索引。"""
        pid = getattr(provider, "provider_id", None)
        if not pid:
            raise ValueError("Provider 缺少 provider_id 属性")
        self._providers[str(pid)] = provider
        logger.info("已注册图片翻译 Provider: %s", pid)

    def get(self, provider_id: str | ImageTranslationProviderId) -> Optional[object]:
        """按 provider_id 取得 Provider。

        Manga Provider 在禁用开关关闭时返回 None，调用方应展示错误，
        不自动改选 AI。
        """
        pid = (
            provider_id.value
            if isinstance(provider_id, ImageTranslationProviderId)
            else str(provider_id)
        )
        if pid == ImageTranslationProviderId.MANGA.value:
            if not self._manga_provider_available:
                logger.warning("Manga Provider 已被功能开关禁用")
                return None
        return self._providers.get(pid)

    def list_available(self) -> list[str]:
        """列出已注册的 Provider id。"""
        ids = list(self._providers.keys())
        if not self._manga_provider_available:
            manga_id = ImageTranslationProviderId.MANGA.value
            ids = [i for i in ids if i != manga_id]
        return ids

    @property
    def manga_provider_available(self) -> bool:
        return self._manga_provider_available

    @manga_provider_available.setter
    def manga_provider_available(self, value: bool) -> None:
        self._manga_provider_available = bool(value)

    def is_registered(self, provider_id: str | ImageTranslationProviderId) -> bool:
        pid = (
            provider_id.value
            if isinstance(provider_id, ImageTranslationProviderId)
            else str(provider_id)
        )
        if pid == ImageTranslationProviderId.MANGA.value:
            return self._manga_provider_available and pid in self._providers
        return pid in self._providers


# 进程级单例注册表
_registry: Optional[ImageTranslationProviderRegistry] = None


def get_registry() -> ImageTranslationProviderRegistry:
    """获取进程级单例注册表。"""
    global _registry
    if _registry is None:
        _registry = ImageTranslationProviderRegistry()
    return _registry


def reset_registry() -> None:
    """重置单例注册表（测试用）。"""
    global _registry
    _registry = None
