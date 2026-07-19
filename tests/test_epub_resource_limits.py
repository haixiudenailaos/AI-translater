"""ENG-4: untrusted EPUB archives must stay inside defined resource limits."""

from __future__ import annotations

from zipfile import ZipFile

import pytest

from src.core import epub_processor
from src.core.epub_processor import EpubArchiveValidationError, _validate_epub_archive


def _write_archive(path, members: dict[str, bytes]) -> None:
    with ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_archive_rejects_parent_directory_member(tmp_path):
    path = tmp_path / "traversal.epub"
    _write_archive(path, {"OPS/../outside.xhtml": b"content"})

    with pytest.raises(EpubArchiveValidationError, match="不安全"):
        _validate_epub_archive(path)


def test_archive_rejects_invalid_zip(tmp_path):
    path = tmp_path / "invalid.epub"
    path.write_bytes(b"not a zip")

    with pytest.raises(EpubArchiveValidationError, match="有效"):
        _validate_epub_archive(path)


def test_archive_enforces_member_and_total_uncompressed_limits(tmp_path, monkeypatch):
    member_path = tmp_path / "member-limit.epub"
    _write_archive(member_path, {"OPS/large.xhtml": b"x" * 33})
    monkeypatch.setattr(epub_processor, "_MAX_EPUB_MEMBER_BYTES", 32)

    with pytest.raises(EpubArchiveValidationError, match="单个资源"):
        _validate_epub_archive(member_path)

    total_path = tmp_path / "total-limit.epub"
    _write_archive(total_path, {"OPS/a.xhtml": b"x" * 20, "OPS/b.xhtml": b"y" * 20})
    monkeypatch.setattr(epub_processor, "_MAX_EPUB_MEMBER_BYTES", 32)
    monkeypatch.setattr(epub_processor, "_MAX_EPUB_TOTAL_BYTES", 32)

    with pytest.raises(EpubArchiveValidationError, match="总大小"):
        _validate_epub_archive(total_path)


def test_archive_enforces_compression_ratio(tmp_path, monkeypatch):
    path = tmp_path / "ratio.epub"
    with ZipFile(path, "w", compression=8) as archive:
        archive.writestr("OPS/repeated.xhtml", b"0" * 8192)
    monkeypatch.setattr(epub_processor, "_MAX_EPUB_COMPRESSION_RATIO", 2)

    with pytest.raises(EpubArchiveValidationError, match="压缩比"):
        _validate_epub_archive(path)


def test_archive_validation_honors_cancellation(tmp_path):
    path = tmp_path / "cancel.epub"
    _write_archive(path, {"OPS/content.xhtml": b"content"})

    with pytest.raises(epub_processor.EpubImportCancelled):
        _validate_epub_archive(path, cancel_requested=lambda: True)
