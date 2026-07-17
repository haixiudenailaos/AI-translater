#!/usr/bin/env python3
"""PERF-004 图片二进制资源存储与旧格式迁移测试。"""

import base64
import json

from src.infrastructure.image_asset_store import (
    load_image_base64,
    load_image_bytes,
    migrate_legacy_images,
    save_image_binary,
)


def test_save_image_binary_keeps_base64_out_of_metadata(tmp_path):
    """新图片映射只保存元数据，按需读取时仍可得到原始内容。"""
    image_data = b"\x89PNG\r\n\x1a\nimage-content"
    info = save_image_binary(tmp_path, 0, "Images/cover.png", image_data, "image/png")

    assert "base64_data" not in info
    assert (tmp_path / info["local_path"]).read_bytes() == image_data
    assert load_image_bytes(tmp_path, info) == image_data
    assert load_image_base64(tmp_path, info) == (
        "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")
    )


def test_migrate_legacy_images_rewrites_metadata_after_assets_exist(tmp_path):
    """旧 Base64 映射迁移后保留可读数据且 JSON 不再承载图片内容。"""
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
    assert "base64_data" not in info
    assert info["translated_path"] == "images/legacy_translated.png"
    assert load_image_bytes(tmp_path, info) == image_data
