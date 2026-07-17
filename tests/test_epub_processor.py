#!/usr/bin/env python3
"""
EPUB 处理器测试

验证 R2-BUG-001/004/005/006/007 的修复行为：
- R2-BUG-001：字符串 spine ID 解析
- R2-BUG-004：禁止按位置复用已变化原文的译文
- R2-BUG-005：章节 ID 保留唯一路径
- R2-BUG-006：导出前验证源 EPUB 指纹
- R2-BUG-007：保留 EPUB 内联格式

使用 conftest.py 中的 EPUB fixtures（simple_epub / string_spine_epub /
nested_dirs_epub），不依赖 Tkinter。
"""

import json
from pathlib import Path

import pytest

from src.core.epub_processor import EPUBProcessor
from src.domain.errors import EpubFingerprintMismatchError

# ── 辅助函数 ──────────────────────────────────────────


def _make_processor(tmp_app_paths) -> EPUBProcessor:
    """构造使用临时工作区的 EPUBProcessor"""
    return EPUBProcessor(app_paths=tmp_app_paths)


def _load_content_mapping(mapping_dir: Path) -> dict:
    """读取 content_mapping.json"""
    return json.loads((Path(mapping_dir) / "content_mapping.json").read_text(encoding="utf-8"))


# ── R2-BUG-001：字符串 spine ID 解析 ─────────────────


class TestStringSpineParsing:
    """R2-BUG-001：ebooklib 字符串 spine ID 无法解析"""

    def test_string_spine_produces_content(self, tmp_app_paths, string_spine_epub):
        """字符串 spine 能按阅读顺序产生正文，不为空"""
        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(string_spine_epub), extract_images=False)

        data = _load_content_mapping(result["mapping_dir"])
        mappings = data["content_mappings"]
        assert len(mappings) > 0, "字符串 spine 未解析出任何正文"

        # 验证两个章节都被解析
        chapter_ids = {m["chapter_id"] for m in mappings.values()}
        assert len(chapter_ids) == 2

    def test_string_and_object_spine_produce_same_chapters(
        self, tmp_app_paths, simple_epub, string_spine_epub
    ):
        """对象 spine 和字符串 spine 得到相同的章节集合"""
        proc = _make_processor(tmp_app_paths)

        result_obj = proc.import_epub(str(simple_epub), extract_images=False)
        data_obj = _load_content_mapping(result_obj["mapping_dir"])
        chapters_obj = {m["chapter_id"] for m in data_obj["content_mappings"].values()}

        # 用新的工作区子目录避免冲突
        proc2 = EPUBProcessor(app_paths=tmp_app_paths)
        result_str = proc2.import_epub(str(string_spine_epub), extract_images=False)
        data_str = _load_content_mapping(result_str["mapping_dir"])
        chapters_str = {m["chapter_id"] for m in data_str["content_mappings"].values()}

        # 去重后的章节 ID 集合应一致（两个 fixture 的章节文件名相同，
        # 但段数不同：simple_epub 每章 2 段，string_spine_epub 每章 1 段）
        assert chapters_obj == chapters_str

    def test_empty_spine_with_no_content_raises(self, tmp_app_paths, make_epub):
        """spine 非空但无法解析出任何正文时抛异常"""
        # 构造一个 spine 指向不存在 id 的 EPUB
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("bad-spine-id")
        book.set_title("空 spine")
        book.set_language("ja")
        # spine 指向不存在的 id
        book.spine = [("nonexistent_id", "yes")]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        bad_epub_path = Path(tmp_app_paths.workspace_dir) / "bad.epub"
        bad_epub_path.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(bad_epub_path), book)

        proc = _make_processor(tmp_app_paths)
        with pytest.raises(Exception, match="spine 非空但未解析出任何正文"):
            proc.import_epub(str(bad_epub_path), extract_images=False)


# ── R2-BUG-005：章节 ID 保留唯一路径 ─────────────────


