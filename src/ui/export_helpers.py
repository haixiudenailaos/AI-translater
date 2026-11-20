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
import os
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


def build_default_bilingual_epub_filename(source_path: Path | None) -> str:
    """生成双语 EPUB 的默认文件名，不改动既有默认文件名函数。"""
    if source_path is None:
        return ""
    return f"{source_path.stem}_双语对照.epub"


def _normalized_path(path: Path) -> str:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate.absolute()
    return os.path.normcase(str(resolved))


def _same_file(left: Path, right: Path) -> bool:
    if _normalized_path(left) == _normalized_path(right):
        return True
    try:
        return left.exists() and right.exists() and Path(left).samefile(Path(right))
    except OSError:
        return False


def _is_within(child: Path, parent: Path) -> bool:
    child_text = _normalized_path(child)
    parent_text = _normalized_path(parent).rstrip("\\/")
    return child_text == parent_text or child_text.startswith(parent_text + os.sep)


def is_forbidden_bilingual_output(
    output_path: Path,
    *,
    source_path: Path | None,
    target_path: Path | None,
    mapping_dir: Path | None,
) -> str | None:
    """Return a user-facing rejection message when the output path is protected.

    双语导出产物不得覆盖原 EPUB、当前会话译文文件或项目映射目录及其子目录。
    返回 ``None`` 表示可以继续。大小写、硬链接与别名按平台规则识别。
    """
    if source_path is not None and _same_file(output_path, Path(source_path)):
        return "不能将双语 EPUB 覆盖原 EPUB 文件，请选择其他输出位置。"
    if target_path is not None and _same_file(output_path, Path(target_path)):
        return "不能将双语 EPUB 覆盖当前会话的译文文件，请选择其他输出位置。"
    if mapping_dir is not None:
        mapping_root = Path(mapping_dir)
        if _is_within(output_path, mapping_root) or _is_within(mapping_root, output_path):
            return "不能将双语 EPUB 写入项目映射目录，请选择其他输出位置。"
    return None
