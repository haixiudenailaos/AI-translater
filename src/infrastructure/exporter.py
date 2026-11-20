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
import importlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Dict
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from ..domain.epub_export import (
    BilingualDocumentPlan,
    BilingualExportReport,
    BilingualRenderPlan,
    EpubDocumentSnapshot,
    EpubExportOptions,
    EpubTextMode,
)
from ..domain.errors import BilingualEpubExportError, EpubFingerprintMismatchError
from ..utils.logger import get_logger
from .bilingual_renderer import BILINGUAL_CSS, build_pair_id, render_bilingual_document
from .bilingual_validation import validate_bilingual_epub
from .document_order import get_item_name, iter_spine_documents, normalize_chapter_id
from .image_rewriter import (
    add_translated_images,
    get_document_content,
    inject_figcaption,
    match_and_get_new_path,
    rewrite_image_references,
    set_document_content,
)
from .mapping_repository import read_content_mapping_snapshot, resolve_mapping_file
from .segment_extractor import (
    BLOCK_TAGS,
    compute_source_checksum,
    is_leaf_block,
)

logger = get_logger(__name__)
_BEAUTIFUL_SOUP_ATTRIBUTE = "BeautifulSoup"
_READ_EPUB_ATTRIBUTE = "read_epub"
_WRITE_EPUB_ATTRIBUTE = "write_epub"


