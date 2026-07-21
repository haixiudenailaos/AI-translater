"""P1-8: EPUB mapping generation manifest regression tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from src.infrastructure import mapping_repository as repository
from src.infrastructure.image_asset_store import load_image_bytes, migrate_legacy_images


def _content_payload(translation: str = "") -> dict:
    return {
        "project_info": {"project_id": "test"},
        "content_mappings": {
            "line_000001": {
                "line_number": 1,
                "original_text": "source",
                "translated_text": translation,
            }
        },
    }


def _images_payload(value: str = "image-a") -> dict:
    return {"image_mappings": {"cover.png": {"local_path": value}}}


def _format_payload(value: str = "title-a") -> dict:
    return {"metadata": {"title": value}, "spine_order": []}


def test_failed_generation_write_keeps_previous_published_bundle(tmp_path, monkeypatch):
    """成员写入失败时 manifest 继续指向上一完整三文件 generation。"""
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload("old"),
        _images_payload("old-image"),
        _format_payload("old-title"),
    )
    old_manifest = (tmp_path / repository.MAPPING_MANIFEST_FILENAME).read_bytes()
    old_content = repository.resolve_mapping_file(tmp_path, "content_mapping.json")

    real_write = repository.write_json_atomic

    def fail_new_images(path: Path, payload: dict) -> None:
        if (
            Path(path).name == "images.json"
            and repository.MAPPING_GENERATIONS_DIRNAME in Path(path).parts
        ):
            raise OSError("injected image generation failure")
        real_write(path, payload)

    monkeypatch.setattr(repository, "write_json_atomic", fail_new_images)

    with pytest.raises(OSError, match="injected image generation failure"):
        repository.publish_mapping_bundle(
            tmp_path,
            _content_payload("new"),
            _images_payload("new-image"),
            _format_payload("new-title"),
        )

    assert (tmp_path / repository.MAPPING_MANIFEST_FILENAME).read_bytes() == old_manifest
    assert repository.resolve_mapping_file(tmp_path, "content_mapping.json") == old_content
    assert (
        json.loads(old_content.read_text(encoding="utf-8"))["content_mappings"]["line_000001"][
            "translated_text"
        ]
        == "old"
    )
    assert (
        json.loads(
            repository.resolve_mapping_file(tmp_path, "images.json").read_text(encoding="utf-8")
        )["image_mappings"]["cover.png"]["local_path"]
        == "old-image"
    )


def test_translation_update_publishes_only_new_content_member(tmp_path):
    """译文更新切换 content generation，不复制或混换图片/格式成员。"""
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload(),
        _images_payload(),
        _format_payload(),
    )
    old_content = repository.resolve_mapping_file(tmp_path, "content_mapping.json")
    old_images = repository.resolve_mapping_file(tmp_path, "images.json")
    old_format = repository.resolve_mapping_file(tmp_path, "format_info.json")

    repository.save_translations(str(tmp_path), ["translated"])

    new_content = repository.resolve_mapping_file(tmp_path, "content_mapping.json")
    assert new_content != old_content
    assert repository.resolve_mapping_file(tmp_path, "images.json") == old_images
    assert repository.resolve_mapping_file(tmp_path, "format_info.json") == old_format
    originals, translations = repository.load_content_mapping(str(tmp_path))
    assert originals == ["source"]
    assert translations == ["translated"]


def test_legacy_image_migration_publishes_a_new_images_generation(tmp_path):
    """Image migration must update the published member, not only its legacy copy."""
    image_data = b"legacy-image"
    images_payload = {
        "image_mappings": {
            "Images/legacy.png": {
                "original_path": "Images/legacy.png",
                "mime_type": "image/png",
                "base64_data": "data:image/png;base64,"
                + base64.b64encode(image_data).decode("ascii"),
            }
        }
    }
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload(),
        images_payload,
        _format_payload(),
    )
    old_images = repository.resolve_mapping_file(tmp_path, "images.json")
    old_content = repository.resolve_mapping_file(tmp_path, "content_mapping.json")
    old_format = repository.resolve_mapping_file(tmp_path, "format_info.json")

    # New readers must not depend on this compatibility copy.
    (tmp_path / "images.json").unlink()

    assert migrate_legacy_images(tmp_path) is True

    new_images = repository.resolve_mapping_file(tmp_path, "images.json")
    migrated = json.loads(new_images.read_text(encoding="utf-8"))
    image_info = migrated["image_mappings"]["Images/legacy.png"]
    assert new_images != old_images
    assert migrated["schema_version"] == 2
    assert image_info["local_path"].startswith("assets/")
    assert load_image_bytes(tmp_path, image_info) == image_data
    assert repository.resolve_mapping_file(tmp_path, "content_mapping.json") == old_content
    assert repository.resolve_mapping_file(tmp_path, "format_info.json") == old_format
