#!/usr/bin/env python3
"""P0-2：应用发行版本与能力契约。

Text Edition（默认下载版本）只包含 TXT/EPUB 文本翻译 + 在线 AI 图片 Provider，
明确排除本地 Manga 推理重依赖（torch / cv2 / onnxruntime / manga_translator）。

Full Edition 在 Text 基础上加入 manga-image-translator 全部依赖，
保留本地 Manga Provider 与 AI Provider 两个图片翻译入口。

``EditionCapabilities`` 是版本能力的唯一真相来源。UI 层和 bootstrap 都应
依据该对象决定是否注册 Manga Provider，而不是无条件 ``import``——
Text 版的 PyInstaller spec 已经在 ``excludes`` 中排除 ``manga_translator``，
运行时导入必然失败。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Edition(str, Enum):
    """应用发行版本。"""

    FULL = "full"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class EditionCapabilities:
    """版本能力契约。

    - ``manga_enabled`` 决定是否允许注册 Manga Provider、是否启用
      Manga 入口按钮。Text 版必须为 ``False``，避免触碰 Manga 模块。
    - 该对象不可变；UI 和 bootstrap 共享同一实例。
    """

    edition: Edition
    manga_enabled: bool

    @classmethod
    def full(cls) -> EditionCapabilities:
        return cls(edition=Edition.FULL, manga_enabled=True)

    @classmethod
    def text(cls) -> EditionCapabilities:
        return cls(edition=Edition.TEXT, manga_enabled=False)

    @property
    def is_full(self) -> bool:
        return self.edition is Edition.FULL

    @property
    def is_text(self) -> bool:
        return self.edition is Edition.TEXT


_cached: EditionCapabilities | None = None


def _capabilities_from_value(value: object) -> EditionCapabilities | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized == Edition.FULL.value:
        return EditionCapabilities.full()
    if normalized == Edition.TEXT.value:
        return EditionCapabilities.text()
    return None


def _load_packaged_edition() -> EditionCapabilities | None:
    """Read the build-time edition marker without importing optional engines."""
    override = _capabilities_from_value(os.environ.get("AI_TRANSLATER_EDITION"))
    if override is not None:
        return override

    if not getattr(sys, "frozen", False):
        return None
    resource_root = getattr(sys, "_MEIPASS", None)
    if not resource_root:
        return None
    marker = Path(resource_root) / "edition.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _capabilities_from_value(payload.get("edition"))


def detect_edition_capabilities() -> EditionCapabilities:
    """运行时检测当前应用版本。

    打包版从构建时写入的 ``edition.json`` 读取能力契约，绝不执行
    ``manga_translator`` 的顶层代码。开发环境没有构建标记时使用
    ``find_spec`` 轻量探测，仅供本地开发便利，仍不导入深度学习依赖。

    惰性检测，只执行一次并缓存到模块级 ``_cached``。
    测试可通过 ``reset_detection_for_tests`` 清除缓存后注入自定义值。
    """
    global _cached
    if _cached is not None:
        return _cached

    packaged = _load_packaged_edition()
    if packaged is not None:
        _cached = packaged
        return _cached

    try:
        available = importlib.util.find_spec("manga_translator") is not None
    except (ImportError, AttributeError, ValueError):
        available = False
    if available:
        _cached = EditionCapabilities.full()
    else:
        _cached = EditionCapabilities.text()
    return _cached


def reset_detection_for_tests() -> None:
    """重置运行时检测缓存（仅测试使用）。"""
    global _cached
    _cached = None
