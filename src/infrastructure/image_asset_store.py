#!/usr/bin/env python3
"""
PERF-004 / Batch-C：图片二进制资源存储（binary-first）

写入路径（save_image_binary）：
  1. 把原始二进制写入 assets/ 并立即读回校验 checksum。
  2. 读回 OK → 不保留 base64_data，节省约 1.33x 磁盘/内存。
  3. 读回失败（校验不一致或 IO 错误）→ 写入受控 base64_data fallback，
     并记录 WARNING；fallback 不触发图片文件过滤器。

读取路径（load_image_bytes / load_image_base64）：
  1. 优先从 local_path 读取二进制文件并验证 checksum。
  2. 仅在 checksum 不一致、文件缺失或 IO 错误时，回退到旧式 base64_data
     （V1.5/V1.6 兼容层，只读迁移）。

旧格式（images.json）：
    {"image_mappings": {"path": {"base64_data": "data:image/png;base64,..."}}}

新格式（images.json + assets/ 目录）：
    {"image_mappings": {"path": {"local_path": "assets/000001.jpg",
                                 "checksum": "<md5>", ...}}}
"""

import base64
import binascii
import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Dict

from ..utils.logger import get_logger
from .atomic_file import write_bytes_atomic
from .mapping_repository import publish_mapping_file_update, resolve_mapping_file

logger = get_logger(__name__)

_ASSETS_DIR = "assets"
# Windows Defender/indexing can retain a just-replaced file longer than the
# previous ~2.5 s retry window. This code runs on import/export workers, not
# Tk's UI thread; a bounded ~10 s window avoids mistaking a transient lock for
# a failed binary asset while still preserving a deterministic upper bound.
_READ_RETRY_COUNT = 11
_READ_RETRY_BASE_DELAY = 0.01
IMAGE_MAPPING_SCHEMA_VERSION = 3  # Batch-C: binary-first, no default base64_data
_READ_THROUGH_CACHE_TTL_SECONDS = 30.0
_READ_THROUGH_CACHE_MAX_BYTES = 8 * 1024 * 1024
_read_through_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_read_through_cache_bytes = 0
_read_through_cache_lock = Lock()

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


def _read_asset_bytes(path: Path) -> bytes:
    """Read an asset with a bounded retry for transient Windows file locks."""
    for attempt in range(_READ_RETRY_COUNT):
        try:
            return path.read_bytes()
        except PermissionError:
            if attempt + 1 == _READ_RETRY_COUNT:
                raise
            time.sleep(_READ_RETRY_BASE_DELAY * (2**attempt))
    raise RuntimeError("unreachable")


def _remember_recent_asset(path: Path, data: bytes) -> None:
    """Keep a short, byte-bounded write-through cache for transient file locks."""
    if len(data) > _READ_THROUGH_CACHE_MAX_BYTES:
        return

    global _read_through_cache_bytes
    cache_key = str(path)
    now = time.monotonic()
    with _read_through_cache_lock:
        while _read_through_cache:
            oldest_key, (written_at, oldest_data) = next(iter(_read_through_cache.items()))
            if now - written_at <= _READ_THROUGH_CACHE_TTL_SECONDS:
                break
            _read_through_cache.pop(oldest_key)
            _read_through_cache_bytes -= len(oldest_data)

        existing = _read_through_cache.pop(cache_key, None)
        if existing is not None:
            _read_through_cache_bytes -= len(existing[1])
        while (
            _read_through_cache
            and _read_through_cache_bytes + len(data) > _READ_THROUGH_CACHE_MAX_BYTES
        ):
            _unused_key, (_unused_time, evicted) = _read_through_cache.popitem(last=False)
            _read_through_cache_bytes -= len(evicted)

        _read_through_cache[cache_key] = (now, data)
        _read_through_cache_bytes += len(data)


