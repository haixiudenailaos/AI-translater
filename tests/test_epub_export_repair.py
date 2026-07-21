#!/usr/bin/env python3
"""
EPUB 导出"原文混入与图片相邻空白页"修复回归测试

验证 docs/EPUB_EXPORT_MIXED_TEXT_AND_BLANK_PAGE_REPAIR.md 第 8 节要求的修复行为：

8.1 原文残留
- 内联标签（em/strong/a/ruby/span）内的原文被清除，不残留
- 标签本身和属性保留
- 多个内联文本节点时，输出可见文本与译文一致

8.2 图片容器索引
- 图片后带单字标题、标点的块不会改变后续段落映射顺序
- 导入和导出使用同一段落计数

8.3 图片资源
- Manga Provider 的 translated_images/manga/... 结果可被导出器读取
- 旧版 images/<filename> 结果仍可读取
- src 带 fragment/query 可正确匹配
- 结果文件缺失时进入 failed_images

8.4 路径规范化与 UID 唯一性
- 同名图片、不同目录不会 UID 冲突
- OEBPS/EPUB/OPS 路径坐标被保留
- 封面不会被批量插图翻译覆盖
- 可解码的结果图片不受宽高比限制
"""

import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from src.infrastructure.exporter import (
    SegmentCountMismatchError,
    _configure_single_page_pagination,
    _replace_text_preserving_inline,
)
from src.infrastructure.image_rewriter import (
    _generate_image_uid,
    _normalize_epub_image_path,
    _resolve_local_image_path,
    add_translated_images,
    inject_figcaption,
    match_and_get_new_path,
    set_document_content,
)
from src.infrastructure.mapping_repository import publish_mapping_file_update


def _png_bytes(width: int = 1, height: int = 1) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


def _publish_content_mapping(mapping_dir: str | Path, payload: dict) -> None:
    """Publish test edits through the production mapping generation contract."""
    publish_mapping_file_update(mapping_dir, "content_mapping.json", payload)


# ── 8.1 原文残留 ──────────────────────────────────────


class TestReplaceTextPreservingInline:
    """根因一：内联标签中的原文残留。"""

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser")

    def test_em_inner_text_cleared(self):
        """<em> 内部的原文被清除，译文写入第一个文本节点位置。"""
        soup = self._make_soup("<p>这是<em>强调</em>内容</p>")
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "translated")
        result = str(soup)
        assert "translated" in result
        # 核心断言：原文"强调"必须被清除
        assert "强调" not in result
        # em 标签本身保留（可能序列化为 <em></em> 或 <em/>）
        assert re.search(r"<em[\s/>]", result)

    def test_strong_inner_text_cleared(self):
        """<strong> 内部原文清除，标签保留。"""
        soup = self._make_soup("<p>前缀<strong>重要</strong>后缀</p>")
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "译文")
        result = str(soup)
        assert "重要" not in result
        assert "前缀" not in result
        assert "后缀" not in result
        assert "译文" in result
        assert re.search(r"<strong[\s/>]", result)

    def test_anchor_href_preserved(self):
        """<a> 标签的 href 属性保留，内部文本清除。"""
        soup = self._make_soup('<p>访问<a href="https://example.com">链接</a>详情</p>')
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "译文")
        result = str(soup)
        assert "链接" not in result
        assert "详情" not in result
        assert "https://example.com" in result
        assert "译文" in result

    def test_ruby_tags_preserved_text_cleared(self):
        """<ruby> 结构保留，内部所有文本（含 rb/rt）清除。"""
        soup = self._make_soup("<p><ruby><rb>漢字</rb><rt>かんじ</rt></ruby></p>")
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "译文")
        result = str(soup)
        # 短期实现：ruby 内部所有原文文本被清除
        assert "漢字" not in result
        assert "かんじ" not in result
        # 标签结构保留
        assert "<ruby" in result
        assert "<rb" in result
        assert "<rt" in result
        # 译文写入
        assert "译文" in result

    def test_span_inner_text_cleared(self):
        """<span> 内部文本清除，标签保留。"""
        soup = self._make_soup('<p>前<span class="x">中</span>后</p>')
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "译文")
        result = str(soup)
        assert "前" not in result
        assert "中" not in result
        assert "后" not in result
        assert "译文" in result
        assert 'class="x"' in result

    def test_multiple_inline_nodes_all_cleared(self):
        """一个块含多个内联文本节点时，输出可见文本与译文完全一致。"""
        soup = self._make_soup("<p>甲<em>乙</em>丙<strong>丁</strong>戊</p>")
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "完整译文")
        # 使用 get_text 验证可见文本
        new_soup = self._make_soup(str(soup))
        visible_text = new_soup.get_text()
        assert visible_text == "完整译文"

    def test_no_text_children_inserts_translation(self):
        """块节点无文本子节点时，译文插入到开头。"""
        soup = self._make_soup("<p><br/></p>")
        p = soup.find("p")
        _replace_text_preserving_inline(soup, p, "插入的译文")
        result = str(soup)
        assert "插入的译文" in result