class TestChapterIdUniqueness:
    """R2-BUG-005：章节 ID 归一化丢失目录层级"""

    def test_nested_dirs_have_different_ids(self, tmp_app_paths, nested_dirs_epub):
        """不同目录下的同名章节拥有不同 ID"""
        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(nested_dirs_epub), extract_images=False)

        data = _load_content_mapping(result["mapping_dir"])
        chapter_ids = {m["chapter_id"] for m in data["content_mappings"].values()}
        # nested_dirs_epub 有两个章节位于 part1/ 和 part2/
        assert len(chapter_ids) == 2
        # 两个 ID 应该不同（包含目录路径）
        chapter_list = list(chapter_ids)
        assert chapter_list[0] != chapter_list[1]

    def test_normalize_chapter_id_preserves_directory(self):
        """_normalize_chapter_id 保留目录层级"""
        # 移除容器前缀但保留子目录
        assert EPUBProcessor._normalize_chapter_id("OEBPS/Text/ch1.xhtml") == "Text/ch1.xhtml"
        assert EPUBProcessor._normalize_chapter_id("OEBPS/part1/ch1.xhtml") == "part1/ch1.xhtml"
        assert EPUBProcessor._normalize_chapter_id("part2/ch1.xhtml") == "part2/ch1.xhtml"

    def test_normalize_chapter_id_path_separator(self):
        """Windows 和 POSIX 分隔符归一化后相同"""
        assert EPUBProcessor._normalize_chapter_id("part1\\ch1.xhtml") == "part1/ch1.xhtml"
        assert EPUBProcessor._normalize_chapter_id("part1/ch1.xhtml") == "part1/ch1.xhtml"

    def test_conflicting_chapter_ids_rejected(self, tmp_app_paths, make_epub):
        """检测到规范化 ID 冲突时拒绝生成映射"""
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("conflict-id")
        book.set_title("冲突测试")
        book.set_language("ja")

        # 两个不同路径但归一化后会冲突的章节
        # _normalize_chapter_id 会移除 OEBPS/ 前缀
        ch1 = epub.EpubHtml(title="ch1", file_name="OEBPS/Text/ch1.xhtml", lang="ja")
        ch1.content = b"<html><body><p>content1</p></body></html>"
        ch2 = epub.EpubHtml(title="ch2", file_name="Text/ch1.xhtml", lang="ja")
        ch2.content = b"<html><body><p>content2</p></body></html>"
        book.add_item(ch1)
        book.add_item(ch2)
        book.spine = [ch1, ch2]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (ch1, ch2)

        conflict_epub = Path(tmp_app_paths.workspace_dir) / "conflict.epub"
        conflict_epub.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(conflict_epub), book)

        proc = _make_processor(tmp_app_paths)
        with pytest.raises(Exception, match="章节 ID 冲突"):
            proc.import_epub(str(conflict_epub), extract_images=False)


# ── R2-BUG-004：禁止按位置复用已变化原文的译文 ────────


