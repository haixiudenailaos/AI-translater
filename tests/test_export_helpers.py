#!/usr/bin/env python3
"""P2-6：导出助手纯函数测试。

验证从 ``translation_controller.py`` 抽取的导出辅助函数：
- ``load_image_translation_result`` 新旧格式兼容
- ``load_image_text_translations`` 文件缺失/解析失败
- ``build_default_epub_filename`` 源路径 None/有值

这些函数原本内联在 ``export_epub_file`` 中，无法在不启动 Tk 的情况下测试。
抽取后可在纯 Python 环境验证 R2-BUG-018 的新旧格式兼容性。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.ui.export_helpers import (
    build_default_bilingual_epub_filename,
    build_default_epub_filename,
    is_forbidden_bilingual_output,
    load_image_text_translations,
    load_image_translation_result,
)


class LoadImageTranslationResultTests(unittest.TestCase):
    """``load_image_translation_result`` 新旧格式兼容性。"""

    def test_missing_file_returns_none(self):
        with TemporaryDirectory() as tmp:
            result = load_image_translation_result(Path(tmp))
            self.assertIsNone(result)

    def test_new_format_returns_result_map(self):
        """新格式 ``{"result_map": {...}, "run_at": ...}`` 应返回 result_map。"""
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            data = {
                "result_map": {"images/ch1.png": "assets/images/ch1_zh.png"},
                "run_at": "2026-07-17T10:00:00",
                "result_count": 1,
            }
            (mapping_dir / "image_translation_result.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            result = load_image_translation_result(mapping_dir)
            self.assertEqual(result, {"images/ch1.png": "assets/images/ch1_zh.png"})

    def test_old_format_returns_raw_dict(self):
        """旧格式 ``{original_path: new_filename}`` 应直接返回。"""
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            data = {"images/ch1.png": "assets/images/ch1_zh.png"}
            (mapping_dir / "image_translation_result.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            result = load_image_translation_result(mapping_dir)
            self.assertEqual(result, {"images/ch1.png": "assets/images/ch1_zh.png"})

    def test_invalid_json_returns_none(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            (mapping_dir / "image_translation_result.json").write_text(
                "not valid json {", encoding="utf-8"
            )
            result = load_image_translation_result(mapping_dir)
            self.assertIsNone(result)

    def test_non_dict_json_returns_none(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            (mapping_dir / "image_translation_result.json").write_text(
                json.dumps(["list", "not", "dict"]), encoding="utf-8"
            )
            result = load_image_translation_result(mapping_dir)
            self.assertIsNone(result)

    def test_new_format_with_non_dict_result_map_returns_none(self):
        """新格式但 result_map 不是 dict 时返回 None（防御）。"""
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            data = {"result_map": ["not", "a", "dict"]}
            (mapping_dir / "image_translation_result.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            result = load_image_translation_result(mapping_dir)
            self.assertIsNone(result)

    def test_values_coerced_to_str(self):
        """非字符串键值应被强制转为字符串（JSON 可能含 int 键）。"""
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            data = {"1": "a.png"}  # JSON 键总是字符串
            (mapping_dir / "image_translation_result.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            result = load_image_translation_result(mapping_dir)
            self.assertEqual(result, {"1": "a.png"})
            # 所有键值都是 str
            for k, v in result.items():
                self.assertIsInstance(k, str)
                self.assertIsInstance(v, str)


class LoadImageTextTranslationsTests(unittest.TestCase):
    """``load_image_text_translations`` 文件加载。"""

    def test_missing_file_returns_none(self):
        with TemporaryDirectory() as tmp:
            result = load_image_text_translations(Path(tmp))
            self.assertIsNone(result)

    def test_valid_dict_returns_dict(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            data = {"images/ch1.png": "图片文字译文"}
            (mapping_dir / "image_text_translations.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            result = load_image_text_translations(mapping_dir)
            self.assertEqual(result, {"images/ch1.png": "图片文字译文"})

    def test_invalid_json_returns_none(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            (mapping_dir / "image_text_translations.json").write_text(
                "broken json {", encoding="utf-8"
            )
            result = load_image_text_translations(mapping_dir)
            self.assertIsNone(result)

    def test_non_dict_json_returns_none(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp)
            (mapping_dir / "image_text_translations.json").write_text(
                json.dumps([1, 2, 3]), encoding="utf-8"
            )
            result = load_image_text_translations(mapping_dir)
            self.assertIsNone(result)


class BuildDefaultEpubFilenameTests(unittest.TestCase):
    """``build_default_epub_filename`` 文件名生成。"""

    def test_none_returns_empty(self):
        self.assertEqual(build_default_epub_filename(None), "")

    def test_simple_path(self):
        path = Path("novel.epub")
        self.assertEqual(build_default_epub_filename(path), "novel_译文.epub")

    def test_path_with_directory(self):
        path = Path("/some/dir/novel.epub")
        self.assertEqual(build_default_epub_filename(path), "novel_译文.epub")

    def test_path_without_extension(self):
        path = Path("novel")
        self.assertEqual(build_default_epub_filename(path), "novel_译文.epub")


class BuildDefaultBilingualEpubFilenameTests(unittest.TestCase):
    """``build_default_bilingual_epub_filename`` 保持与纯译文文件名不同。"""

    def test_none_returns_empty(self):
        self.assertEqual(build_default_bilingual_epub_filename(None), "")

    def test_uses_bilingual_suffix(self):
        self.assertEqual(
            build_default_bilingual_epub_filename(Path("novel.epub")),
            "novel_双语对照.epub",
        )


class ForbiddenBilingualOutputTests(unittest.TestCase):
    """双语导出目标路径保护。"""

    def test_source_epub_is_forbidden(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            source.write_bytes(b"epub")
            message = is_forbidden_bilingual_output(
                source, source_path=source, target_path=None, mapping_dir=None
            )
            self.assertIsNotNone(message)
            self.assertIn("原 EPUB", message)

    def test_session_target_is_forbidden(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "book_译文.epub"
            message = is_forbidden_bilingual_output(
                target, source_path=None, target_path=target, mapping_dir=None
            )
            self.assertIsNotNone(message)

    def test_mapping_directory_is_forbidden(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp) / "mapping"
            mapping_dir.mkdir()
            output = mapping_dir / "out.epub"
            message = is_forbidden_bilingual_output(
                output, source_path=None, target_path=None, mapping_dir=mapping_dir
            )
            self.assertIsNotNone(message)
            self.assertIn("映射目录", message)

    def test_mapping_subdirectory_is_forbidden(self):
        with TemporaryDirectory() as tmp:
            mapping_dir = Path(tmp) / "mapping"
            (mapping_dir / "nested").mkdir(parents=True)
            output = mapping_dir / "nested" / "out.epub"
            self.assertIsNotNone(
                is_forbidden_bilingual_output(
                    output, source_path=None, target_path=None, mapping_dir=mapping_dir
                )
            )

    def test_unrelated_output_is_allowed(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            source.write_bytes(b"epub")
            mapping_dir = Path(tmp) / "mapping"
            mapping_dir.mkdir()
            output = Path(tmp) / "exports" / "book_双语对照.epub"
            self.assertIsNone(
                is_forbidden_bilingual_output(
                    output,
                    source_path=source,
                    target_path=None,
                    mapping_dir=mapping_dir,
                )
            )

    def test_case_variant_source_path_is_forbidden_on_case_insensitive_platforms(self):
        if os.path.normcase("A") != os.path.normcase("a"):
            self.skipTest("platform path normalization is case-sensitive")
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "Book.epub"
            source.write_bytes(b"epub")
            alias = Path(tmp) / "book.epub"
            message = is_forbidden_bilingual_output(
                alias, source_path=source, target_path=None, mapping_dir=None
            )
            self.assertIsNotNone(message)

    def test_existing_hard_link_alias_is_forbidden(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            source.write_bytes(b"epub")
            alias = Path(tmp) / "alias.epub"
            try:
                os.link(source, alias)
            except OSError:
                self.skipTest("hard links are not available")
            message = is_forbidden_bilingual_output(
                alias, source_path=source, target_path=None, mapping_dir=None
            )
            self.assertIsNotNone(message)


if __name__ == "__main__":
    unittest.main()