# ── 8.3 图片资源：路径解析与契约 ────────────────────────


class TestResolveLocalImagePath:
    """根因三：统一 result_map 相对路径契约。"""

    def test_manga_relative_path_resolved(self, tmp_path):
        """Manga Provider 的新格式：值为相对于 mapping_dir 的路径。"""
        # 构造 mapping_dir/translated_images/manga/i001_hash.png
        manga_dir = tmp_path / "translated_images" / "manga"
        manga_dir.mkdir(parents=True)
        img_file = manga_dir / "i001_abc123.png"
        img_file.write_bytes(b"fake png")

        result = _resolve_local_image_path(tmp_path, "translated_images/manga/i001_abc.png")
        # 文件名不完全匹配，应返回相对路径候选
        assert result == tmp_path / "translated_images/manga/i001_abc.png"

        # 完全匹配的路径
        result2 = _resolve_local_image_path(tmp_path, "translated_images/manga/i001_abc123.png")
        assert result2 == img_file
        assert result2.exists()

    def test_legacy_filename_resolved(self, tmp_path):
        """旧 AI Provider 格式：值仅为文件名，文件位于 mapping_dir/images/。"""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        img_file = images_dir / "cover_translated.png"
        img_file.write_bytes(b"fake png")

        result = _resolve_local_image_path(tmp_path, "cover_translated.png")
        assert result == img_file
        assert result.exists()

    def test_relative_path_preferred_over_legacy(self, tmp_path):
        """若相对路径存在，优先使用（即使 images/ 下也有同名）。"""
        # 同时存在两种位置
        manga_dir = tmp_path / "translated_images" / "manga"
        manga_dir.mkdir(parents=True)
        (manga_dir / "x.png").write_bytes(b"manga")

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "x.png").write_bytes(b"legacy")

        # 传入相对路径：优先按相对路径解析
        result = _resolve_local_image_path(tmp_path, "translated_images/manga/x.png")
        assert result == manga_dir / "x.png"

    def test_empty_value_returns_none(self, tmp_path):
        assert _resolve_local_image_path(tmp_path, "") is None
        assert _resolve_local_image_path(tmp_path, None) is None

    def test_nonexistent_returns_candidate(self, tmp_path):
        """不存在的文件返回候选路径（调用方检查 exists）。"""
        result = _resolve_local_image_path(tmp_path, "translated_images/manga/missing.png")
        assert result == tmp_path / "translated_images/manga/missing.png"
        assert not result.exists()

    def test_parent_traversal_is_rejected(self, tmp_path):
        assert _resolve_local_image_path(tmp_path, "../outside.png") is None


