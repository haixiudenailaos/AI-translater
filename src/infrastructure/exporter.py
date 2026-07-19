#!/usr/bin/env python3
"""
EPUB 导出协调模块

协调 archive reader、segment locator 和 image rewriter，最终写出 EPUB。

阶段 4（EPUB 拆分）从 epub_processor.py 提取。

职责：
- export_epub：根据映射目录重建并导出 EPUB
  1. 加载 content_mapping.json
  2. 验证源 EPUB 指纹（R2-BUG-006）
  3. 处理图片替换（委托 image_rewriter）
  4. 按 spine 顺序替换正文（使用与导入一致的遍历规则）
  5. 注入 figcaption（委托 image_rewriter）
  6. 写出 EPUB
"""

import hashlib
import json
import os
import re
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from typing import Dict
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from ..domain.errors import EpubFingerprintMismatchError
from ..utils.logger import get_logger
from .document_order import get_item_name, iter_spine_documents, normalize_chapter_id
from .image_rewriter import (
    add_translated_images,
    get_document_content,
    inject_figcaption,
    match_and_get_new_path,
    rewrite_image_references,
    set_document_content,
)
from .segment_extractor import (
    BLOCK_TAGS,
    compute_source_checksum,
    is_leaf_block,
)

logger = get_logger(__name__)


class SegmentCountMismatchError(Exception):
    """Raised when the source DOM cannot be safely aligned with its mapping."""