def _recent_asset_if_valid(path: Path, expected_checksum: str) -> bytes | None:
    """Return a fresh write-through entry only when it still matches the checksum."""
    global _read_through_cache_bytes
    cache_key = str(path)
    now = time.monotonic()
    with _read_through_cache_lock:
        cached = _read_through_cache.get(cache_key)
        if cached is None:
            return None
        written_at, data = cached
        if now - written_at > _READ_THROUGH_CACHE_TTL_SECONDS:
            _read_through_cache.pop(cache_key)
            _read_through_cache_bytes -= len(data)
            return None
        _read_through_cache.move_to_end(cache_key)

    if expected_checksum and hashlib.md5(data).hexdigest() != expected_checksum:
        return None
    return data


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
    *,
    preserve_fallback_on_readback_permission: bool = False,
) -> Dict:
    """Batch-C：binary-first 保存图片，仅在读回校验失败时添加 base64_data fallback。

    写入流程：
    1. 写入 assets/<name>_<checksum8>.ext（跳过已存在的 dedup 命中）。
    2. 立即读回并校验 MD5 checksum。
    3. 读回 OK → 返回不含 base64_data 的条目（节省 ~1.33x 磁盘/内存）。
    4. 读回失败（IO 错误 / checksum 不一致）→ 写入 base64_data 并记录 WARNING。

    Args:
        mapping_dir: 映射目录
        index: 图片序号（用于生成唯一文件名）
        image_name: EPUB 内原始路径
        image_data: 原始二进制数据
        mime_type: MIME 类型

    Returns:
        包含本地资产元数据的字典；仅在读回失败时含有 base64_data 字段。
    """
    assets_dir = mapping_dir / _ASSETS_DIR
    assets_dir.mkdir(parents=True, exist_ok=True)

    checksum = hashlib.md5(image_data).hexdigest()
    base_filename = _safe_filename(index, mime_type, image_name)
    filename = f"{Path(base_filename).stem}_{checksum[:8]}{Path(base_filename).suffix}"
    file_path = assets_dir / filename

    # 文件名已包含原图 checksum，存在即代表同一份缓存
    if file_path.exists():
        logger.debug("图片去重命中: %s -> %s", image_name, filename)
    else:
        try:
            write_bytes_atomic(file_path, image_data)
        except OSError as e:
            logger.warning("图片缓存写入失败，启用 Base64 fallback: %s", e)
            return {
                "original_path": image_name,
                "local_path": f"{_ASSETS_DIR}/{filename}",
                "mime_type": mime_type,
                "file_size": len(image_data),
                "checksum": checksum,
                "base64_data": (
                    f"data:{mime_type};base64," + base64.b64encode(image_data).decode("ascii")
                ),
            }

    _remember_recent_asset(file_path, image_data)

    entry: Dict = {
        "original_path": image_name,
        "local_path": f"{_ASSETS_DIR}/{filename}",
        "mime_type": mime_type,
        "file_size": len(image_data),
        "checksum": checksum,
    }

    # 读回校验：确保落盘字节与原始数据一致。Windows 上原子替换后的
    # 短暂共享锁不表示数据损坏；已经完成有界重试仍被拒绝时，保留刚刚
    # 成功发布的二进制条目，让后续读取路径继续重试，而不是把瞬态锁
    # 固化为一份 Base64 JSON 副本。
    readback_ok = False
    try:
        written = _read_asset_bytes(file_path)
        readback_ok = hashlib.md5(written).hexdigest() == checksum
    except PermissionError as e:
        logger.warning(
            "图片读回在重试后仍被临时锁定，延后校验并保持 binary-first: %s (%s)",
            filename,
            e,
        )
        if preserve_fallback_on_readback_permission:
            entry["base64_data"] = f"data:{mime_type};base64," + base64.b64encode(
                image_data
            ).decode("ascii")
        return entry
    except OSError as e:
        logger.warning("图片读回失败，启用 Base64 fallback: %s (%s)", filename, e)

    if not readback_ok:
        logger.warning("图片读回 checksum 不一致，启用 Base64 fallback: %s", filename)
        entry["base64_data"] = f"data:{mime_type};base64," + base64.b64encode(image_data).decode(
            "ascii"
        )

    return entry


def _decode_embedded_base64(image_info: Dict) -> bytes | None:
    """Decode the legacy Base64 fallback from image metadata (read-only compat layer)."""
    b64_data = image_info.get("base64_data", "")
    if not b64_data:
        return None
    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]
    try:
        return base64.b64decode(b64_data)
    except (binascii.Error, ValueError) as e:
        logger.warning("Base64 解码失败: %s", e)
        return None


