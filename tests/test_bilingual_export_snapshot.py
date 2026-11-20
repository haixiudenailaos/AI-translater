#!/usr/bin/env python3
"""双语导出的稳定映射读取、快照对齐与行号校验测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domain.epub_export import EpubDocumentSnapshot, resolve_language_tag
from src.domain.errors import BilingualEpubExportError
from src.infrastructure import mapping_repository
from src.infrastructure.exporter import (
    _overlay_snapshot_targets,
    _validate_bilingual_snapshot,
)
from src.infrastructure.mapping_repository import (
    MappingSnapshotBusyError,
    MappingSnapshotError,
    read_content_mapping_snapshot,
)


def _content_payload(
    lines: list[str],
    *,
    originals: list[str] | None = None,
    translated: list[str] | None = None,
) -> dict:
    originals = originals if originals is not None else lines
    translated = translated if translated is not None else [""] * len(originals)
    return {
        "project_info": {"original_file": "book.epub"},
        "content_mappings": {
            f"line_{index + 1:06d}": {
                "line_number": index + 1,
                "original_text": originals[index],
                "translated_text": translated[index],
                "chapter_id": "Text/ch1.xhtml",
                "block_index": index,
                "source_checksum": "x",
            }
            for index in range(len(originals))
        },
    }


def _snapshot(
    mapping_dir: Path,
    source_lines: tuple[str, ...],
    target_lines: tuple[str, ...] | None = None,
) -> EpubDocumentSnapshot:
    return EpubDocumentSnapshot(
        session_id="session",
        document_version=1,
        source_path=mapping_dir / "book.epub",
        mapping_dir=mapping_dir,
        source_lines=source_lines,
        target_lines=target_lines or tuple("" for _ in source_lines),
    )


# ── 稳定读取 ─────────────────────────────────────────


def test_legacy_mapping_without_manifest_is_read_into_memory(tmp_path):
    payload = _content_payload(["原文"])
    (tmp_path / "content_mapping.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    data = read_content_mapping_snapshot(tmp_path)

    assert data["content_mappings"]["line_000001"]["original_text"] == "原文"


def test_snapshot_retries_when_generation_file_is_collected(tmp_path, monkeypatch):
    g2 = tmp_path / ".mapping_generations" / "g2"
    g2.mkdir(parents=True)
    payload = _content_payload(["新内容"])
    (g2 / "content_mapping.json").write_text(json.dumps(payload), encoding="utf-8")

    manifests = iter(
        [
            {
                "generation": "g1",
                "files": {"content_mapping.json": ".mapping_generations/g1/content_mapping.json"},
            },
            {
                "generation": "g2",
                "files": {"content_mapping.json": ".mapping_generations/g2/content_mapping.json"},
            },
            {
                "generation": "g2",
                "files": {"content_mapping.json": ".mapping_generations/g2/content_mapping.json"},
            },
            {
                "generation": "g2",
                "files": {"content_mapping.json": ".mapping_generations/g2/content_mapping.json"},
            },
        ]
    )
    monkeypatch.setattr(mapping_repository, "_load_manifest", lambda _root: next(manifests))

    data = read_content_mapping_snapshot(tmp_path, max_attempts=3)

    assert data["content_mappings"]["line_000001"]["original_text"] == "新内容"


@pytest.mark.parametrize("missing_member", [False, True])
def test_snapshot_gives_up_after_bounded_generation_changes(tmp_path, monkeypatch, missing_member):
    for name in ("g1", "g2"):
        directory = tmp_path / ".mapping_generations" / name
        directory.mkdir(parents=True)
        if not missing_member:
            (directory / "content_mapping.json").write_text(
                json.dumps(_content_payload(["内容"])), encoding="utf-8"
            )

    counter = {"value": 0}

    def alternating(_root):
        counter["value"] += 1
        generation = "g1" if counter["value"] % 2 else "g2"
        return {
            "generation": generation,
            "files": {
                "content_mapping.json": f".mapping_generations/{generation}/content_mapping.json"
            },
        }

    monkeypatch.setattr(mapping_repository, "_load_manifest", alternating)

    with pytest.raises(MappingSnapshotBusyError):
        read_content_mapping_snapshot(tmp_path, max_attempts=3)
    assert counter["value"] == 6


def test_stable_manifest_with_missing_member_is_a_mapping_error(tmp_path, monkeypatch):
    manifest = {
        "generation": "stable",
        "files": {"content_mapping.json": ".mapping_generations/stable/content_mapping.json"},
    }
    # A legacy compatibility copy must not hide a corrupt published generation.
    (tmp_path / "content_mapping.json").write_text(
        json.dumps(_content_payload(["legacy"])), encoding="utf-8"
    )
    reads: list[Path] = []

    def load_manifest(root):
        reads.append(root)
        return manifest

    monkeypatch.setattr(mapping_repository, "_load_manifest", load_manifest)

    with pytest.raises(MappingSnapshotError) as excinfo:
        read_content_mapping_snapshot(tmp_path)

    assert not isinstance(excinfo.value, MappingSnapshotBusyError)
    assert "content_mapping.json" in str(excinfo.value)
    assert len(reads) == 2


@pytest.mark.parametrize("old_member_exists", [False, True])
def test_changed_member_reference_is_retried_even_with_same_generation(
    tmp_path, monkeypatch, old_member_exists
):
    if old_member_exists:
        old_directory = tmp_path / ".mapping_generations" / "old"
        old_directory.mkdir(parents=True)
        (old_directory / "content_mapping.json").write_text(
            json.dumps(_content_payload(["old"])), encoding="utf-8"
        )
    directory = tmp_path / ".mapping_generations" / "new"
    directory.mkdir(parents=True)
    (directory / "content_mapping.json").write_text(
        json.dumps(_content_payload(["new"])), encoding="utf-8"
    )
    old_manifest = {
        "generation": "same",
        "files": {"content_mapping.json": ".mapping_generations/old/content_mapping.json"},
    }
    new_manifest = {
        "generation": "same",
        "files": {"content_mapping.json": ".mapping_generations/new/content_mapping.json"},
    }
    manifests = iter([old_manifest, new_manifest, new_manifest, new_manifest])
    monkeypatch.setattr(mapping_repository, "_load_manifest", lambda _root: next(manifests))

    data = read_content_mapping_snapshot(tmp_path)

    assert data["content_mappings"]["line_000001"]["original_text"] == "new"


def test_corrupt_content_mapping_fails_explicitly(tmp_path):
    (tmp_path / "content_mapping.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(MappingSnapshotError):
        read_content_mapping_snapshot(tmp_path)


def test_missing_legacy_mapping_is_a_mapping_error_not_busy(tmp_path):
    with pytest.raises(MappingSnapshotError) as excinfo:
        read_content_mapping_snapshot(tmp_path, max_attempts=2)

    assert not isinstance(excinfo.value, MappingSnapshotBusyError)
    assert "content_mapping.json" in str(excinfo.value)


# ── 语言标签 ─────────────────────────────────────────


def test_language_tag_uses_explicit_local_name_mapping():
    assert resolve_language_tag("中文") == "zh"
    assert resolve_language_tag("简体中文") == "zh-Hans"
    assert resolve_language_tag("日语") == "ja"


def test_language_tag_passes_through_explicit_bcp47_values():
    assert resolve_language_tag("zh-CN") == "zh-CN"


def test_unknown_language_values_are_omitted():
    assert resolve_language_tag("未知语言") is None
    assert resolve_language_tag("") is None
    assert resolve_language_tag(None) is None


# ── 快照对齐 ─────────────────────────────────────────


def _mapping_dir(tmp_path: Path) -> Path:
    mapping_dir = tmp_path / "mapping"
    mapping_dir.mkdir()
    (mapping_dir / "book.epub").write_bytes(b"epub")
    return mapping_dir


def _validate(mapping_dir: Path, payload: dict, snapshot: EpubDocumentSnapshot) -> list[dict]:
    payload["project_info"]["original_file"] = str(mapping_dir / "book.epub")
    return _validate_bilingual_snapshot(
        mapping_dir, payload["project_info"], payload["content_mappings"], snapshot
    )


def test_snapshot_rejects_duplicate_line_numbers(tmp_path):
    mapping_dir = _mapping_dir(tmp_path)
    payload = _content_payload(["一", "二"])
    payload["content_mappings"]["line_000002"]["line_number"] = 1

    with pytest.raises(BilingualEpubExportError, match="行号重复"):
        _validate(mapping_dir, payload, _snapshot(mapping_dir, ("一", "二")))


def test_snapshot_rejects_missing_line_number(tmp_path):
    mapping_dir = _mapping_dir(tmp_path)
    payload = _content_payload(["一", "二"])
    payload["content_mappings"]["line_000002"]["line_number"] = 3

    with pytest.raises(BilingualEpubExportError, match="行号缺失"):
        _validate(mapping_dir, payload, _snapshot(mapping_dir, ("一", "二")))


def test_snapshot_rejects_count_mismatch(tmp_path):
    mapping_dir = _mapping_dir(tmp_path)
    payload = _content_payload(["一"])

    with pytest.raises(BilingualEpubExportError, match="映射记录数"):
        _validate(mapping_dir, payload, _snapshot(mapping_dir, ("一", "二")))


def test_snapshot_rejects_edited_source_text(tmp_path):
    mapping_dir = _mapping_dir(tmp_path)
    payload = _content_payload(["原始原文"])

    with pytest.raises(BilingualEpubExportError, match="原文已编辑"):
        _validate(mapping_dir, payload, _snapshot(mapping_dir, ("界面里改过的原文",)))


def test_snapshot_rejects_mapping_from_another_project(tmp_path):
    mapping_dir = _mapping_dir(tmp_path)
    other_source = tmp_path / "other.epub"
    other_source.write_bytes(b"epub")
    payload = _content_payload(["一"])
    payload["project_info"]["original_file"] = str(other_source)

    with pytest.raises(BilingualEpubExportError, match="不属于同一项目"):
        _validate_bilingual_snapshot(
            mapping_dir,
            payload["project_info"],
            payload["content_mappings"],
            _snapshot(mapping_dir, ("一",)),
        )


def test_overlay_replaces_only_the_private_mapping_copy(tmp_path):
    mapping_dir = _mapping_dir(tmp_path)
    payload = _content_payload(["一", "一"])
    snapshot = _snapshot(mapping_dir, ("一", "一"), ("译文甲", "译文乙"))
    ordered = _validate(mapping_dir, payload, snapshot)

    _overlay_snapshot_targets(ordered, snapshot)

    assert [record["translated_text"] for record in ordered] == ["译文甲", "译文乙"]
    assert payload["content_mappings"]["line_000002"]["original_text"] == "一"