def _mapping_int(value: object, *, default: int) -> int:
    """Safely recover integer ordering fields from persisted JSON."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return int(value)
    except ValueError:
        return default


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
    *,
    options: EpubExportOptions | None = None,
    snapshot: EpubDocumentSnapshot | None = None,
    on_report: Callable[[BilingualExportReport], None] | None = None,
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
    8. 双语模式：原文节点保持原样，译文插入其后的独立容器

    Args:
        mapping_dir: 映射目录路径
        output_path: 输出 EPUB 文件路径
        image_map: {original_epub_path: local_new_filename} 图片替换映射
        image_text_map: {image_path: {translated_text, original_text}} 图片文字翻译
        options: 呈现选项；``None`` 等价于既有纯译文导出
        snapshot: 双语模式必需的主线程文档快照
        on_report: 双语模式通过增强校验后回调一次不可变统计

    Returns:
        输出文件路径

    Raises:
        EpubFingerprintMismatchError: 源 EPUB 内容哈希不匹配
        BilingualEpubExportError: 双语快照、结构或写出后校验失败
        Exception: 缺少必要文件或依赖
    """
    bilingual = options is not None and options.text_mode is EpubTextMode.BILINGUAL
    if bilingual and snapshot is None:
        raise BilingualEpubExportError("双语 EPUB 导出缺少文档快照，已拒绝使用磁盘旧译文")
    if not bilingual and snapshot is not None:
        raise BilingualEpubExportError("文档快照仅用于双语 EPUB 导出")
    started_at = perf_counter()
    bilingual_job_id = uuid4().hex[:8] if bilingual else ""

    try:
        epub_module = importlib.import_module("ebooklib.epub")
        beautiful_soup = getattr(importlib.import_module("bs4"), _BEAUTIFUL_SOUP_ATTRIBUTE)
        read_epub = getattr(epub_module, _READ_EPUB_ATTRIBUTE)
        write_epub = getattr(epub_module, _WRITE_EPUB_ATTRIBUTE)
    except ImportError:
        raise Exception("需要安装ebooklib和beautifulsoup4库来支持EPUB导出")

    mapping_dir_p = Path(mapping_dir)
    if bilingual:
        assert snapshot is not None
        content_obj = read_content_mapping_snapshot(mapping_dir_p)
        items = content_obj.get("content_mappings", {})
        project_info = content_obj.get("project_info", {})
        if not isinstance(items, dict) or not isinstance(project_info, dict):
            raise BilingualEpubExportError("映射文件格式无效，无法导出双语 EPUB")
        ordered_records = _validate_bilingual_snapshot(mapping_dir_p, project_info, items, snapshot)
        _overlay_snapshot_targets(ordered_records, snapshot)
    else:
        content_file = resolve_mapping_file(mapping_dir_p, "content_mapping.json")
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
    book = read_epub(str(original_file))
    _configure_single_page_pagination(book, project_info, Path(original_file))

    stylesheet_name = ""
    stylesheet_id = ""
    if bilingual:
        stylesheet_name, stylesheet_id = _choose_bilingual_stylesheet(book)
    language_tag = options.target_language_tag if options is not None else None

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
        mapping_soup = beautiful_soup(html, "html.parser")
        soup = beautiful_soup(html, "xml")
        mapping_blocks = _collect_text_blocks(mapping_soup)
        output_blocks = _collect_text_blocks(soup)
        if len(mapping_blocks) != len(output_blocks):
            raise SegmentCountMismatchError(
                f"章节 {normalize_chapter_id(doc_name)} 的 HTML/XML 段落结构不一致："
                f"定位 {len(mapping_blocks)}，输出 {len(output_blocks)}"
            )
        if bilingual:
            for block_index, (mapping_block, output_block) in enumerate(
                zip(mapping_blocks, output_blocks, strict=True)
            ):
                if (mapping_block.get_text() or "").strip() != (
                    output_block.get_text() or ""
                ).strip():
                    raise SegmentCountMismatchError(
                        f"章节 {normalize_chapter_id(doc_name)} 第 {block_index} 段 "
                        "的 HTML/XML 文本不一致"
                    )
        parsed_docs.append((item, doc_name, soup, mapping_blocks, output_blocks))
    parsed_at = perf_counter()

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
    bilingual_documents: list[BilingualDocumentPlan] = []
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

        if bilingual:
            if isinstance(item, epub_module.EpubNav):
                # ebooklib 会用包内目录数据重新生成导航文档，不能在其 DOM 中插入译文。
                bilingual_documents.append(_preserved_document_plan(doc_name, records))
            else:
                bilingual_documents.append(
                    render_bilingual_document(
                        soup,
                        doc_name,
                        records,
                        output_blocks,
                        language_tag=language_tag,
                        stylesheet_name=stylesheet_name,
                    )
                )
        else:
            for node, record in zip(output_blocks, records, strict=False):
                translation = str(record.get("translated_text", "")).strip()
                original = str(record.get("original_text", "")).strip()
                if translation and translation != original:
                    _replace_text_preserving_inline(soup, node, translation)

        # 注释必须在图片 src 改写前匹配原始 EPUB 路径。
        if image_text_map:
            inject_figcaption(soup, image_text_map, doc_name, mark_generated=bilingual)

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
    rendered_at = perf_counter()

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
    bilingual_plan: BilingualRenderPlan | None = None
    if bilingual:
        _add_bilingual_stylesheet(book, epub_module, stylesheet_name, stylesheet_id)
        _append_target_language(book, language_tag)
        bilingual_plan = _build_bilingual_plan(
            stylesheet_name, bilingual_documents, len(sorted_records)
        )
    _ensure_toc_link_ids(book, epub_module)
    write_epub(str(out_path), book)
    _enforce_single_page_spine(out_path)
    written_at = perf_counter()
    if bilingual_plan is not None:
        # 双语结构校验只在新模式执行；通过后才允许主线程显示统计。
        validate_bilingual_epub(out_path, bilingual_plan, language_tag=language_tag)
        validated_at = perf_counter()
        report = bilingual_plan.report
        logger.info(
            "双语 EPUB 导出完成: job=%s, text_mode=%s, session=%s, document_version=%s, total=%d, "
            "paired=%d, untranslated=%d, preserved=%d, "
            "prepare=%.2fs, render=%.2fs, write=%.2fs, validate=%.2fs, elapsed=%.2fs",
            bilingual_job_id,
            EpubTextMode.BILINGUAL.value,
            snapshot.session_id if snapshot is not None else "",
            snapshot.document_version if snapshot is not None else "",
            report.total_segments,
            report.paired_segments,
            report.untranslated_segments,
            report.preserved_segments,
            parsed_at - started_at,
            rendered_at - parsed_at,
            written_at - rendered_at,
            validated_at - written_at,
            validated_at - started_at,
        )
        if on_report is not None:
            on_report(report)
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
            if isinstance(item, tuple | list):
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


def _same_project_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return os.path.normcase(str(left.absolute())) == os.path.normcase(str(right.absolute()))