def _load_validated_binary(mapping_dir: Path, image_info: Dict) -> bytes | None:
    """尝试从 local_path 读取并 checksum 校验；失败返回 None。"""
    local_path = image_info.get("local_path", "")
    if not local_path:
        return None
    try:
        file_path = _validate_local_path(mapping_dir, local_path)
    except ImagePathValidationError as e:
        logger.warning("local_path 校验失败，拒绝读取: %s", e)
        return None
    if not file_path.exists():
        return None
    try:
        data = _read_asset_bytes(file_path)
    except OSError as e:
        cached = _recent_asset_if_valid(file_path, image_info.get("checksum", ""))
        if cached is not None:
            logger.debug("图片磁盘读取暂时失败，使用短期写穿透缓存: %s", local_path)
            return cached
        logger.warning("读取图片二进制失败: %s", e)
        return None
    expected = image_info.get("checksum", "")
    if expected and hashlib.md5(data).hexdigest() != expected:
        logger.warning("图片 checksum 不一致，拒绝使用: %s", local_path)
        return None
    return data


def load_image_base64(mapping_dir: Path, image_info: Dict) -> str:
    """Batch-C：按需加载图片并返回 Base64 data URI（binary-first）。

    优先从 local_path 读取二进制文件并验证 checksum；仅在 checksum 不一致、
    文件缺失或 IO 错误时，回退到旧式 base64_data（V1.5/V1.6 兼容层，只读）。

    P1-9：local_path 必须通过信任边界校验，拒绝绝对路径、``..`` 和
    符号链接逃逸。

    Args:
        mapping_dir: 映射目录
        image_info: 图片元数据

    Returns:
        Base64 data URI 字符串（如 "data:image/png;base64,..."），
        如果加载失败返回空字符串。
    """
    # Binary-first：先尝试从磁盘读取并校验
    data = _load_validated_binary(mapping_dir, image_info)
    if data is not None:
        mime = image_info.get("mime_type", "image/png")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    # Legacy fallback：旧格式或 base64_data fallback 条目
    embedded = _decode_embedded_base64(image_info)
    if embedded is not None:
        mime = image_info.get("mime_type", "image/png")
        return f"data:{mime};base64," + base64.b64encode(embedded).decode("ascii")

    return ""


def load_image_bytes(mapping_dir: Path, image_info: Dict) -> bytes | None:
    """Batch-C：按需加载图片二进制数据（binary-first）。

    优先从 local_path 读取并验证 checksum；仅在校验失败、文件缺失或
    IO 错误时，回退到旧式 base64_data（V1.5/V1.6 兼容层，只读）。

    P1-9：local_path 必须通过信任边界校验。

    Args:
        mapping_dir: 映射目录
        image_info: 图片元数据

    Returns:
        原始二进制数据，加载失败返回 None。
    """
    # Binary-first
    data = _load_validated_binary(mapping_dir, image_info)
    if data is not None:
        return data

    # Legacy fallback
    return _decode_embedded_base64(image_info)


def migrate_legacy_images(mapping_dir: Path) -> bool:
    """Batch-C：将旧 images.json（V1.5/V1.6 Base64-first）迁移到 binary-first 格式。

    检测 images.json 中是否含有 base64_data 字段，如果有则逐张解码
    到 assets/ 目录（binary-first）。迁移后不保留 base64_data，
    除非 save_image_binary 的读回校验失败（自动写入 fallback）。

    Args:
        mapping_dir: 映射目录

    Returns:
        True 表示执行了迁移，False 表示无需迁移。
    """
    images_file = resolve_mapping_file(mapping_dir, "images.json")
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

    changed = False
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
        new_info = save_image_binary(
            mapping_dir,
            idx,
            image_path,
            image_data,
            mime_type,
            preserve_fallback_on_readback_permission=True,
        )
        # 保留原有 translated_path 等字段
        for k in ("translated_path",):
            if k in info:
                new_info[k] = info[k]
        image_mappings[image_path] = new_info
        changed = True

    has_local_paths = all(bool(info.get("local_path")) for info in image_mappings.values())
    if has_local_paths and images_data.get("schema_version") != IMAGE_MAPPING_SCHEMA_VERSION:
        images_data["schema_version"] = IMAGE_MAPPING_SCHEMA_VERSION
        changed = True

    if changed:
        images_data["image_mappings"] = image_mappings
        publish_mapping_file_update(mapping_dir, "images.json", images_data)
        logger.info("图片映射兼容迁移完成，共 %d 张图片", len(image_mappings))

    return changed