class TestAddTranslatedImagesBothFormats:
    """根因三：add_translated_images 兼容新旧两种 result_map 格式。"""

    def _make_book(self):
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("img-test")
        book.set_title("图片测试")
        book.set_language("ja")
        return book

    def test_manga_format_image_added(self, tmp_path):
        """Manga Provider 格式：result_map 值为 translated_images/manga/..."""
        book = self._make_book()
        manga_dir = tmp_path / "translated_images" / "manga"
        manga_dir.mkdir(parents=True)
        png_bytes = _png_bytes()
        img_file = manga_dir / "i001_abc123_def45678.png"
        img_file.write_bytes(png_bytes)

        image_map = {"OEBPS/Images/i001.jpg": "translated_images/manga/i001_abc123_def45678.png"}
        path_mapping, failed = add_translated_images(book, image_map, tmp_path)

        assert failed == 0
        assert "OEBPS/Images/i001.jpg" in path_mapping
        # EPUB 内路径应保留原资源所在的路径坐标。
        new_path = path_mapping["OEBPS/Images/i001.jpg"]
        assert new_path.endswith("i001_abc123_def45678.png")
        assert new_path.startswith("OEBPS/Images/")
        # 新图片 item 应已添加到 book
        import ebooklib

        image_items = [it for it in book.get_items() if it.get_type() == ebooklib.ITEM_IMAGE]
        assert len(image_items) >= 1

    def test_legacy_format_image_added(self, tmp_path):
        """旧 AI Provider 格式：result_map 值仅为文件名。"""
        book = self._make_book()
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        img_file = images_dir / "cover_abc_translated.png"
        img_file.write_bytes(_png_bytes())

        image_map = {"Images/cover.jpg": "cover_abc_translated.png"}
        path_mapping, failed = add_translated_images(book, image_map, tmp_path)

        assert failed == 0
        assert "Images/cover.jpg" in path_mapping
        assert path_mapping["Images/cover.jpg"].endswith("cover_abc_translated.png")

    def test_missing_file_counted_as_failed(self, tmp_path):
        """结果文件缺失时进入 failed_images，返回失败计数。"""
        book = self._make_book()
        image_map = {"Images/missing.jpg": "translated_images/manga/nonexistent.png"}
        path_mapping, failed = add_translated_images(book, image_map, tmp_path)

        assert failed == 1
        assert "Images/missing.jpg" not in path_mapping
        assert path_mapping == {}

    def test_mixed_formats_in_one_call(self, tmp_path):
        """同一批次中混合新旧格式，各自按对应规则解析。"""
        book = self._make_book()
        # 准备两种位置的文件
        manga_dir = tmp_path / "translated_images" / "manga"
        manga_dir.mkdir(parents=True)
        images_dir = tmp_path / "images"
        images_dir.mkdir()

        real_png = _png_bytes()
        (manga_dir / "manga_img.png").write_bytes(real_png)
        (images_dir / "legacy_img.png").write_bytes(real_png)

        image_map = {
            "Images/manga.jpg": "translated_images/manga/manga_img.png",
            "Images/legacy.jpg": "legacy_img.png",
        }
        path_mapping, failed = add_translated_images(book, image_map, tmp_path)

        assert failed == 0
        assert len(path_mapping) == 2
        assert "Images/manga.jpg" in path_mapping
        assert "Images/legacy.jpg" in path_mapping

    def test_epub3_cover_is_kept_original(self, tmp_path):
        """manifest 中的 cover-image 不得被批量插图翻译覆盖。"""
        from ebooklib import epub

        book = self._make_book()
        original = epub.EpubImage(
            uid="cover",
            file_name="Images/cover.jpg",
            media_type="image/jpeg",
            content=b"\xff\xd8\xff" + b"original",
        )
        original.properties = ["cover-image"]
        book.add_item(original)

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        translated = images_dir / "cover_translated.png"
        translated.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        )

        path_mapping, failed = add_translated_images(
            book,
            {"Images/cover.jpg": "cover_translated.png"},
            tmp_path,
        )

        assert failed == 0
        assert path_mapping == {}
        assert original.get_content().startswith(b"\xff\xd8\xff")

    def test_different_aspect_ratio_is_accepted(self, tmp_path):
        """可正常解码的生成图不应因宽高比变化而被拒绝。"""
        from io import BytesIO

        from ebooklib import epub
        from PIL import Image

        def png(width, height):
            buffer = BytesIO()
            Image.new("RGB", (width, height), "white").save(buffer, "PNG")
            return buffer.getvalue()

        book = self._make_book()
        original = epub.EpubImage(
            uid="inner",
            file_name="Images/inner.png",
            media_type="image/png",
            content=png(200, 100),
        )
        book.add_item(original)

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "inner_translated.png").write_bytes(png(100, 100))

        path_mapping, failed = add_translated_images(
            book,
            {"Images/inner.png": "inner_translated.png"},
            tmp_path,
        )

        assert failed == 0
        assert path_mapping["Images/inner.png"].endswith("inner_translated.png")

    def test_decodable_uncommon_format_is_converted_to_png(self, tmp_path):
        """能正常解码的非常用格式应转为 EPUB 兼容的 PNG。"""
        book = self._make_book()
        images_dir = tmp_path / "images"
        images_dir.mkdir()

        buffer = BytesIO()
        Image.new("RGB", (32, 24), "white").save(buffer, "BMP")
        (images_dir / "translated_result.jpg").write_bytes(buffer.getvalue())

        path_mapping, failed = add_translated_images(
            book,
            {"Images/inner.jpg": "translated_result.jpg"},
            tmp_path,
        )

        assert failed == 0
        assert path_mapping["Images/inner.jpg"].endswith("translated_result.png")
        translated_item = next(
            item
            for item in book.get_items()
            if getattr(item, "file_name", "").endswith("translated_result.png")
        )
        assert translated_item.media_type == "image/png"
        assert translated_item.get_content().startswith(b"\x89PNG\r\n\x1a\n")


# ── 8.3 src query/fragment 匹配 ────────────────────────


