#!/usr/bin/env python3
"""
PERF-004：图片二进制资源存储

将 EPUB 图片从 Base64 JSON 内嵌改为二进制文件存储。

旧格式（images.json）：
    {"image_mappings": {"path": {"base64_data": "data:image/png;base64,..."}}}

新格式（images.json + assets/ 目录）：
    {"image_mappings": {"path": {"local_path": "assets/000001.jpg", ...}}}

兼容策略：
    - 加载旧 images.json 时自动迁移，逐张解码到 assets/ 目录。
    - 读取时优先从 local_path 加载二进制，回退 base64_data。
"""

import base64
import binascii
import hashlib
import json
from pathlib import Path
from typing import Dict

from ..utils.logger import get_logger
from .atomic_file import write_bytes_atomic, write_json_atomic

logger = get_logger(__name__)

_ASSETS_DIR = "assets"

# 文件名安全的扩展名提取
_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


class ImagePathValidationError(Exception):
    """P1-9：图片 local_path 信任边界校验失败。"""


def _validate_local_path(mapping_dir: Path, local_path: str) -> Path:
    """P1-9：验证 local_path 受控于 assets 根目录。

    拒绝绝对路径、包含 ``..`` 的相对路径，以及 resolve 后逃逸出
    ``mapping_dir/assets`` 的路径。错误消息不泄露 mapping_dir 完整路径。

    Args:
        mapping_dir: 映射目录（受控根）。
        local_path: images.json 中记录的相对路径（如 ``assets/000001.png``）。

    Returns:
        校验通过后的绝对 Path。

    Raises:
        ImagePathValidationError: 路径非法或逃逸。
    """
    if not local_path:
        raise ImagePathValidationError("local_path 为空")

    # 拒绝绝对路径（Windows 与 POSIX）
    candidate = Path(local_path)
    if candidate.is_absolute():
        raise ImagePathValidationError("local_path 不得为绝对路径")

    # 拒绝包含 .. 的路径组件（防目录穿越）
    parts = candidate.parts
    if any(part == ".." for part in parts):
        raise ImagePathValidationError("local_path 不得包含父目录引用")

    # resolve 后必须仍位于 mapping_dir 下
    assets_root = (mapping_dir / _ASSETS_DIR).resolve()
    resolved = (mapping_dir / candidate).resolve()
    try:
        resolved.relative_to(assets_root)
    except ValueError:
        raise ImagePathValidationError("local_path 逃逸出 assets 受控目录") from None

    # 拒绝符号链接逃逸（解析后路径不在 assets_root 下）
    if not resolved.is_relative_to(assets_root):
        raise ImagePathValidationError("local_path 解析后位于 assets 目录外")

    return resolved


def _safe_filename(index: int, mime_type: str, original_name: str) -> str:
    """生成安全的资源文件名。"""
    ext = _EXT_MAP.get(mime_type, "")
    if not ext:
        # 从原始路径提取扩展名
        ext = Path(original_name).suffix.lower() or ".bin"
    return f"{index:06d}{ext}"


def save_image_binary(
    mapping_dir: Path,
    index: int,
    image_name: str,
    image_data: bytes,
    mime_type: str,
) -> Dict:
    """PERF-004：将图片以二进制写入 assets/ 目录，返回不含 Base64 的元数据。

    Args:
        mapping_dir: 映射目录
        index: 图片序号（用于生成唯一文件名）
        image_name: EPUB 内原始路径
        image_data: 原始二进制数据
        mime_type: MIME 类型

    Returns:
        元数据字典（不含 base64_data）
    """
    assets_dir = mapping_dir / _ASSETS_DIR
    assets_dir.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(index, mime_type, image_name)
    file_path = assets_dir / filename

    # 同一项目内按 checksum 去重
    checksum = hashlib.md5(image_data).hexdigest()
    if file_path.exists():
        existing = file_path.read_bytes()
        if hashlib.md5(existing).hexdigest() == checksum:
            logger.debug("图片去重命中: %s -> %s", image_name, filename)
        else:
            # checksum 冲突，追加序号
            filename = f"{index:06d}_{checksum[:8]}{Path(filename).suffix}"
            file_path = assets_dir / filename

    write_bytes_atomic(file_path, image_data)

    return {
        "original_path": image_name,
        "local_path": f"{_ASSETS_DIR}/{filename}",
        "mime_type": mime_type,
        "file_size": len(image_data),
        "checksum": checksum,
    }


