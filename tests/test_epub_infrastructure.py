#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 4（EPUB 拆分）infrastructure 层模块测试

验证新提取的模块独立可测试：
- document_order：spine 遍历、章节 ID 归一化、item 辅助方法
- segment_extractor：块级标签选择、段落提取、定位符匹配
- mapping_repository：映射文件读写
- image_rewriter：图片路径匹配
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.infrastructure.document_order import (
    iter_spine_documents,
    normalize_chapter_id,
    get_item_name,
    get_item_media_type,
)
from src.infrastructure.segment_extractor import (
    BLOCK_TAGS,
    compute_source_checksum,
    is_leaf_block,
    extract_segments_from_document,
    match_existing_translation,
)
from src.infrastructure.mapping_repository import (
    load_content_mapping,
    save_translations,
    load_old_translations,
)
from src.infrastructure.image_rewriter import (
    match_and_get_new_path,
    inject_figcaption,
)


# ── document_order 测试 ────────────────────────────────


class TestNormalizeChapterId:
    def test_preserves_directory(self):
        assert normalize_chapter_id("part1/chapter.xhtml") == "part1/chapter.xhtml"

    def test_removes_oebps_prefix(self):
        assert normalize_chapter_id("OEBPS/Text/chapter1.xhtml") == "Text/chapter1.xhtml"

    def test_removes_epub_prefix(self):
        assert normalize_chapter_id("EPUB/chapter.xhtml") == "chapter.xhtml"

    def test_removes_ops_prefix(self):
        assert normalize_chapter_id("OPS/chapter.xhtml") == "chapter.xhtml"

    def test_case_insensitive_prefix(self):
        assert normalize_chapter_id("oebps/Text/c.xhtml") == "Text/c.xhtml"

    def test_windows_separator(self):
        assert normalize_chapter_id("OEBPS\\Text\\chapter.xhtml") == "Text/chapter.xhtml"

    def test_empty_string(self):
        assert normalize_chapter_id("") == ""

    def test_nested_dirs_dont_collapse(self):
        """不同目录的同名文件归一化后不同"""
        a = normalize_chapter_id("part1/chapter.xhtml")
        b = normalize_chapter_id("part2/chapter.xhtml")
        assert a != b


class TestGetItemName:
    def test_file_name_attr(self):
        item = MagicMock()
        item.file_name = "OEBPS/Text/c.xhtml"
        assert get_item_name(item) == "OEBPS/Text/c.xhtml"

    def test_href_attr(self):
        item = MagicMock()
        del item.file_name
        item.href = "chapter.xhtml"
        assert get_item_name(item) == "chapter.xhtml"

    def test_get_name_method(self):
        item = MagicMock()
        del item.file_name
        del item.href
        item.get_name.return_value = "c.xhtml"
        assert get_item_name(item) == "c.xhtml"

    def test_empty_when_no_attr(self):
        item = MagicMock()
        del item.file_name
        del item.href
        del item.get_name
        assert get_item_name(item) == ""


class TestGetItemMediaType:
    def test_get_media_type_method(self):
        item = MagicMock()
        item.get_media_type.return_value = "image/png"
        assert get_item_media_type(item) == "image/png"

    def test_infer_from_extension_html(self):
        item = MagicMock()
        del item.get_media_type
        del item.media_type
        item.file_name = "chapter.xhtml"
        assert get_item_media_type(item) == "application/xhtml+xml"

    def test_infer_from_extension_png(self):
        item = MagicMock()
        del item.get_media_type
        del item.media_type
        item.file_name = "cover.png"
        assert get_item_media_type(item) == "image/png"