class TestMatchAndGetNewPathQueryFragment:
    """根因三：src 带 query/fragment 时仍能正确匹配。"""

    def test_src_with_fragment_matches(self):
        """../image/a.jpg#fragment 应能匹配到 a.jpg。"""
        path_mapping = {"OEBPS/images/a.jpg": "OEBPS/images/a_new.jpg"}
        matched, rel = match_and_get_new_path(
            "../images/a.jpg#fragment", path_mapping, Path("OEBPS/Text")
        )
        assert matched is True
        assert "a_new.jpg" in rel

    def test_src_with_query_matches(self):
        """../image/a.jpg?x=1 应能匹配到 a.jpg。"""
        path_mapping = {"OEBPS/images/a.jpg": "OEBPS/images/a_new.jpg"}
        matched, rel = match_and_get_new_path(
            "../images/a.jpg?x=1", path_mapping, Path("OEBPS/Text")
        )
        assert matched is True
        assert "a_new.jpg" in rel

    def test_src_with_both_fragment_and_query(self):
        """同时带 query 和 fragment：a.jpg?x=1#frag。"""
        path_mapping = {"OEBPS/images/a.jpg": "OEBPS/images/a_new.jpg"}
        matched, rel = match_and_get_new_path(
            "../images/a.jpg?x=1#frag", path_mapping, Path("OEBPS/Text")
        )
        assert matched is True

    def test_clean_src_still_matches(self):
        """无 query/fragment 的 src 仍能匹配（回归保护）。"""
        path_mapping = {"OEBPS/images/a.jpg": "OEBPS/images/a_new.jpg"}
        matched, _ = match_and_get_new_path("../images/a.jpg", path_mapping, Path("OEBPS/Text"))
        assert matched is True

    def test_same_basename_in_different_directories_does_not_cross_match(self):
        path_mapping = {
            "part1/images/cover.jpg": "part1/images/cover_new.jpg",
            "part2/images/cover.jpg": "part2/images/cover_new.jpg",
        }
        matched, rel = match_and_get_new_path(
            "../images/cover.jpg", path_mapping, Path("part2/Text")
        )
        assert matched is True
        assert rel == "../images/cover_new.jpg"

    def test_bare_basename_does_not_choose_an_ambiguous_directory(self):
        path_mapping = {
            "part1/cover.jpg": "part1/cover_new.jpg",
            "part2/cover.jpg": "part2/cover_new.jpg",
        }
        matched, rel = match_and_get_new_path("cover.jpg", path_mapping, Path("."))
        assert matched is False
        assert rel == ""


# ── 8.4 路径规范化与 UID 唯一性 ────────────────────────


class TestImageUidAndPathNormalization:
    """根因三：UID 唯一性和 EPUB 内路径规范化。"""

    def test_different_paths_different_uids(self):
        """不同原始路径生成不同 UID（同名图片不冲突）。"""
        uid1 = _generate_image_uid("part1/image.png")
        uid2 = _generate_image_uid("part2/image.png")
        assert uid1 != uid2
        assert uid1.startswith("img_")
        assert uid2.startswith("img_")

    def test_same_path_same_uid(self):
        """相同路径生成相同 UID（幂等）。"""
        uid1 = _generate_image_uid("Images/cover.jpg")
        uid2 = _generate_image_uid("Images/cover.jpg")
        assert uid1 == uid2

    def test_uid_length_stable(self):
        """UID 长度固定，避免 manifest 字段超长。"""
        uid = _generate_image_uid("a/b/c/d/e/very/deep/path/image.jpg")
        # img_ + 12 hex chars = 16
        assert len(uid) == 16

    def test_normalize_keeps_oebps_coordinate(self):
        """OEBPS/ 可能属于 item 坐标，不能只从图片路径一侧移除。"""
        result = _normalize_epub_image_path(Path("OEBPS/Images"), "cover_new.png")
        assert result == "OEBPS/Images/cover_new.png"

    def test_normalize_keeps_epub_coordinate(self):
        result = _normalize_epub_image_path(Path("EPUB/Images"), "cover_new.png")
        assert result == "EPUB/Images/cover_new.png"

    def test_normalize_keeps_ops_coordinate(self):
        result = _normalize_epub_image_path(Path("OPS/Images"), "cover_new.png")
        assert result == "OPS/Images/cover_new.png"

    def test_normalize_keeps_subdirectory(self):
        """子目录结构保留（避免不同目录同名文件互相覆盖）。"""
        result = _normalize_epub_image_path(Path("Images/chapter1"), "img.png")
        assert result == "Images/chapter1/img.png"

    def test_normalize_uses_posix_separator(self):
        """Windows 路径分隔符被转换为 POSIX。"""
        result = _normalize_epub_image_path(Path("OEBPS\\Images\\chapter1"), "img.png")
        assert "\\" not in result
        assert result == "OEBPS/Images/chapter1/img.png"