def load_image_base64(mapping_dir: Path, image_info: Dict) -> str:
    """PERF-004：按需加载图片并返回 Base64 data URI。

    优先从 local_path 读取二进制文件，回退到 base64_data（旧格式）。

    P1-9：local_path 必须通过信任边界校验，拒绝绝对路径、``..`` 和
    符号链接逃逸。校验失败时记录警告并回退到 base64_data。

    Args:
        mapping_dir: 映射目录
        image_info: 图片元数据

    Returns:
        Base64 data URI 字符串（如 "data:image/png;base64,..."），
        如果加载失败返回空字符串。
    """
    # 优先从二进制文件加载
    local_path = image_info.get("local_path", "")
    if local_path:
        try:
            file_path = _validate_local_path(mapping_dir, local_path)
        except ImagePathValidationError as e:
            logger.warning("local_path 校验失败，拒绝读取: %s", e)
            return ""
        if file_path.exists():
            try:
                data = file_path.read_bytes()
                mime = image_info.get("mime_type", "image/png")
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:{mime};base64,{b64}"
            except OSError as e:
                logger.warning("读取图片二进制失败: %s", e)

    # 回退到旧格式 base64_data
    b64_data = image_info.get("base64_data", "")
    return b64_data


def load_image_bytes(mapping_dir: Path, image_info: Dict) -> bytes | None:
    """PERF-004：按需加载图片二进制数据。

    优先从 local_path 读取，回退解码 base64_data。

    P1-9：local_path 必须通过信任边界校验，拒绝绝对路径、``..`` 和
    符号链接逃逸。校验失败时记录警告并回退到 base64_data。

    Args:
        mapping_dir: 映射目录
        image_info: 图片元数据

    Returns:
        原始二进制数据，加载失败返回 None。
    """
    local_path = image_info.get("local_path", "")
    if local_path:
        try:
            file_path = _validate_local_path(mapping_dir, local_path)
        except ImagePathValidationError as e:
            logger.warning("local_path 校验失败，拒绝读取: %s", e)
            file_path = None
        if file_path is not None and file_path.exists():
            try:
                return file_path.read_bytes()
            except OSError as e:
                logger.warning("读取图片二进制失败: %s", e)

    # 回退解码 base64_data
    b64_data = image_info.get("base64_data", "")
    if b64_data:
        if "," in b64_data:
            b64_data = b64_data.split(",", 1)[1]
        try:
            return base64.b64decode(b64_data)
        except (binascii.Error, ValueError) as e:
            logger.warning("Base64 解码失败: %s", e)

    return None


def migrate_legacy_images(mapping_dir: Path) -> bool:
    """PERF-004：迁移旧格式 images.json，将 Base64 解码为二进制文件。

    检测 images.json 中是否含有 base64_data 字段，如果有则逐张解码
    到 assets/ 目录，生成新元数据，原子写入新 schema。

    Args:
        mapping_dir: 映射目录

    Returns:
        True 表示执行了迁移，False 表示无需迁移。
    """
    images_file = mapping_dir / "images.json"
    if not images_file.exists():
        return False

    try:
        images_data = json.loads(images_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取 images.json 失败，跳过迁移: %s", e)
        return False

    image_mappings: Dict[str, Dict] = images_data.get("image_mappings", {})
    if not image_mappings:
        return False

    migrated = False
    for idx, (image_path, info) in enumerate(image_mappings.items()):
        # 已有 local_path 且文件存在，跳过
        local_path = info.get("local_path", "")
        if local_path:
            # P1-9：迁移时也要校验 local_path 信任边界
            try:
                validated = _validate_local_path(mapping_dir, local_path)
            except ImagePathValidationError as e:
                logger.warning("迁移时 local_path 校验失败，跳过: %s", e)
                continue
            if validated.exists():
                continue

        # 没有 base64_data，无法迁移
        b64_data = info.get("base64_data", "")
        if not b64_data:
            continue

        # 解码 Base64
        b64_str = b64_data.split(",", 1)[1] if "," in b64_data else b64_data
        try:
            image_data = base64.b64decode(b64_str)
        except (binascii.Error, ValueError) as e:
            logger.warning("迁移图片解码失败: %s: %s", image_path, e)
            continue

        mime_type = info.get("mime_type", "image/png")
        new_info = save_image_binary(mapping_dir, idx, image_path, image_data, mime_type)
        # 保留原有 translated_path 等字段
        for k in ("translated_path",):
            if k in info:
                new_info[k] = info[k]
        image_mappings[image_path] = new_info
        migrated = True

    if migrated:
        write_json_atomic(images_file, {"image_mappings": image_mappings})
        logger.info("PERF-004: 迁移完成，%d 张图片已转为二进制存储", len(image_mappings))

    return migrated
