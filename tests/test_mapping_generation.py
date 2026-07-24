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


# ── Batch B: generation GC tests ──────────────────────────────────────────


def _count_generation_dirs(mapping_dir: Path) -> int:
    """Return number of subdirectories inside .mapping_generations/."""
    gen_root = mapping_dir / repository.MAPPING_GENERATIONS_DIRNAME
    if not gen_root.is_dir():
        return 0
    return sum(1 for p in gen_root.iterdir() if p.is_dir())


def _count_unreachable_generation_dirs(mapping_dir: Path) -> int:
    """Return number of generation dirs NOT referenced by the current manifest."""
    gen_root = mapping_dir / repository.MAPPING_GENERATIONS_DIRNAME
    if not gen_root.is_dir():
        return 0
    manifest = repository._load_manifest(mapping_dir)
    if manifest is None:
        return 0
    reachable = repository._reachable_generation_names(manifest)
    return sum(
        1 for p in gen_root.iterdir() if p.is_dir() and p.name not in reachable
    )


def test_gc_keeps_only_active_generation_after_repeated_bundle_publishes(tmp_path):
    """连续整包发布后，每次都只保留当前 bundle 对应的一个 generation 目录。

    publish_mapping_bundle 将全部三个成员写入同一 generation 目录，
    因此 GC 后只有 1 个目录存活。
    """
    for i in range(6):
        repository.publish_mapping_bundle(
            tmp_path,
            _content_payload(f"v{i}"),
            _images_payload(f"img-{i}"),
            _format_payload(f"title-{i}"),
        )
    # 整包发布：所有成员共享同一 generation，GC 后只剩 1 个目录
    assert _count_generation_dirs(tmp_path) == 1
    # 不可达数量必须为 0（GC 验收条件）
    assert _count_unreachable_generation_dirs(tmp_path) == 0
    originals, translations = repository.load_content_mapping(str(tmp_path))
    assert translations == ["v5"]


def test_gc_keeps_only_active_generation_after_repeated_file_updates(tmp_path):
    """单文件更新时，GC 确保不可达 generation 数为 0。

    每次 save_translations 只更新 content 成员，manifest 中的 images/format
    仍指向原始 bundle generation。因此最多同时存在 2 个可达目录
    （最新 content gen + 原始 bundle gen）。GC 应回收所有不可达的旧目录。
    """
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload(),
        _images_payload(),
        _format_payload(),
    )
    for i in range(8):
        repository.save_translations(str(tmp_path), [f"trans-{i}"])

    # 核心验收：不可达 generation 数必须为 0
    assert _count_unreachable_generation_dirs(tmp_path) == 0
    # 单文件更新：content-only gen + 原始 bundle gen（含 images/format）= 最多 2
    assert _count_generation_dirs(tmp_path) <= 2
    _, translations = repository.load_content_mapping(str(tmp_path))
    assert translations == ["trans-7"]


def test_gc_does_not_remove_currently_referenced_generation(tmp_path):
    """GC 不会删除当前 manifest 正在引用的 generation 目录。"""
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload("keep"),
        _images_payload(),
        _format_payload(),
    )
    # 整包发布：所有成员在同一 generation，只有 1 个目录
    assert _count_generation_dirs(tmp_path) == 1
    assert _count_unreachable_generation_dirs(tmp_path) == 0
    path = repository.resolve_mapping_file(tmp_path, "content_mapping.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["content_mappings"]["line_000001"]["translated_text"] == "keep"


def test_gc_count_stable_under_partial_member_updates(tmp_path):
    """轮流更新各成员时，GC 确保不可达数为 0，总目录数 <= 成员数（3）。

    当三个成员分别在各自 generation 目录中时，最多 3 个可达目录。
    GC 应在每次发布后立即清除不可达的旧目录，使总数保持稳定上界。
    """
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload(),
        _images_payload("img-0"),
        _format_payload("t-0"),
    )
    for i in range(1, 5):
        repository.save_images_mapping(tmp_path, {"cover.png": {"local_path": f"img-{i}"}})
        repository.save_format_info(tmp_path, {"metadata": {"title": f"t-{i}"}, "spine_order": []})
        # 每次更新后验证无不可达目录
        assert _count_unreachable_generation_dirs(tmp_path) == 0

    # 总目录数 <= 3（每个成员最多独占一个 generation 目录）
    assert _count_generation_dirs(tmp_path) <= 3


def test_legacy_compat_copies_disabled_skips_top_level_json(tmp_path, monkeypatch):
    """_LEGACY_COMPAT_COPIES_ENABLED=False 时不再写顶层兼容副本。"""
    monkeypatch.setattr(repository, "_LEGACY_COMPAT_COPIES_ENABLED", False)
    repository.publish_mapping_bundle(
        tmp_path,
        _content_payload("x"),
        _images_payload(),
        _format_payload(),
    )
    # 顶层 JSON 不应存在
    assert not (tmp_path / "content_mapping.json").exists()
    assert not (tmp_path / "images.json").exists()
    # generation 内文件应正常存在
    path = repository.resolve_mapping_file(tmp_path, "content_mapping.json")
    assert path.exists()


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