class TestXhtmlWrapperPreservation:
    """导出不能让 ebooklib 重建并破坏原始 XHTML 外壳。"""

    def test_export_keeps_stylesheet_body_class_language_and_svg_attributes(
        self, tmp_app_paths, tmp_path
    ):
        from zipfile import ZipFile

        from ebooklib import epub

        from src.core.epub_processor import EPUBProcessor

        book = epub.EpubBook()
        book.set_identifier("wrapper-preservation")
        book.set_title("插图布局测试")
        book.set_language("ja")

        raw_xhtml = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja">
<head>
<link rel="stylesheet" type="text/css" href="stylesheet.css"/>
<title>image page</title>
</head>
<body class="class-0">
<svg xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet"
     viewBox="0 0 1200 1800"><image href="image.png"/></svg>
</body>
</html>"""
        chapter = epub.EpubHtml(
            uid="image-page", title="image page", file_name="image.xhtml", lang="ja"
        )
        set_document_content(chapter, raw_xhtml)
        book.add_item(chapter)
        text_chapter = epub.EpubHtml(
            uid="text-page", title="text page", file_name="text.xhtml", lang="ja"
        )
        set_document_content(
            text_chapter,
            b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja">
<head><title>text page</title></head>
<body class="class-1"><p>source paragraph</p></body>
</html>""",
        )
        book.add_item(text_chapter)
        book.add_item(
            epub.EpubItem(
                uid="style",
                file_name="stylesheet.css",
                media_type="text/css",
                content=b".class-0{text-align:center}",
            )
        )
        book.add_item(
            epub.EpubImage(
                uid="image",
                file_name="image.png",
                media_type="image/png",
                content=_png_bytes(120, 180),
            )
        )
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = [chapter, text_chapter]
        book.toc = (chapter, text_chapter)

        source_path = tmp_path / "source.epub"
        epub.write_epub(str(source_path), book)

        processor = EPUBProcessor(app_paths=tmp_app_paths)
        mapping = processor.import_epub(str(source_path), extract_images=False)
        output_path = tmp_path / "output.epub"
        processor.export_epub(mapping["mapping_dir"], str(output_path))

        with ZipFile(output_path) as archive:
            document_name = next(
                name for name in archive.namelist() if name.endswith("/image.xhtml")
            )
            exported = archive.read(document_name).decode("utf-8")
            package_name = next(name for name in archive.namelist() if name.endswith(".opf"))
            package = archive.read(package_name).decode("utf-8")

        assert 'rel="stylesheet"' in exported
        assert 'href="stylesheet.css"' in exported
        assert '<body class="class-0">' in exported
        assert 'xml:lang="ja"' in exported
        assert 'preserveAspectRatio="xMidYMid meet"' in exported
        assert 'viewBox="0 0 1200 1800"' in exported
        assert 'lang="en"' not in exported
        assert '<meta property="rendition:spread">none</meta>' in package
        assert '<meta property="rendition:layout">reflowable</meta>' in package
        assert "urn:ai-translater:" in package
        assert "rendition:spread-none" in package
        assert "rendition:page-spread-center" not in package

    def test_export_keeps_plain_image_page_without_svg_conversion(self, tmp_app_paths, tmp_path):
        from zipfile import ZipFile

        from ebooklib import epub

        from src.core.epub_processor import EPUBProcessor

        book = epub.EpubBook()
        book.set_identifier("plain-image-preservation")
        book.set_title("普通插图结构测试")
        book.set_language("ja")
        chapter = epub.EpubHtml(
            uid="plain-image", title="image page", file_name="image.xhtml", lang="ja"
        )
        original_body = (
            '<body class="class-0"><div class="class_sX-0">'
            '<img alt="" class="class_sX-1" src="image.png"/>'
            "</div></body>"
        )
        set_document_content(
            chapter,
            (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja">'
                "<head><title>image page</title></head>"
                f"{original_body}</html>"
            ).encode(),
        )
        book.add_item(chapter)
        text_chapter = epub.EpubHtml(
            uid="plain-text", title="text page", file_name="text.xhtml", lang="ja"
        )
        set_document_content(
            text_chapter,
            (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja">'
                b"<head><title>text page</title></head>"
                b'<body class="class-1"><p>source paragraph</p></body></html>'
            ),
        )
        book.add_item(text_chapter)
        book.add_item(
            epub.EpubImage(
                uid="image",
                file_name="image.png",
                media_type="image/png",
                content=_png_bytes(1120, 1600),
            )
        )
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = [chapter, text_chapter]
        book.toc = (chapter, text_chapter)

        source_path = tmp_path / "source.epub"
        epub.write_epub(str(source_path), book)
        processor = EPUBProcessor(app_paths=tmp_app_paths)
        mapping = processor.import_epub(str(source_path), extract_images=False)
        output_path = tmp_path / "output.epub"
        processor.export_epub(mapping["mapping_dir"], str(output_path))

        with ZipFile(output_path) as archive:
            document_name = next(
                name for name in archive.namelist() if name.endswith("/image.xhtml")
            )
            exported = archive.read(document_name).decode("utf-8")
            package_name = next(name for name in archive.namelist() if name.endswith(".opf"))
            package = archive.read(package_name).decode("utf-8")

        assert original_body in exported
        assert "<svg" not in exported
        assert "height:100%" not in exported
        assert "overflow:hidden" not in exported
        assert 'properties="svg"' not in package


