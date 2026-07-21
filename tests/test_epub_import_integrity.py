"""EPUB 导入完整性与队列取消的 P1 回归测试。"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from src.core.concurrent_manager import ConcurrentTranslationManager
from src.core.epub_processor import EpubImportCancelled, EpubImportPartialError, EPUBProcessor


def test_asset_only_image_cache_is_reparsed_with_base64_fallback(tmp_app_paths, make_epub):
    """旧 V1.6 资产缓存必须重解析，不能继续复用可能被改写的落盘图片。"""
    image_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    epub_path = make_epub(
        chapters=[("chapter", ["text"])],
        images=[("Images/page.png", image_data)],
    )
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    first = processor.import_epub(str(epub_path), extract_images=True)
    images_file = Path(first["images_file"])
    old_payload = json.loads(images_file.read_text(encoding="utf-8"))
    old_payload.pop("schema_version", None)
    for info in old_payload["image_mappings"].values():
        info.pop("base64_data", None)
    images_file.write_text(json.dumps(old_payload), encoding="utf-8")

    second = processor.import_epub(str(epub_path), extract_images=True)
    repaired = json.loads(Path(second["images_file"]).read_text(encoding="utf-8"))
    info = repaired["image_mappings"]["Images/page.png"]

    assert repaired["schema_version"] == 2
    assert base64.b64decode(info["base64_data"].split(",", 1)[1]) == image_data


def test_chapter_parse_failure_preserves_previous_complete_mapping(
    tmp_app_paths, simple_epub, monkeypatch
):
    """章节失败必须终止导入，不能把旧的完整 mapping 覆盖为缺章版本。"""
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    initial = processor.import_epub(str(simple_epub), extract_images=False)
    mapping_dir = Path(initial["mapping_dir"])
    tracked_files = [
        mapping_dir / "content_mapping.json",
        mapping_dir / "images.json",
        mapping_dir / "format_info.json",
    ]
    before = {path: path.read_bytes() for path in tracked_files}

    # 绕过 size + mtime 快速复用，让第二次导入进入正文解析路径。
    stat = simple_epub.stat()
    os.utime(simple_epub, (stat.st_atime, stat.st_mtime + 2))

    from bs4 import BeautifulSoup as real_soup

    calls = 0

    def fail_second_chapter(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated malformed chapter")
        return real_soup(*args, **kwargs)

    monkeypatch.setattr("bs4.BeautifulSoup", fail_second_chapter)

    with pytest.raises(EpubImportPartialError, match="第 2 章"):
        processor.import_epub(str(simple_epub), extract_images=False)

    assert {path: path.read_bytes() for path in tracked_files} == before


def test_concurrent_manager_forwards_epub_cancel_callback():
    """队列管理器必须把在途导入的取消令牌交给 EPUB 处理器。"""

    class _Processor:
        def __init__(self):
            self.cancel_requested = None

        def import_epub(self, _path, *, cancel_requested=None):
            self.cancel_requested = cancel_requested
            assert cancel_requested is not None
            assert cancel_requested()
            raise EpubImportCancelled("cancelled by queue")

    manager = ConcurrentTranslationManager.__new__(ConcurrentTranslationManager)
    processor = _Processor()
    manager._epub_processor = processor

    with pytest.raises(EpubImportCancelled):
        manager.add_task("book.epub", cancel_requested=lambda: True)

    assert processor.cancel_requested is not None