def _validate_bilingual_snapshot(
    mapping_dir: Path,
    project_info: dict,
    items: dict,
    snapshot: EpubDocumentSnapshot,
) -> list[dict]:
    """Validate snapshot/mapping identity and strict line alignment; return ordered records."""
    if not _same_project_path(mapping_dir, snapshot.mapping_dir):
        raise BilingualEpubExportError("文档快照的映射目录与导出映射目录不一致")
    original_file = project_info.get("original_file")
    if not original_file or not _same_project_path(Path(str(original_file)), snapshot.source_path):
        raise BilingualEpubExportError("文档快照与原 EPUB 映射不属于同一项目")

    records = list(items.values())
    if len(snapshot.source_lines) != len(snapshot.target_lines):
        raise BilingualEpubExportError("文档快照的原文与译文行数不一致")
    if len(records) != len(snapshot.source_lines):
        raise BilingualEpubExportError(
            "映射记录数与文档快照不一致：" f"映射 {len(records)}，快照 {len(snapshot.source_lines)}"
        )

    by_line_number: dict[int, dict] = {}
    for record in records:
        line_number = record.get("line_number")
        if isinstance(line_number, bool) or not isinstance(line_number, int):
            raise BilingualEpubExportError(f"映射行号 {line_number!r} 不是整数，无法安全对齐")
        if line_number in by_line_number:
            raise BilingualEpubExportError(f"映射行号重复：第 {line_number} 行出现多次")
        by_line_number[line_number] = record

    ordered: list[dict] = []
    for expected_line in range(1, len(records) + 1):
        record = by_line_number.get(expected_line)
        if record is None:
            raise BilingualEpubExportError(f"映射行号缺失：第 {expected_line} 行不存在")
        ordered.append(record)

    for index, record in enumerate(ordered):
        mapped_text = str(record.get("original_text", "")).strip()
        current_text = str(snapshot.source_lines[index]).strip()
        if mapped_text != current_text:
            raise BilingualEpubExportError(
                f"第 {index + 1} 行原文已编辑，与 EPUB 映射不一致；"
                "可使用现有 TXT 对照导出保存编辑内容"
            )
    return ordered


def _overlay_snapshot_targets(ordered_records: list[dict], snapshot: EpubDocumentSnapshot) -> None:
    """Overlay snapshot translations onto the private in-memory mapping copy."""
    for record, target in zip(ordered_records, snapshot.target_lines, strict=True):
        record["translated_text"] = target


def _existing_item_names(book) -> set[str]:
    names: set[str] = set()
    for item in book.get_items():
        name = get_item_name(item)
        if name:
            names.add(str(name))
    return names


def _existing_item_ids(book) -> set[str]:
    ids: set[str] = set()
    for item in book.get_items():
        item_id = getattr(item, "id", None)
        if isinstance(item_id, str) and item_id:
            ids.add(item_id)
    return ids


def _preserved_document_plan(document_name: str, records: list[dict]) -> BilingualDocumentPlan:
    """Build a plan for a document that keeps all segments untouched (e.g. nav)."""
    pair_ids = tuple(build_pair_id(record.get("line_number")) for record in records)
    return BilingualDocumentPlan(
        document_name=document_name,
        targets=(),
        preserved_pair_ids=pair_ids,
        untranslated_pair_ids=(),
        original_ids=(),
        generated=True,
    )


def _choose_bilingual_stylesheet(book) -> tuple[str, str]:
    """Choose a CSS file name and manifest id that never shadow existing resources."""
    names = _existing_item_names(book)
    file_name = "Styles/ait-bilingual.css"
    suffix = 0
    while file_name in names:
        suffix += 1
        file_name = f"Styles/ait-bilingual-{suffix}.css"

    ids = _existing_item_ids(book)
    item_id = "ait-bilingual-css"
    suffix = 0
    while item_id in ids:
        suffix += 1
        item_id = f"ait-bilingual-css-{suffix}"
    return file_name, item_id


def _add_bilingual_stylesheet(book, epub_module, file_name: str, item_id: str) -> None:
    css_item = epub_module.EpubItem(
        uid=item_id,
        file_name=file_name,
        media_type="text/css",
        content=BILINGUAL_CSS.encode("utf-8"),
    )
    book.add_item(css_item)


def _append_target_language(book, language_tag: str | None) -> None:
    if not language_tag:
        return
    try:
        existing = {str(value[0]) for value in book.get_metadata("DC", "language") if value}
    except (KeyError, AttributeError):
        existing = set()
    if language_tag not in existing:
        book.add_metadata("DC", "language", language_tag)


def _build_bilingual_plan(
    stylesheet_name: str,
    documents: list[BilingualDocumentPlan],
    total_segments: int,
) -> BilingualRenderPlan:
    report = BilingualExportReport(
        total_segments=total_segments,
        paired_segments=sum(len(document.targets) for document in documents),
        untranslated_segments=sum(len(document.untranslated_pair_ids) for document in documents),
        preserved_segments=sum(len(document.preserved_pair_ids) for document in documents),
    )
    if report.total_segments != (
        report.paired_segments + report.untranslated_segments + report.preserved_segments
    ):
        raise BilingualEpubExportError("双语渲染计划未覆盖全部映射段落")
    return BilingualRenderPlan(
        stylesheet_name=stylesheet_name,
        documents=tuple(documents),
        report=report,
    )


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
            key=lambda record: _mapping_int(
                record.get("block_index")
                if record.get("block_index") is not None
                else record.get("line_number", 0),
                default=0,
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