class TestIterSpineDocuments:
    def test_string_spine(self):
        """字符串 idref 通过 get_item_with_id 解析"""
        import ebooklib
        book = MagicMock()
        doc = MagicMock()
        doc.get_type.return_value = ebooklib.ITEM_DOCUMENT
        doc.file_name = "c1.xhtml"
        book.get_item_with_id.return_value = doc
        book.spine = ["c1"]

        result = list(iter_spine_documents(book))
        assert len(result) == 1
        assert result[0] == doc

    def test_skip_nonlinear(self):
        """linear='no' 的条目被跳过"""
        import ebooklib
        book = MagicMock()
        book.get_item_with_id.return_value = None
        book.spine = [("c1", "no")]

        result = list(iter_spine_documents(book))
        assert len(result) == 0

    def test_dedup(self):
        """重复 idref 去重"""
        import ebooklib
        book = MagicMock()
        doc = MagicMock()
        doc.get_type.return_value = ebooklib.ITEM_DOCUMENT
        doc.file_name = "c1.xhtml"
        book.get_item_with_id.return_value = doc
        book.spine = ["c1", "c1"]

        result = list(iter_spine_documents(book))
        assert len(result) == 1


# ── segment_extractor 测试 ─────────────────────────────


class TestBlockTags:
    def test_block_tags_is_frozen(self):
        """BLOCK_TAGS 是 frozenset，不可变"""
        assert isinstance(BLOCK_TAGS, frozenset)

    def test_contains_p(self):
        assert "p" in BLOCK_TAGS

    def test_contains_h1(self):
        assert "h1" in BLOCK_TAGS

    def test_not_contains_div(self):
        assert "div" not in BLOCK_TAGS


class TestComputeSourceChecksum:
    def test_same_text_same_checksum(self):
        assert compute_source_checksum("hello") == compute_source_checksum("hello")

    def test_different_text_different_checksum(self):
        assert compute_source_checksum("hello") != compute_source_checksum("world")

    def test_checksum_length(self):
        assert len(compute_source_checksum("test")) == 8


