#!/usr/bin/env python3
"""双语 EPUB 写出后的独立结构校验。

校验器只读取临时 EPUB 与 exporter 提供的私有渲染计划，不重新提取段落
（否则会把新增译文误当作原段落），也不负责备份或原子发布。
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from ..domain.epub_export import BilingualDocumentPlan, BilingualRenderPlan
from ..domain.errors import BilingualEpubExportError
from .bilingual_renderer import (
    HEADING_TARGET_CLASS,
    INLINE_TARGET_CLASS,
    PAIR_ATTRIBUTE,
    SOURCE_CLASS,
    TARGET_CLASS,
)
from .image_rewriter import IMAGE_CAPTION_ATTRIBUTE

_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_MIMETYPE = b"application/epub+zip"
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE_TARGET_SOURCES = {"li", "caption", "figcaption"}


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _class_tokens(element: ElementTree.Element) -> set[str]:
    return set(str(element.get("class") or "").split())


def _normalized_text(element: ElementTree.Element) -> str:
    return "".join("".join(element.itertext()).split())


def _parse_xml(payload: bytes, description: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise BilingualEpubExportError(f"{description} 不是可解析的 XML：{exc}") from exc


def _read_container_root(archive: ZipFile) -> tuple[str, str]:
    if "META-INF/container.xml" not in archive.namelist():
        raise BilingualEpubExportError("导出文件缺少 META-INF/container.xml")
    container = _parse_xml(archive.read("META-INF/container.xml"), "container.xml")
    for element in container.iter():
        if _local_name(element.tag) == "rootfile" and element.get("full-path"):
            package_path = str(element.get("full-path"))
            return package_path, posixpath.dirname(package_path)
    raise BilingualEpubExportError("container.xml 缺少 rootfile")


def _manifest_entries(package: ElementTree.Element) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    ids: list[str] = []
    for element in package.iter():
        if _local_name(element.tag) != "item":
            continue
        href = element.get("href")
        if not href:
            continue
        item_id = str(element.get("id") or "")
        ids.append(item_id)
        entries[href] = {
            "id": item_id,
            "media_type": str(element.get("media-type") or ""),
        }
    if len(ids) != len(set(ids)):
        raise BilingualEpubExportError("导出 OPF manifest 中存在重复的 item id")
    return entries


def _text_excluding_targets(element: ElementTree.Element) -> str:
    """Collect original text while ignoring inserted targets and image captions."""
    chunks = [element.text or ""]
    for child in element:
        if TARGET_CLASS not in _class_tokens(child) and not child.get(IMAGE_CAPTION_ATTRIBUTE):
            chunks.append(_text_excluding_targets(child))
        chunks.append(child.tail or "")
    return "".join(chunks)


def _verify_target_structure(
    document_name: str,
    root: ElementTree.Element,
    sources: list[ElementTree.Element],
    targets: list[ElementTree.Element],
) -> None:
    """Check tag placement against the bilingual rendering contract in §7.2."""
    parents = {child: parent for parent in root.iter() for child in parent}
    sources_by_pair = {
        str(element.get(PAIR_ATTRIBUTE)): element
        for element in sources
        if element.get(PAIR_ATTRIBUTE)
    }
    for element in targets:
        pair_id = str(element.get(PAIR_ATTRIBUTE) or "")
        source = sources_by_pair.get(pair_id)
        if source is None:
            raise BilingualEpubExportError(f"章节 {document_name} 的译文容器缺少对应原文节点")
        source_tag = _local_name(source.tag).lower()
        target_tag = _local_name(element.tag).lower()
        parent = parents.get(element)
        if source_tag in _HEADING_TAGS:
            if target_tag == "span":
                if parent is not source or INLINE_TARGET_CLASS not in _class_tokens(element):
                    raise BilingualEpubExportError(f"章节 {document_name} 的标题译文结构不正确")
            elif target_tag != "p" or HEADING_TARGET_CLASS not in _class_tokens(element):
                raise BilingualEpubExportError(f"章节 {document_name} 的标题译文结构不正确")
            elif parent is None or _local_name(parent.tag).lower() in {"ul", "ol", "p"}:
                raise BilingualEpubExportError(f"章节 {document_name} 的标题译文位置不安全")
        elif source_tag in _INLINE_TARGET_SOURCES:
            if (
                target_tag != "span"
                or parent is not source
                or INLINE_TARGET_CLASS not in _class_tokens(element)
            ):
                raise BilingualEpubExportError(
                    f"章节 {document_name} 的 {source_tag} 译文应内嵌在原节点内"
                )
        elif source_tag == "blockquote":
            if target_tag != "p" or parent is not source:
                raise BilingualEpubExportError(
                    f"章节 {document_name} 的引用译文应保留在同一引用块内"
                )
        else:
            if target_tag == "p":
                container = parents.get(source)
                if parent is not container or parent is None:
                    raise BilingualEpubExportError(
                        f"章节 {document_name} 的段落译文应为原节点的同级兄弟"
                    )
                if _local_name(parent.tag).lower() in {"ul", "ol", "p"}:
                    raise BilingualEpubExportError(f"章节 {document_name} 的段落译文位置不安全")
            elif (
                target_tag != "span"
                or parent is not source
                or INLINE_TARGET_CLASS not in _class_tokens(element)
            ):
                raise BilingualEpubExportError(f"章节 {document_name} 的段落译文结构不正确")


def _verify_document(
    root: ElementTree.Element,
    document_plan: BilingualDocumentPlan,
    *,
    stylesheet_name: str,
    language_tag: str | None,
) -> tuple[int, int, int]:
    """Verify one rendered document and return its (paired, untranslated, preserved)."""
    document_name = document_plan.document_name
    if document_plan.generated:
        leftover = [element for element in root.iter() if TARGET_CLASS in _class_tokens(element)]
        if leftover:
            raise BilingualEpubExportError(
                f"章节 {document_name} 由阅读器重新生成，却包含双语译文容器"
            )
        return (0, 0, len(document_plan.preserved_pair_ids))

    targets = [element for element in root.iter() if TARGET_CLASS in _class_tokens(element)]
    sources = [element for element in root.iter() if SOURCE_CLASS in _class_tokens(element)]

    expected = {entry.pair_id: entry.text for entry in document_plan.targets}
    observed_ids = [str(element.get(PAIR_ATTRIBUTE) or "") for element in targets]
    if "" in observed_ids or len(observed_ids) != len(set(observed_ids)):
        raise BilingualEpubExportError(f"章节 {document_name} 存在缺失或重复的双语 pair 标记")
    present = set(observed_ids)
    if present != set(expected):
        raise BilingualEpubExportError(
            f"章节 {document_name} 的译文容器与渲染计划不一致："
            f"期望 {len(expected)}，实际 {len(targets)}"
        )
    if present.intersection(document_plan.untranslated_pair_ids):
        raise BilingualEpubExportError(f"章节 {document_name} 为未译段落生成了译文容器")
    if present.intersection(document_plan.preserved_pair_ids):
        raise BilingualEpubExportError(f"章节 {document_name} 为保留段落生成了译文容器")
    if len(sources) != len(targets):
        raise BilingualEpubExportError(f"章节 {document_name} 的原节点标记数量与译文容器不一致")
    source_pair_ids = [str(element.get(PAIR_ATTRIBUTE) or "") for element in sources]
    if (
        "" in source_pair_ids
        or len(source_pair_ids) != len(set(source_pair_ids))
        or set(source_pair_ids) != present
    ):
        raise BilingualEpubExportError(f"章节 {document_name} 的原节点 pair 标记不完整或重复")

    for element in targets:
        pair_id = str(element.get(PAIR_ATTRIBUTE) or "")
        if _normalized_text(element) != "".join(expected[pair_id].split()):
            raise BilingualEpubExportError(f"章节 {document_name} 的译文文本与渲染计划不一致")
        if language_tag:
            if element.get("lang") != language_tag:
                raise BilingualEpubExportError(f"章节 {document_name} 的译文 lang 属性不正确")
            if element.get(f"{{{_XML_NAMESPACE}}}lang") != language_tag:
                raise BilingualEpubExportError(f"章节 {document_name} 的译文 xml:lang 属性不正确")

    _verify_target_structure(document_name, root, sources, targets)

    expected_sources = {entry.pair_id: entry.source_text for entry in document_plan.targets}
    for element in sources:
        pair_id = str(element.get(PAIR_ATTRIBUTE) or "")
        if pair_id not in expected_sources:
            raise BilingualEpubExportError(f"章节 {document_name} 的原节点标记不在渲染计划中")
        if "".join(_text_excluding_targets(element).split()) != "".join(
            expected_sources[pair_id].split()
        ):
            raise BilingualEpubExportError(f"章节 {document_name} 的原文内容被双语渲染改变")

    modified_ids = [str(element.get("id")) for element in root.iter() if element.get("id")]
    if len(modified_ids) != len(document_plan.original_ids):
        raise BilingualEpubExportError(f"章节 {document_name} 的既有 id 数量发生变化")
    if not set(document_plan.original_ids).issubset(set(modified_ids)):
        raise BilingualEpubExportError(f"章节 {document_name} 的原有锚点或链接标识被破坏")

    link_targets = {
        posixpath.normpath(
            posixpath.join(posixpath.dirname(document_name), str(element.get("href")))
        )
        for element in root.iter()
        if _local_name(element.tag) == "link" and element.get("href")
    }
    if document_plan.targets:
        if stylesheet_name not in link_targets:
            raise BilingualEpubExportError(f"章节 {document_name} 缺少双语样式表引用")
    elif stylesheet_name in link_targets:
        raise BilingualEpubExportError(f"章节 {document_name} 未插入译文却引用了双语样式表")

    return (
        len(document_plan.targets),
        len(document_plan.untranslated_pair_ids),
        len(document_plan.preserved_pair_ids),
    )


def validate_bilingual_epub(
    epub_path: Path,
    plan: BilingualRenderPlan,
    *,
    language_tag: str | None = None,
) -> None:
    """Validate a published bilingual EPUB against an exporter-private render plan."""
    paired = untranslated = preserved = 0
    try:
        with ZipFile(epub_path) as archive:
            if "mimetype" not in archive.namelist():
                raise BilingualEpubExportError("导出文件缺少 EPUB mimetype 条目")
            if archive.read("mimetype").strip() != _MIMETYPE:
                raise BilingualEpubExportError("导出文件的 mimetype 内容不正确")

            package_path, package_dir = _read_container_root(archive)
            if package_path not in archive.namelist():
                raise BilingualEpubExportError("导出文件缺少 OPF package 文档")
            package = _parse_xml(archive.read(package_path), package_path)
            manifest = _manifest_entries(package)

            stylesheet = manifest.get(plan.stylesheet_name)
            if stylesheet is None:
                raise BilingualEpubExportError("双语样式表未登记到 OPF manifest")
            if stylesheet["media_type"] != "text/css":
                raise BilingualEpubExportError("双语样式表的媒体类型不是 text/css")

            for document_plan in plan.documents:
                archive_name = posixpath.join(package_dir, document_plan.document_name)
                if archive_name not in archive.namelist():
                    raise BilingualEpubExportError(
                        f"导出文件缺少章节 {document_plan.document_name}"
                    )
                root = _parse_xml(archive.read(archive_name), document_plan.document_name)
                doc_paired, doc_untranslated, doc_preserved = _verify_document(
                    root,
                    document_plan,
                    stylesheet_name=plan.stylesheet_name,
                    language_tag=language_tag,
                )
                paired += doc_paired
                untranslated += doc_untranslated
                preserved += doc_preserved
    except BadZipFile as exc:
        raise BilingualEpubExportError("导出文件不是有效的 EPUB/ZIP") from exc

    report = plan.report
    if (
        report.paired_segments != paired
        or report.untranslated_segments != untranslated
        or report.preserved_segments != preserved
        or report.total_segments != paired + untranslated + preserved
    ):
        raise BilingualEpubExportError("双语导出的段落统计与实际结构不一致")
