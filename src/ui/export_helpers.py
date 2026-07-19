#!/usr/bin/env python3
"""P2-6：导出相关纯函数助手。

从 ``translation_controller.py`` 抽取的导出用例辅助逻辑，不依赖 Tk，
可独立单元测试。``TranslationController`` 作为 adapter 调用这些函数，
自身只负责 UI 交互（filedialog/messagebox）。

设计要点：
- ``load_image_translation_result`` / ``load_image_text_translations``
  原本内联在 ``export_epub_file`` 中，是纯文件 I/O，抽到独立模块后
  可在不用启动 Tk 的情况下测试新旧格式兼容性。
- 返回 ``None`` 表示文件缺失或解析失败，调用方按原逻辑跳过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_image_translation_result(mapping_dir: Path) -> dict[str, str] | None:
    """加载图片翻译结果映射。

    兼容两种格式：
    - 新格式：``{"result_map": {...}, "run_at": ..., "result_count": ...}``
    - 旧格式：``{original_path: new_filename}``

    Args:
        mapping_dir: EPUB 映射目录，包含 ``image_translation_result.json``。

    Returns:
        成功时返回 ``{original_epub_path: translated_relative_path}``；
        文件不存在或解析失败返回 ``None``。
    """
    result_file = mapping_dir / "image_translation_result.json"
    if not result_file.exists():
        return None
    try:
        with open(result_file, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # R2-BUG-018：兼容新旧格式
    if isinstance(raw, dict) and "result_map" in raw:
        result_map = raw["result_map"]
        if isinstance(result_map, dict):
            return {str(k): str(v) for k, v in result_map.items()}
        return None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return None


def load_image_text_translations(mapping_dir: Path) -> dict[str, Any] | None:
    """加载图片文字翻译结果。

    Args:
        mapping_dir: EPUB 映射目录，包含 ``image_text_translations.json``。

    Returns:
        成功时返回原始 JSON 字典；文件不存在或解析失败返回 ``None``。
    """
    text_trans_file = mapping_dir / "image_text_translations.json"
    if not text_trans_file.exists():
        return None
    try:
        with open(text_trans_file, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, dict):
        return raw
    return None


def build_default_epub_filename(source_path: Path | None) -> str:
    """根据源文件名生成默认 EPUB 导出文件名。

    Args:
        source_path: 源 EPUB 文件路径；``None`` 时返回空字符串。

    Returns:
        形如 ``"源文件名_译文.epub"`` 的文件名；无源路径时为空。
    """
    if source_path is None:
        return ""
    return f"{source_path.stem}_译文.epub"
