#!/usr/bin/env python3
"""PERF-004 / Batch-C：图片 binary-first 存储与旧格式迁移测试。"""

import base64
import hashlib
import json
from unittest.mock import patch

from src.infrastructure.image_asset_store import (
    IMAGE_MAPPING_SCHEMA_VERSION,
    load_image_base64,
    load_image_bytes,
    migrate_legacy_images,
    save_image_binary,
)


def test_save_image_binary_no_base64_on_healthy_fs(tmp_path):
    """Batch-C：健康文件系统下不写入 base64_data（节省 ~1.33x 磁盘占用）。"""
    image_data = b"\x89PNG\r\n\x1a\nimage-content"
    info = save_image_binary(tmp_path, 0, "Images/cover.png", image_data, "image/png")

    assert "base64_data" not in info, "正常文件系统不应写入 base64_data"
    assert (tmp_path / info["local_path"]).is_file()
    assert info["checksum"] == hashlib.md5(image_data).hexdigest()
    # 读取应返回原始内容
    assert load_image_bytes(tmp_path, info) == image_data
    expected_uri = "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")
    assert load_image_base64(tmp_path, info) == expected_uri


def test_save_image_binary_adds_fallback_on_readback_failure(tmp_path):
    """Batch-C：读回 checksum 失败时写入 base64_data fallback。"""
    image_data = b"\x89PNG\r\n\x1a\ncorrupt-test"

    # 让读回返回不同内容，触发 checksum 不一致
    original_read = bytes.decode  # unused, just using patch target
    call_count = [0]

    def flaky_read_bytes(path):
        call_count[0] += 1
        if call_count[0] == 1:
            return b"corrupted-bytes"  # 读回内容不同
        return image_data

    with patch(
        "src.infrastructure.image_asset_store._read_asset_bytes", side_effect=flaky_read_bytes
    ):
        info = save_image_binary(tmp_path, 0, "Images/p.png", image_data, "image/png")

    assert "base64_data" in info, "读回失败时应写入 base64_data fallback"
    assert load_image_bytes(tmp_path, info) == image_data


def test_save_image_binary_dedup_hit_does_not_rewrite_asset(tmp_path):
    """PERF-6：checksum 命中时不得再次写入或 fsync 图片资产。"""
    image_data = b"deduplicated-image"
    first = save_image_binary(tmp_path, 3, "Images/reused.png", image_data, "image/png")

    with patch("src.infrastructure.image_asset_store.write_bytes_atomic") as write_asset:
        second = save_image_binary(tmp_path, 3, "Images/reused.png", image_data, "image/png")

    assert second == first
    write_asset.assert_not_called()


def test_migrate_legacy_images_creates_binary_assets(tmp_path):
    """Batch-C：旧 Base64 映射迁移后建立二进制资产，并更新 schema_version。"""
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
    # 正常文件系统：迁移后不应保留 base64_data；临时锁的 fallback 也兼容。
    assert migrated["schema_version"] == IMAGE_MAPPING_SCHEMA_VERSION
    assert info["translated_path"] == "images/legacy_translated.png"
    assert (tmp_path / info["local_path"]).is_file()
    assert load_image_bytes(tmp_path, info) == image_data


def test_load_falls_back_to_base64_on_checksum_mismatch(tmp_path):
    """binary-first 读取：checksum 不一致时回退到 base64_data（旧格式兼容）。"""
    image_data = b"\xff\xd8\xff\xe0original-jpeg\xff\xd9"
    info = save_image_binary(tmp_path, 0, "Images/page.jpg", image_data, "image/jpeg")
    # 强制写入 base64_data（模拟旧格式或保存时读回失败的条目）
    info["base64_data"] = "data:image/jpeg;base64," + base64.b64encode(image_data).decode("ascii")
    # 改写磁盘文件，触发 checksum 不一致
    (tmp_path / info["local_path"]).write_bytes(b"encrypted-header" + b"\x00" * 4096)

    assert load_image_bytes(tmp_path, info) == image_data
    assert load_image_base64(tmp_path, info) == (
        "data:image/jpeg;base64," + base64.b64encode(image_data).decode("ascii")
    )


def test_load_returns_none_when_binary_corrupt_and_no_base64(tmp_path):
    """binary-first 读取：checksum 不一致且无 base64_data 时返回 None / 空串。"""
    image_data = b"\x89PNG\r\n\x1a\ntest"
    info = save_image_binary(tmp_path, 0, "Images/test.png", image_data, "image/png")
    # 确保没有 base64_data（正常保存路径）
    assert "base64_data" not in info
    # 改写磁盘文件，触发 checksum 不一致
    (tmp_path / info["local_path"]).write_bytes(b"garbage")

    assert load_image_bytes(tmp_path, info) is None
    assert load_image_base64(tmp_path, info) == ""


def test_load_image_bytes_retries_transient_permission_error(tmp_path, monkeypatch):
    """原子替换后短暂锁定不能使刚写入的图片被误判为丢失。"""
    image_data = b"retry-image"
    info = save_image_binary(tmp_path, 0, "Images/retry.png", image_data, "image/png")
    # 本用例直接测试二进制路径（不加 base64_data，binary-first 已是默认路径）
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