class TestPositionalMatchValidation:
    """R2-BUG-004：位置降级匹配不校验原文"""

    def test_changed_source_not_reused(self, tmp_app_paths, simple_epub):
        """修改原文后不会自动复用旧译文"""
        proc = _make_processor(tmp_app_paths)

        # 第一次导入并保存译文
        result1 = proc.import_epub(str(simple_epub), extract_images=False)
        data1 = _load_content_mapping(result1["mapping_dir"])
        mappings1 = data1["content_mappings"]

        # 给第一个段落写入译文
        first_key = sorted(mappings1.keys(), key=lambda k: mappings1[k]["line_number"])[0]
        mappings1[first_key]["translated_text"] = "这是测试译文"
        mappings1[first_key]["translated_at"] = "2026-01-01T00:00:00"
        # 写回时保留 source_checksum 为旧原文的校验和
        (Path(result1["mapping_dir"]) / "content_mapping.json").write_text(
            json.dumps(data1, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 第二次导入：修改第一个段落的原文内容
        # 构造一个修改了原文的 EPUB
        from ebooklib import epub

        book = epub.read_epub(str(simple_epub))
        for item in book.items:
            if hasattr(item, "get_type") and item.get_type() == 9:  # ITEM_DOCUMENT
                content = item.get_content().decode("utf-8")
                if "これはテストです" in content:
                    content = content.replace("これはテストです。", "これは変更された原文です。")
                    item.set_content(content.encode("utf-8"))
                    break

        modified_epub = Path(tmp_app_paths.workspace_dir) / "modified.epub"
        epub.write_epub(str(modified_epub), book)

        # 用同一 mapping_dir 重新导入（保留旧译文）
        proc2 = _make_processor(tmp_app_paths)
        result2 = proc2.import_epub(str(modified_epub), extract_images=False)
        data2 = _load_content_mapping(result2["mapping_dir"])
        mappings2 = data2["content_mappings"]

        # 找到修改后的段落（原文已变化）
        # 其译文应为空（位置降级匹配校验失败）
        changed_found = False
        for m in mappings2.values():
            if "変更された原文" in m.get("original_text", ""):
                # 原文变化后不应复用旧译文
                assert m["translated_text"] == "", f"原文变化后仍复用旧译文: {m['translated_text']}"
                changed_found = True
                break
        assert changed_found, "未找到修改后的段落"

    def test_unchanged_source_reused_by_locator(self, tmp_app_paths, simple_epub):
        """原文未变化时通过 locator 恢复旧译文"""
        proc = _make_processor(tmp_app_paths)

        # 第一次导入并保存译文
        result1 = proc.import_epub(str(simple_epub), extract_images=False)
        data1 = _load_content_mapping(result1["mapping_dir"])
        mappings1 = data1["content_mappings"]

        # 写入译文
        first_key = sorted(mappings1.keys(), key=lambda k: mappings1[k]["line_number"])[0]
        mappings1[first_key]["translated_text"] = "测试译文保留"
        mappings1[first_key]["translated_at"] = "2026-01-01T00:00:00"
        (Path(result1["mapping_dir"]) / "content_mapping.json").write_text(
            json.dumps(data1, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 第二次导入同一文件（原文未变化）
        proc2 = _make_processor(tmp_app_paths)
        result2 = proc2.import_epub(str(simple_epub), extract_images=False)
        data2 = _load_content_mapping(result2["mapping_dir"])
        mappings2 = data2["content_mappings"]

        # 第一段译文应被保留
        first_item = min(mappings2.values(), key=lambda x: x["line_number"])
        assert first_item["translated_text"] == "测试译文保留"


# ── R2-BUG-006：导出前验证源 EPUB 指纹 ────────────────


class TestExportFingerprint:
    """R2-BUG-006：导出未验证 EPUB 源文件是否变化"""

    def test_modified_source_rejected(self, tmp_app_paths, simple_epub, tmp_path):
        """修改源 EPUB 后旧映射不能直接导出"""
        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(simple_epub), extract_images=False)

        # 修改源 EPUB 文件内容
        original_data = simple_epub.read_bytes()
        modified_data = original_data + b"\x00" * 100  # 追加字节改变内容
        modified_path = tmp_path / "modified_source.epub"
        modified_path.write_bytes(modified_data)

        # 更新 mapping 中的 original_file 指向修改后的文件
        data = _load_content_mapping(result["mapping_dir"])
        data["project_info"]["original_file"] = str(modified_path)
        (Path(result["mapping_dir"]) / "content_mapping.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 导出应抛出 EpubFingerprintMismatchError
        output_path = tmp_path / "output.epub"
        with pytest.raises(EpubFingerprintMismatchError):
            proc.export_epub(result["mapping_dir"], str(output_path))

    def test_unchanged_source_allowed(self, tmp_app_paths, simple_epub, tmp_path):
        """源 EPUB 未变化时可正常导出"""
        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(simple_epub), extract_images=False)

        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))
        assert output_path.exists()

    def test_content_hash_stored_on_import(self, tmp_app_paths, simple_epub):
        """导入时保存 source_content_hash"""
        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(simple_epub), extract_images=False)

        data = _load_content_mapping(result["mapping_dir"])
        stored_hash = data["project_info"].get("source_content_hash")
        assert stored_hash
        assert len(stored_hash) == 64  # SHA256 hex 长度


# ── R2-BUG-007：保留 EPUB 内联格式 ───────────────────


class TestInlineFormatPreservation:
    """R2-BUG-007：导出删除全部内联文本节点"""

    def test_em_tag_preserved(self, tmp_app_paths, tmp_path):
        """含 em 强调标签的段落导出后标签保留"""
        import re

        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("inline-test")
        book.set_title("内联格式测试")
        book.set_language("ja")

        html_content = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">
