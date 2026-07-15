# -*- coding: utf-8 -*-
"""
图片翻译基础设施层

包含 Provider 注册表、Manga/AI 适配器、manifest 仓库、运行时和语言映射。
所有 UI、导入器和队列只通过 ImageTranslationService 调用，不直接实例化
本包内的 Provider。
"""

from .registry import ImageTranslationProviderRegistry, get_registry
from .manifest_repository import ManifestRepository, ManifestData

__all__ = [
    "ImageTranslationProviderRegistry",
    "get_registry",
    "ManifestRepository",
    "ManifestData",
]
