#!/usr/bin/env python3
"""PERF-004 图片二进制资源存储与旧格式迁移测试。"""

import base64
import json
from unittest.mock import patch

from src.infrastructure.image_asset_store import (
    load_image_base64,
    load_image_bytes,
    migrate_legacy_images,
    save_image_binary,
)


def test_save_image_binary_keeps_v15_base64_fallback(tmp_path):
    """新图片映射同时保留 V1.5 Base64，避免落盘图片被透明加密改写。"""
    image_data = b"\x89PNG\r\n\x1a\nimage-content"
    info = save_image_binary(tmp_path, 0, "Images/cover.png", image_data, "image/png")

    assert info["base64_data"] == (
        "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")
    )
    assert (tmp_path / info["local_path"]).is_file()
    assert load_image_bytes(tmp_path, info) == image_data
    assert load_image_base64(tmp_path, info) == (
        "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")
    )


def test_save_image_binary_dedup_hit_does_not_rewrite_asset(tmp_path):
    """PERF-6：checksum 命中时不得再次写入或 fsync 图片资产。"""
    image_data = b"deduplicated-image"
    first = save_image_binary(tmp_path, 3, "Images/reused.png", image_data, "image/png")

    with patch("src.infrastructure.image_asset_store.write_bytes_atomic") as write_asset:
        second = save_image_binary(tmp_path, 3, "Images/reused.png", image_data, "image/png")

    assert second == first
    write_asset.assert_not_called()


def test_migrate_legacy_images_keeps_base64_after_assets_exist(tmp_path):
    """旧 Base64 映射迁移后仍保留可绕过透明加密的原始数据。"""
    image_data = b"legacy-image"
    legacy_payload = {
        "image_mappings": {
            "Images/legacy.png": {
                "original_path": "Images/legacy.png",
                "mime_type": "image/png",
                "base64_data": "data:image/png;base64,"
                + base64.b64encode(image_data).decode("ascii"),
                "translated_path": "images/legacy_translated.png",
            }
        }
    }
    images_file = tmp_path / "images.json"
    images_file.write_text(json.dumps(legacy_payload), encoding="utf-8")

    assert migrate_legacy_images(tmp_path) is True

    migrated = json.loads(images_file.read_text(encoding="utf-8"))
    info = migrated["image_mappings"]["Images/legacy.png"]
    assert migrated["schema_version"] == 2
    assert (
        info["base64_data"] == legacy_payload["image_mappings"]["Images/legacy.png"]["base64_data"]
    )
    assert info["translated_path"] == "images/legacy_translated.png"
    assert load_image_bytes(tmp_path, info) == image_data


def test_load_prefers_base64_when_local_asset_was_rewritten(tmp_path):
    """磁盘图片被透明加密改写后，读取仍返回导入时的原始字节。"""
    image_data = b"\xff\xd8\xff\xe0original-jpeg\xff\xd9"
    info = save_image_binary(tmp_path, 0, "Images/page.jpg", image_data, "image/jpeg")
    (tmp_path / info["local_path"]).write_bytes(b"encrypted-header" + b"\x00" * 4096)

    assert load_image_bytes(tmp_path, info) == image_data
    assert load_image_base64(tmp_path, info) == (
        "data:image/jpeg;base64," + base64.b64encode(image_data).decode("ascii")
    )


def test_load_image_bytes_retries_transient_permission_error(tmp_path, monkeypatch):
    """原子替换后短暂锁定不能使刚写入的图片被误判为丢失。"""
    image_data = b"retry-image"
    info = save_image_binary(tmp_path, 0, "Images/retry.png", image_data, "image/png")
    # 此用例覆盖本地 assets 的重试分支；常规路径会优先使用 Base64 回退。
    info["base64_data"] = ""
    asset_path = (tmp_path / info["local_path"]).resolve()
    attempts = 0

    def flaky_read(path):
        nonlocal attempts
        if path.resolve() == asset_path and attempts < 2:
            attempts += 1
            raise PermissionError("simulated transient lock")
        return image_data

    monkeypatch.setattr(type(asset_path), "read_bytes", flaky_read)
    with patch("src.infrastructure.image_asset_store.time.sleep"):
        assert load_image_bytes(tmp_path, info) == image_data
    assert attempts == 2