<head><title>test</title></head>
<body>
<p>这是<em>强调</em>内容</p>
</body>
</html>""".encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = html_content
        book.add_item(chapter)
        book.spine = [chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "inline.epub"
        epub.write_epub(str(epub_path), book)

        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        # 写入译文
        data = _load_content_mapping(result["mapping_dir"])
        for _key, m in data["content_mappings"].items():
            m["translated_text"] = "translated text"
            m["translated_at"] = "2026-01-01"
        (Path(result["mapping_dir"]) / "content_mapping.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 导出
        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))

        # 读取导出后的 EPUB，检查 em 标签保留
        # EPUB 导出修复（根因一）：清除所有后代原文文本节点，保留内联标签
        exported_book = epub.read_epub(str(output_path))
        for item in exported_book.items:
            if hasattr(item, "get_type") and item.get_type() == 9:  # ITEM_DOCUMENT
                content = item.get_content().decode("utf-8")
                # 匹配 <em> / <em/> / <em ...> 等多种序列化格式
                if re.search(r"<em[\s/>]", content):
                    # em 标签保留（可能为空 <em/> 或 <em></em>）
                    assert "<em" in content
                    # 修复关键断言：原文"强调"必须被清除，不能残留在 em 标签内
                    # 这是 EPUB_EXPORT_MIXED_TEXT_AND_BLANK_PAGE_REPAIR.md 根因一的核心要求
                    assert "强调" not in content, "em 标签内的原文未清除，译文混入原文"
                    # 译文应存在于输出中
                    assert "translated text" in content
                    return
        pytest.fail("导出后未找到 em 标签")

    def test_link_target_preserved(self, tmp_app_paths, tmp_path):
        """含超链接的段落导出后 href 保留"""
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("link-test")
        book.set_title("链接测试")
        book.set_language("ja")

        html_content = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">
<head><title>test</title></head>
<body>
<p>请访问<a href="https://example.com">链接</a>查看详情</p>
</body>
</html>""".encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = html_content
        book.add_item(chapter)
        book.spine = [chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "link.epub"
        epub.write_epub(str(epub_path), book)

        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        # 写入译文
        data = _load_content_mapping(result["mapping_dir"])
        for _key, m in data["content_mappings"].items():
            m["translated_text"] = "translated content"
            m["translated_at"] = "2026-01-01"
        (Path(result["mapping_dir"]) / "content_mapping.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 导出
        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))

        # 读取导出后的 EPUB，检查 href 保留
        exported_book = epub.read_epub(str(output_path))
        for item in exported_book.items:
            if hasattr(item, "get_type") and item.get_type() == 9:
                content = item.get_content().decode("utf-8")
                if "href" in content:
                    assert "https://example.com" in content
                    return
        pytest.fail("导出后未找到链接 href")

    def test_untranslated_paragraph_unchanged(self, tmp_app_paths, tmp_path):
        """未翻译段落的 HTML 不发生无关变化"""
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("untranslated-test")
        book.set_title("未翻译测试")
        book.set_language("ja")

        original_html = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">
<head><title>test</title></head>
<body>
<p>这段没有译文</p>
</body>
</html>""".encode()
        chapter = epub.EpubHtml(title="ch1", file_name="Text/ch1.xhtml", lang="ja")
        chapter.content = original_html
        book.add_item(chapter)
        book.spine = [chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (chapter,)

        epub_path = tmp_path / "untranslated.epub"
        epub.write_epub(str(epub_path), book)

        proc = _make_processor(tmp_app_paths)
        result = proc.import_epub(str(epub_path), extract_images=False)

        # 不写入任何译文，直接导出
        output_path = tmp_path / "output.epub"
        proc.export_epub(result["mapping_dir"], str(output_path))

        # 导出后原文应保留
        exported_book = epub.read_epub(str(output_path))
        for item in exported_book.items:
            if hasattr(item, "get_type") and item.get_type() == 9:
                content = item.get_content().decode("utf-8")
                if "这段没有译文" in content:
                    return  # 原文保留
        pytest.fail("未翻译段落的原文丢失")