class TestFullPageIllustrationLayout:
    """独立整页插图不能因注入说明文字而新增分页。"""

    def test_image_only_page_keeps_original_dom(self):
        from bs4 import BeautifulSoup

        html = (
            '<html><body class="class-0"><div><p>'
            '<img src="image/front.jpg" alt=""/>'
            "</p></div></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        original_body = str(soup.body)

        inject_figcaption(
            soup,
            {
                "image/front.jpg": {
                    "original_text": "原文",
                    "translated_text": "译文",
                }
            },
            "part0002.xhtml",
        )

        assert str(soup.body) == original_body
        assert soup.find("figcaption") is None

    def test_image_only_page_keeps_publisher_layout(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<html><head></head><body class="class-0">'
            '<div class="class_sX-0"><img class="class_sX-1" src="image.jpg"/>'
            "</div></body></html>",
            "html.parser",
        )
        original = str(soup)

        inject_figcaption(soup, {}, "part0012.xhtml")
        assert str(soup) == original
        assert soup.find("img") is not None
        assert soup.find("svg") is None

    def test_each_export_gets_a_fresh_identifier(self, tmp_path):
        from ebooklib import epub

        source_path = tmp_path / "source.epub"
        source_path.write_bytes(b"same source")
        books = [epub.EpubBook(), epub.EpubBook()]

        for book in books:
            _configure_single_page_pagination(book, {}, source_path)

        namespace = "http://purl.org/dc/elements/1.1/"
        identifiers = [book.metadata[namespace]["identifier"][0][0] for book in books]
        assert identifiers[0] != identifiers[1]
        assert all(value.endswith(":single-page-v2") for value in identifiers)

    def test_svg_metadata_does_not_make_illustration_page_look_like_text(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html><body><svg>
              <title>Front illustration</title>
              <desc>Full-page artwork</desc>
              <image href="../images/front.jpg"/>
            </svg></body></html>
            """,
            "html.parser",
        )
        original_body = str(soup.body)

        inject_figcaption(
            soup,
            {"images/front.jpg": {"translated_text": "插图译文"}},
            "text/front.xhtml",
        )

        assert str(soup.body) == original_body
        assert soup.find("figcaption") is None

    def test_hidden_running_title_does_not_enable_caption_injection(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html><body>
              <span class="display-none">Illustration</span>
              <img src="../images/front.jpg"/>
            </body></html>
            """,
            "html.parser",
        )

        inject_figcaption(
            soup,
            {"images/front.jpg": {"translated_text": "插图译文"}},
            "text/front.xhtml",
        )

        assert soup.find("figcaption") is None

    def test_inline_illustration_can_still_receive_caption(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<html><body><p>正文</p><img src="../image/inner.jpg"/></body></html>',
            "html.parser",
        )
        inject_figcaption(
            soup,
            {"image/inner.jpg": {"translated_text": "图片译文"}},
            "Text/chapter.xhtml",
        )

        caption = soup.find("figcaption")
        assert caption is not None
        assert "图片译文" in caption.get_text()


# ── 8.2 图片容器索引：导入导出段落计数一致 ────────────


class TestImageContainerSegmentAlignment:
    """根因二：含图片的块不会改变后续段落映射顺序。"""

    def test_image_block_with_single_char_not_skipped(self, tmp_app_paths, tmp_path):
        """图片后带单字标题的块，导入和导出使用同一段落计数。

        见 docs/EPUB_EXPORT_MIXED_TEXT_AND_BLANK_PAGE_REPAIR.md 4.1：
        旧导出端对含图片的块执行"文本长度<2 跳过"，导入端没有同样规则，
        导致 global_line_index 偏移。
        """
        from ebooklib import epub

        from src.core.epub_processor import EPUBProcessor

        # 构造 EPUB：图片块 + 单字标题 + 正常段落
        # 图片块带单字文本（"<p><img src='x.jpg'/>图</p>"）
        # 导入端会为这个块生成一个 mapping（因为 get_text() 返回 "图"）
        # 旧导出端会跳过这个块（因为文本长度 < 2），导致后续段落错位
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\xff"
            b"\xff?\x00\x05\xfe\x02\xfe\xa3\x1f\x16\xb7\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        book = epub.EpubBook()
        book.set_identifier("img-align-test")
        book.set_title("对齐测试")
        book.set_language("ja")

        html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">\n'
            "<head><title>test</title></head>\n"
            "<body>\n"
            '<p><img src="../Images/i001.jpg"/>图</p>\n'
            "<p>这是正常段落。</p>\n"
            "</body>\n"
            "</html>"
        ).encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = html
        book.add_item(chapter)
        book.spine = [chapter]

        # 添加图片
        img_item = epub.EpubImage(
            uid="img_i001",
            file_name="Images/i001.jpg",
            media_type="image/jpeg",
            content=png_bytes,
        )
        book.add_item(img_item)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "img_align.epub"
        epub.write_epub(str(epub_path), book)

        proc = EPUBProcessor(app_paths=tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        # 导入端应为两个块都生成 mapping（图片块文本为"图"，正常段落文本为"这是正常段落。"）
        data = json.loads(
            (Path(result["mapping_dir"]) / "content_mapping.json").read_text(encoding="utf-8")
        )
        mappings = list(data["content_mappings"].values())
        mappings.sort(key=lambda x: x["line_number"])
        assert len(mappings) == 2
        assert mappings[0]["original_text"] == "图"
        assert mappings[1]["original_text"] == "这是正常段落。"

        # 设置译文
        for m in mappings:
            if m["original_text"] == "图":
                m["translated_text"] = "Image"
            else:
                m["translated_text"] = "这是正常段落的译文。"
            m["translated_at"] = "2026-01-01"
        _publish_content_mapping(result["mapping_dir"], data)

        # 导出应成功（不应因段落计数不一致而硬失败）
        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))

        # 验证译文正确写入对应段落（未被图片块错位）
        exported = epub.read_epub(str(output_path))
        for item in exported.items:
            if hasattr(item, "get_type") and item.get_type() == 9:
                content = item.get_content().decode("utf-8")
                # 正常段落的译文应存在
                if "这是正常段落的译文。" in content:
                    # 图片块的译文也应存在（"Image"）
                    # 但更重要的是：正常段落的译文没有被错位到图片块
                    return
        pytest.fail("正常段落的译文未写入，可能因图片块错位")

    def test_segment_count_mismatch_raises_hard_failure(self, tmp_app_paths, tmp_path):
        """段落计数不一致时抛出 SegmentCountMismatchError 硬失败。"""
        from ebooklib import epub

        from src.core.epub_processor import EPUBProcessor

        book = epub.EpubBook()
        book.set_identifier("mismatch-test")
        book.set_title("计数测试")
        book.set_language("ja")

        html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">\n'
            "<head><title>test</title></head>\n"
            "<body>\n"
            "<p>第一段。</p>\n"
            "<p>第二段。</p>\n"
            "</body>\n"
            "</html>"
        ).encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = html
        book.add_item(chapter)
        book.spine = [chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "mismatch.epub"
        epub.write_epub(str(epub_path), book)

        proc = EPUBProcessor(app_paths=tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        # 篡改 mapping：删除一个段落，使 mapping 数量与 DOM 不一致
        data = json.loads(
            (Path(result["mapping_dir"]) / "content_mapping.json").read_text(encoding="utf-8")
        )
        keys = list(data["content_mappings"].keys())
        del data["content_mappings"][keys[0]]
        # 重新编号剩余的 line_number 和 block_index 以通过其他校验
        for i, k in enumerate(data["content_mappings"].keys()):
            data["content_mappings"][k]["line_number"] = i + 1
            data["content_mappings"][k]["block_index"] = i
        _publish_content_mapping(result["mapping_dir"], data)

        output_path = tmp_path / "output.epub"
        # 应抛出 SegmentCountMismatchError，因为 DOM 有 2 段但 mapping 只有 1 段
        with pytest.raises(SegmentCountMismatchError) as exc_info:
            proc.export_epub(result["mapping_dir"], str(output_path))

        # 错误信息应包含章节 ID 和数量
        err_msg = str(exc_info.value)
        assert "Text/ch1.xhtml" in err_msg
        assert "期望" in err_msg
        assert "实际" in err_msg

    def test_image_block_with_punctuation_aligned(self, tmp_app_paths, tmp_path):
        """图片后带标点的块不会改变后续段落映射顺序。"""
        from ebooklib import epub

        from src.core.epub_processor import EPUBProcessor

        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\xff"
            b"\xff?\x00\x05\xfe\x02\xfe\xa3\x1f\x16\xb7\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        book = epub.EpubBook()
        book.set_identifier("punct-test")
        book.set_title("标点测试")
        book.set_language("ja")

        html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">\n'
            "<head><title>test</title></head>\n"
            "<body>\n"
            '<p><img src="../Images/i001.jpg"/>。</p>\n'
            "<p>正文内容。</p>\n"
            "</body>\n"
            "</html>"
        ).encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = html
        book.add_item(chapter)
        book.spine = [chapter]
        img_item = epub.EpubImage(
            uid="img_i001",
            file_name="Images/i001.jpg",
            media_type="image/jpeg",
            content=png_bytes,
        )
        book.add_item(img_item)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "punct.epub"
        epub.write_epub(str(epub_path), book)

        proc = EPUBProcessor(app_paths=tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        data = json.loads(
            (Path(result["mapping_dir"]) / "content_mapping.json").read_text(encoding="utf-8")
        )
        mappings = list(data["content_mappings"].values())
        mappings.sort(key=lambda x: x["line_number"])
        # 标点"。"是非空文本，导入端会生成一个 mapping
        assert len(mappings) == 2
        assert mappings[0]["original_text"] == "。"
        assert mappings[1]["original_text"] == "正文内容。"

        # 设置译文并导出，应成功
        for m in mappings:
            m["translated_text"] = "译文" + m["original_text"]
            m["translated_at"] = "2026-01-01"
        _publish_content_mapping(result["mapping_dir"], data)

        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))
        # 未抛出 SegmentCountMismatchError 即为成功
        assert output_path.exists()