def compute_file_hash(path: Path) -> str:
    """计算文件内容的 SHA256 哈希（R2-BUG-006）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def export_epub(
    mapping_dir: str,
    output_path: str,
    image_map: Dict[str, str] | None = None,
    image_text_map: Dict[str, Dict] | None = None,
) -> str:
    """根据 mapping 重建并导出 EPUB。

    改进说明：
    1. 严格按 line_number 全局顺序读取译文
    2. 确保第 N 行译文替换第 N 个原文段落
    3. 没有译文的段落保留原文
    4. 修复图片前后的多余空白问题
    5. 修正特殊格式标题识别问题
    6. 支持插图翻译替换
    7. 支持图片文字翻译注释（figcaption）

    Args:
        mapping_dir: 映射目录路径
        output_path: 输出 EPUB 文件路径
        image_map: {original_epub_path: local_new_filename} 图片替换映射
        image_text_map: {image_path: {translated_text, original_text}} 图片文字翻译

    Returns:
        输出文件路径

    Raises:
        EpubFingerprintMismatchError: 源 EPUB 内容哈希不匹配
        Exception: 缺少必要文件或依赖
    """
    try:
        from bs4 import BeautifulSoup
        from ebooklib import epub
    except ImportError:
        raise Exception("需要安装ebooklib和beautifulsoup4库来支持EPUB导出")

    mapping_dir_p = Path(mapping_dir)
    content_file = mapping_dir_p / "content_mapping.json"
    if not content_file.exists():
        raise Exception("缺少content_mapping.json，无法导出EPUB")

    content_obj = json.loads(content_file.read_text(encoding="utf-8"))
    items = content_obj.get("content_mappings", {})
    project_info = content_obj.get("project_info", {})
    original_file = project_info.get("original_file")
    if not original_file or not Path(original_file).exists():
        raise Exception("original_file不存在，无法基于原结构导出EPUB")

    # R2-BUG-006：导出前验证源 EPUB 内容哈希
    _verify_source_fingerprint(project_info, Path(original_file))

    # 加载原书以保留结构
    book = epub.read_epub(str(original_file))
    _configure_single_page_pagination(book, project_info, Path(original_file))

    # PERF-007：收集 spine 文档列表，避免多次遍历生成器
    spine_docs = list(iter_spine_documents(book))
    spine_names = {get_item_name(item) for item in spine_docs}

    # 处理图片替换
    path_mapping: Dict[str, str] = {}
    if image_map:
        print(f"正在处理 {len(image_map)} 张图片的替换...")
        path_mapping, failed_image_count = add_translated_images(book, image_map, mapping_dir_p)
        if failed_image_count:
            logger.warning(
                "有 %s 张翻译图片校验或添加失败，已保留原图",
                failed_image_count,
            )
        if path_mapping:
            # PERF-007：非 spine 文档单独处理图片引用，spine 文档在导出循环中合并处理
            rewrite_image_references(book, path_mapping, skip_names=spine_names)

    sorted_records = [
        value
        for _key, value in sorted(
            items.items(), key=lambda entry: entry[1].get("line_number", 999999)
        )
    ]
    parsed_docs = []
    for item in spine_docs:
        doc_name = get_item_name(item)
        html = get_document_content(item).decode("utf-8", errors="ignore")
        mapping_soup = BeautifulSoup(html, "html.parser")
        soup = BeautifulSoup(html, "xml")
        mapping_blocks = _collect_text_blocks(mapping_soup)
        output_blocks = _collect_text_blocks(soup)
        if len(mapping_blocks) != len(output_blocks):
            raise SegmentCountMismatchError(
                f"章节 {normalize_chapter_id(doc_name)} 的 HTML/XML 段落结构不一致："
                f"定位 {len(mapping_blocks)}，输出 {len(output_blocks)}"
            )
        parsed_docs.append((item, doc_name, soup, mapping_blocks, output_blocks))

    use_locators = bool(sorted_records) and all(
        record.get("chapter_id")
        and record.get("block_index") is not None
        and record.get("source_checksum")
        for record in sorted_records
    )
    if use_locators:
        records_by_chapter = _index_records_by_chapter(sorted_records)
        consumed_records = 0
    else:
        records_by_chapter = _index_records_by_chapter(sorted_records)
        consumed_legacy_chapters: set[str] = set()

    # PERF-007：单次 DOM 解析，合并正文替换、图片引用替换、figcaption 和空白清理
    for item, doc_name, soup, mapping_blocks, output_blocks in parsed_docs:
        if use_locators:
            chapter_id = normalize_chapter_id(doc_name)
            chapter_records = records_by_chapter.get(chapter_id, [])
            _validate_chapter_records(chapter_id, mapping_blocks, chapter_records)
            records = chapter_records
            consumed_records += len(records)
        else:
            record_chapter, records = _find_legacy_chapter_records(doc_name, records_by_chapter)
            _validate_legacy_records(doc_name, mapping_blocks, records)
            if record_chapter:
                consumed_legacy_chapters.add(record_chapter)

        for node, record in zip(output_blocks, records, strict=False):
            translation = str(record.get("translated_text", "")).strip()
            original = str(record.get("original_text", "")).strip()
            if translation and translation != original:
                _replace_text_preserving_inline(soup, node, translation)

        # 注释必须在图片 src 改写前匹配原始 EPUB 路径。
        if image_text_map:
            inject_figcaption(soup, image_text_map, doc_name)

        # PERF-007：图片引用替换（合并到同一 DOM 解析，避免双重解析）
        if path_mapping:
            doc_dir = Path(doc_name).parent
            for img in soup.find_all("img"):
                src = img.get("src")
                if src:
                    matched, new_rel_path = match_and_get_new_path(src, path_mapping, doc_dir)
                    if matched:
                        img["src"] = new_rel_path
            for image_tag in soup.find_all("image"):
                href = image_tag.get("xlink:href") or image_tag.get("href")
                if href:
                    matched, new_rel_path = match_and_get_new_path(href, path_mapping, doc_dir)
                    if matched:
                        attribute = "xlink:href" if image_tag.has_attr("xlink:href") else "href"
                        image_tag[attribute] = new_rel_path

        set_document_content(item, str(soup).encode("utf-8"))

    if use_locators and consumed_records != len(sorted_records):
        raise SegmentCountMismatchError(
            "映射中存在无法定位到当前 spine 的段落："
            f"期望 {len(sorted_records)}，实际 {consumed_records}"
        )
    if not use_locators:
        unmatched_translations = [
            record
            for chapter_id, records in records_by_chapter.items()
            if chapter_id not in consumed_legacy_chapters
            for record in records
            if str(record.get("translated_text", "")).strip()
            and str(record.get("translated_text", "")).strip()
            != str(record.get("original_text", "")).strip()
        ]
        if unmatched_translations:
            raise SegmentCountMismatchError(
                f"旧格式映射中有 {len(unmatched_translations)} 条有效译文无法定位"
            )

    # 写出 EPUB
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_toc_link_ids(book, epub)
    epub.write_epub(str(out_path), book)
    _enforce_single_page_spine(out_path)
    return str(out_path)


def _configure_single_page_pagination(
    book,
    project_info: dict,
    source_path: Path,
) -> None:
    """Disable synthetic spreads and separate exports from source-book caches."""
    source_hash = str(project_info.get("source_content_hash") or "")
    if not source_hash:
        source_hash = compute_file_hash(source_path)
    translated_identifier = f"urn:ai-translater:{source_hash[:16]}:{uuid4().hex}:single-page-v2"

    dc_namespace = "http://purl.org/dc/elements/1.1/"
    dc_metadata = book.metadata.setdefault(dc_namespace, {})
    dc_metadata["identifier"] = [(translated_identifier, {"id": "bookid"})]

    book.add_metadata(
        "OPF",
        None,
        "none",
        {"property": "rendition:spread"},
    )
    book.add_metadata(
        "OPF",
        None,
        "reflowable",
        {"property": "rendition:layout"},
    )


def _enforce_single_page_spine(epub_path: Path) -> None:
    """Add itemref overrides readers use when package spread metadata is ignored."""
    temporary_path: Path | None = None
    try:
        with ZipFile(epub_path, "r") as source:
            container = ElementTree.fromstring(source.read("META-INF/container.xml"))
            rootfile = container.find("{*}rootfiles/{*}rootfile")
            if rootfile is None or not rootfile.get("full-path"):
                raise ValueError("EPUB container.xml 缺少 rootfile")
            package_path = str(rootfile.get("full-path"))
            package_text = source.read(package_path).decode("utf-8")
            updated_package = re.sub(
                r"<itemref\b(?P<attrs>[^>]*?)(?P<slash>/?)>",
                _add_itemref_pagination_properties,
                package_text,
            ).encode("utf-8")

            with NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=epub_path.parent,
                prefix=f".{epub_path.stem}.",
                suffix=".tmp",
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)

            with ZipFile(temporary_path, "w") as destination:
                destination.comment = source.comment
                for info in source.infolist():
                    if info.filename == package_path:
                        destination.writestr(info, updated_package)
                        continue
                    with source.open(info, "r") as source_entry:
                        with destination.open(info, "w") as destination_entry:
                            copyfileobj(source_entry, destination_entry, length=1024 * 1024)
        os.replace(temporary_path, epub_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _ensure_toc_link_ids(book, epub_module) -> None:
    """Give deserialized TOC links stable NCX identifiers before writing.

    ebooklib reads a NAV document into ``Link`` objects without their NCX-only
    ``uid`` field.  Its writer requires that field, so a read-modify-write
    export otherwise fails while rebuilding the NCX table of contents.
    """

    used_ids: set[str] = set()

    def visit(items, path: tuple[int, ...] = ()) -> None:
        for index, item in enumerate(items):
            item_path = (*path, index)
            if isinstance(item, (tuple, list)):
                if item:
                    visit((item[0],), (*item_path, 0))
                if len(item) > 1:
                    visit(item[1], (*item_path, 1))
                continue
            if not isinstance(item, epub_module.Link):
                continue
            existing = getattr(item, "uid", None)
            if isinstance(existing, str) and existing.strip() and existing not in used_ids:
                used_ids.add(existing)
                continue
            fingerprint = f"{item.href}\n{item.title}\n{item_path}"
            candidate = f"nav_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]}"
            suffix = 1
            while candidate in used_ids:
                suffix += 1
                candidate = (
                    f"nav_{hashlib.sha256(f'{fingerprint}:{suffix}'.encode()).hexdigest()[:16]}"
                )
            item.uid = candidate
            used_ids.add(candidate)

    visit(book.toc)


def _add_itemref_pagination_properties(match) -> str:
    attrs = match.group("attrs")
    slash = match.group("slash")
    required = ["rendition:spread-none"]

    properties_match = re.search(
        r'\bproperties\s*=\s*(["\'])(.*?)\1',
        attrs,
    )
    if properties_match:
        properties = properties_match.group(2).split()
        for value in required:
            if value not in properties:
                properties.append(value)
        quote = properties_match.group(1)
        replacement = f"properties={quote}{' '.join(properties)}{quote}"
        attrs = attrs[: properties_match.start()] + replacement + attrs[properties_match.end() :]
    else:
        attrs = f'{attrs} properties="{" ".join(required)}"'
    return f"<itemref{attrs}{slash}>"


def _verify_source_fingerprint(project_info: dict, source_path: Path) -> None:
    """验证源 EPUB 文件指纹（R2-BUG-006）。

    优先使用内容哈希校验，旧数据无哈希时回退到 size+mtime。
    """
    stored_hash = project_info.get("source_content_hash")
    if stored_hash:
        current_hash = compute_file_hash(source_path)
        if current_hash != stored_hash:
            raise EpubFingerprintMismatchError(
                expected=stored_hash,
                actual=current_hash,
            )
    else:
        # 旧数据无内容哈希：回退到 size+mtime 校验
        stored_size = project_info.get("source_file_size")
        stored_mtime = project_info.get("source_file_mtime")
        src_stat = source_path.stat()
        if stored_size and stored_size != src_stat.st_size:
            logger.warning(
                "源 EPUB 大小已变化（旧=%s, 新=%s），无内容哈希无法精确校验，导出可能产生位置错位",
                stored_size,
                src_stat.st_size,
            )
        elif stored_mtime and stored_mtime != src_stat.st_mtime:
            logger.info("源 EPUB 修改时间已变化但大小一致，继续导出")


def _replace_text_preserving_inline(soup, node, translation: str) -> None:
    """R2-BUG-007：保留内联格式替换文本。

    替换全部后代文本节点，保留 em/strong/a/ruby/span 等内联标签及其属性。
    译文放在第一个文本节点位置，其余后代文本节点清除。
    """

    text_nodes = list(node.find_all(string=True, recursive=True))
    if text_nodes:
        text_nodes[0].replace_with(soup.new_string(translation))
        for s in text_nodes[1:]:
            s.extract()
    else:
        node.insert(0, soup.new_string(translation))


def _collect_text_blocks(soup) -> list:
    return [
        node
        for node in soup.find_all(True)
        if node.name in BLOCK_TAGS and is_leaf_block(node) and bool((node.get_text() or "").strip())
    ]


def _index_records_by_chapter(records: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for record in records:
        chapter_id = normalize_chapter_id(str(record.get("chapter_id", "")))
        result.setdefault(chapter_id, []).append(record)
    for chapter_records in result.values():
        chapter_records.sort(
            key=lambda record: int(
                record.get("block_index")
                if record.get("block_index") is not None
                else record.get("line_number", 0)
            )
        )
    return result


def _validate_chapter_records(
    chapter_id: str,
    blocks: list,
    records: list[dict],
) -> None:
    if len(blocks) != len(records):
        raise SegmentCountMismatchError(
            f"章节 {chapter_id} 段落数不一致：期望 {len(records)}，实际 {len(blocks)}"
        )
    for block_index, (node, record) in enumerate(zip(blocks, records, strict=True)):
        actual_text = (node.get_text() or "").strip()
        expected_index = int(record.get("block_index", -1))
        expected_checksum = str(record.get("source_checksum", ""))
        if (
            expected_index != block_index
            or compute_source_checksum(actual_text) != expected_checksum
        ):
            raise SegmentCountMismatchError(f"章节 {chapter_id} 第 {block_index} 段定位校验失败")


def _validate_legacy_records(doc_name: str, blocks: list, records: list[dict]) -> None:
    if len(blocks) != len(records):
        raise SegmentCountMismatchError(
            f"章节 {normalize_chapter_id(doc_name)} 段落数不一致："
            f"期望 {len(records)}，实际 {len(blocks)}"
        )
    for block_index, (node, record) in enumerate(zip(blocks, records, strict=True)):
        actual_text = (node.get_text() or "").strip()
        expected_text = str(record.get("original_text", "")).strip()
        if actual_text != expected_text:
            raise SegmentCountMismatchError(
                f"章节 {normalize_chapter_id(doc_name)} 第 {block_index} 段原文不一致"
            )


def _find_legacy_chapter_records(
    doc_name: str,
    records_by_chapter: dict[str, list[dict]],
) -> tuple[str, list[dict]]:
    """Match historical chapter IDs that were prefixed with ``Text/``."""
    chapter_id = normalize_chapter_id(doc_name)
    if chapter_id in records_by_chapter:
        return chapter_id, records_by_chapter[chapter_id]

    suffix_matches = [
        candidate
        for candidate in records_by_chapter
        if candidate.endswith(f"/{chapter_id}") or chapter_id.endswith(f"/{candidate}")
    ]
    if len(suffix_matches) == 1:
        matched = suffix_matches[0]
        return matched, records_by_chapter[matched]
    if len(suffix_matches) > 1:
        raise SegmentCountMismatchError(f"章节 {chapter_id} 的旧格式路径匹配不唯一")
    return "", []