class TestIsLeafBlock:
    def test_p_without_block_children(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<p>text</p>", "html.parser")
        p = soup.find("p")
        assert is_leaf_block(p) is True

    def test_div_with_p_child(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div><p>text</p></div>", "html.parser")
        div = soup.find("div")
        assert is_leaf_block(div) is False


class TestExtractSegments:
    def test_extract_from_simple_doc(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<html><body><p>hello</p><p>world</p></body></html>", "html.parser")
        segments, next_line, count = extract_segments_from_document(soup, "ch1", 1)
        assert count == 2
        assert next_line == 3
        assert len(segments) == 2
        assert list(segments.values())[0]["original_text"] == "hello"
        assert list(segments.values())[0]["chapter_id"] == "ch1"

    def test_skip_empty_text(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<html><body><p></p><p>text</p></body></html>", "html.parser")
        segments, _, count = extract_segments_from_document(soup, "ch1", 1)
        assert count == 1

    def test_skip_nested_blocks(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div><p>inner</p></div>", "html.parser")
        segments, _, count = extract_segments_from_document(soup, "ch1", 1)
        # div 有 p 子标签，不是叶子块，被跳过；p 是叶子块
        assert count == 1


class TestMatchExistingTranslation:
    def test_locator_match(self):
        existing_by_locator = {"ch1|0|abc12345": {"translated_text": "你好", "translated_at": "2024"}}
        text_occ: dict = {}
        result = match_existing_translation(
            "ch1", 0, "abc12345", "hello",
            existing_by_locator, {}, {}, text_occ,
        )
        assert result == ("你好", "2024")

    def test_seq_match_with_checksum(self):
        existing_by_seq = {"ch1|0": {"translated_text": "你好", "translated_at": "2024",
                                      "source_checksum": "abc12345", "original_text": "hello"}}
        text_occ: dict = {}
        result = match_existing_translation(
            "ch1", 0, "abc12345", "hello",
            {}, existing_by_seq, {}, text_occ,
        )
        assert result == ("你好", "2024")

    def test_seq_match_rejects_changed_source(self):
        existing_by_seq = {"ch1|0": {"translated_text": "旧译文", "translated_at": "2024",
                                      "source_checksum": "oldcheck", "original_text": "old text"}}
        text_occ: dict = {}
        result = match_existing_translation(
            "ch1", 0, "newcheck", "new text",
            {}, existing_by_seq, {}, text_occ,
        )
        assert result == ("", "")

    def test_text_fallback(self):
        existing_translations = {"hello": {"translated_text": "你好", "translated_at": "2024"}}
        text_occ: dict = {}
        result = match_existing_translation(
            "ch1", 0, "abc12345", "hello",
            {}, {}, existing_translations, text_occ,
        )
        assert result == ("你好", "2024")
        assert text_occ["hello"] == 1

    def test_no_match(self):
        text_occ: dict = {}
        result = match_existing_translation(
            "ch1", 0, "abc12345", "hello",
            {}, {}, {}, text_occ,
        )
        assert result == ("", "")


# ── mapping_repository 测试 ────────────────────────────


class TestMappingRepository:
    def test_save_and_load(self, tmp_path):
        """保存后加载，译文按行号对齐"""
        from src.infrastructure.mapping_repository import save_content_mapping
        content_mappings = {
            "line_000001": {"original_text": "a", "translated_text": "", "line_number": 1},
            "line_000002": {"original_text": "b", "translated_text": "", "line_number": 2},
        }
        project_info = {"project_id": "test"}
        save_content_mapping(tmp_path, content_mappings, project_info)

        save_translations(str(tmp_path), ["甲", "乙"])
        originals, translations = load_content_mapping(str(tmp_path))
        assert originals == ["a", "b"]
        assert translations == ["甲", "乙"]

    def test_load_old_translations(self, tmp_path):
        """加载旧翻译数据用于 reimport"""
        from src.infrastructure.mapping_repository import save_content_mapping
        content_mappings = {
            "line_000001": {
                "original_text": "hello", "translated_text": "你好",
                "line_number": 1, "chapter_id": "ch1", "block_index": 0,
                "source_checksum": "abc12345", "translated_at": "2024",
            },
        }
        save_content_mapping(tmp_path, content_mappings, {"project_id": "test"})

        existing_text, existing_locator, existing_seq = load_old_translations(tmp_path)
        assert "hello" in existing_text
        assert "ch1|0|abc12345" in existing_locator
        assert "ch1|0" in existing_seq

    def test_load_old_translations_empty(self, tmp_path):
        """空目录返回空字典"""
        existing_text, existing_locator, existing_seq = load_old_translations(tmp_path)
        assert existing_text == {}
        assert existing_locator == {}
        assert existing_seq == {}


# ── image_rewriter 测试 ────────────────────────────────


class TestMatchAndGetNewPath:
    def test_exact_match(self):
        path_mapping = {"images/cover.jpg": "images/cover_new.jpg"}
        matched, rel = match_and_get_new_path("cover.jpg", path_mapping, Path("."))
        assert matched is True
        assert "cover_new.jpg" in rel

    def test_no_match(self):
        path_mapping = {"images/a.jpg": "images/a_new.jpg"}
        matched, rel = match_and_get_new_path("nonexistent.png", path_mapping, Path("."))
        assert matched is False

    def test_path_tail_match(self):
        path_mapping = {"OEBPS/images/cover.jpg": "OEBPS/images/cover_new.jpg"}
        matched, rel = match_and_get_new_path("images/cover.jpg", path_mapping, Path("OEBPS"))
        assert matched is True


class TestInjectFigcaption:
    def test_inject_when_matched(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<html><body><img src='images/cover.jpg'></body></html>", "html.parser")
        image_text_map = {"images/cover.jpg": {"translated_text": "封面", "original_text": "Cover"}}
        inject_figcaption(soup, image_text_map, "ch1")
        figcaption = soup.find("figcaption")
        assert figcaption is not None
        assert "封面" in figcaption.string

    def test_no_inject_when_no_match(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<html><body><img src='other.jpg'></body></html>", "html.parser")
        image_text_map = {"images/cover.jpg": {"translated_text": "封面"}}
        inject_figcaption(soup, image_text_map, "ch1")
        assert soup.find("figcaption") is None

    def test_no_inject_when_empty_map(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<html><body><img src='cover.jpg'></body></html>", "html.parser")
        inject_figcaption(soup, {}, "ch1")
        assert soup.find("figcaption") is None