# ── 8.1 端到端：导出后正文无原文残留 ──────────────────


class TestEndToEndNoResidualOriginal:
    """根因一端到端：导出的 EPUB 正文中不再出现"译文 + 同一段原文"的混排。"""

    def test_exported_epub_has_no_inline_residual(self, tmp_app_paths, tmp_path):
        """含 em 标签的段落在导出后，原文不残留。"""
        from ebooklib import epub

        from src.core.epub_processor import EPUBProcessor

        book = epub.EpubBook()
        book.set_identifier("residual-test")
        book.set_title("残留测试")
        book.set_language("ja")

        html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">\n'
            "<head><title>test</title></head>\n"
            "<body>\n"
            "<p>这是<em>华恋</em>的台词。</p>\n"
            "</body>\n"
            "</html>"
        ).encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = html
        book.add_item(chapter)
        book.spine = [chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "residual.epub"
        epub.write_epub(str(epub_path), book)

        proc = EPUBProcessor(app_paths=tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        # 设置译文（必须与原文不同，否则会被跳过替换）
        data = json.loads(
            (Path(result["mapping_dir"]) / "content_mapping.json").read_text(encoding="utf-8")
        )
        for m in data["content_mappings"].values():
            m["translated_text"] = "这是华恋的台词译文。"
            m["translated_at"] = "2026-01-01"
        _publish_content_mapping(result["mapping_dir"], data)

        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))

        # 读取导出 EPUB，扫描正文是否仍有原文残留
        exported = epub.read_epub(str(output_path))
        for item in exported.items:
            if hasattr(item, "get_type") and item.get_type() == 9:
                content = item.get_content().decode("utf-8")
                # em 标签应保留（空标签）
                assert re.search(r"<em[\s/>]", content), "em 标签应保留"
                # 关键断言：em 内部的原文"华恋"不应残留
                # 解析后用 BeautifulSoup 提取 em 标签的文本内容
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(content, "html.parser")
                em = soup.find("em")
                if em is not None:
                    em_text = em.get_text()
                    # em 内部文本应为空（短期实现清除所有后代文本）
                    assert em_text == "", f"em 标签内部应无文本残留，实际: {em_text!r}"
                # 译文应存在
                assert "这是华恋的台词译文。" in content
                return
        pytest.fail("未找到正文文档")
