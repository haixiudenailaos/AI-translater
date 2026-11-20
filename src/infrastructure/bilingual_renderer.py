#!/usr/bin/env python3
"""双语 EPUB 局部 DOM 渲染器。

只对已经定位好的段落节点插入译文：原文节点保持位置、子树、锚点和属性，
译文作为纯文本写入新容器。本模块不读取项目、不访问 UI，也不负责写出或校验。
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.epub_export import (
    BilingualDocumentPlan,
    BilingualTargetEntry,
)
from ..domain.errors import BilingualEpubExportError
from .image_rewriter import is_hidden_node, is_image_only_document

SOURCE_CLASS = "ait-bilingual-source"
TARGET_CLASS = "ait-bilingual-target"
HEADING_TARGET_CLASS = "ait-bilingual-heading-target"
INLINE_TARGET_CLASS = "ait-bilingual-inline-target"
PAIR_ATTRIBUTE = "data-ait-pair"

BILINGUAL_CSS = """\
.ait-bilingual-source {
    page-break-after: avoid;
    break-after: avoid;
}
.ait-bilingual-target {
    display: block;
    margin-top: 0.25em;
    margin-bottom: 1em;
    font-size: 0.95em;
    line-height: 1.65;
    white-space: normal;
}
.ait-bilingual-inline-target {
    font-size: 0.95rem;
    font-weight: normal;
    line-height: 1.65;
    white-space: normal;
}
.ait-bilingual-heading-target {
    font-weight: bold;
}
"""

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_APPEND_TARGET_TAGS = {"li", "caption", "figcaption"}
# 允许在块级节点之后插入同级 p 的容器；不在其中的结构退回内嵌 span。
_FLOW_CONTAINERS = {
    "body",
    "div",
    "section",
    "article",
    "main",
    "aside",
    "header",
    "footer",
    "nav",
    "blockquote",
    "li",
    "dd",
    "dt",
    "figure",
    "figcaption",
    "td",
    "th",
    "caption",
    "form",
    "fieldset",
    "details",
}


def build_pair_id(line_number: object) -> str:
    """Convert a mapping line number into the EPUB-internal pair marker."""
    try:
        value = int(line_number)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise BilingualEpubExportError(f"映射行号无效，无法生成双语标记: {line_number!r}") from exc
    return f"line_{value:06d}"


def has_bilingual_markers(soup: Any) -> bool:
    """Return whether a source document already contains this feature's markers."""
    if soup.find(attrs={PAIR_ATTRIBUTE: True}) is not None:
        return True
    return soup.find(class_=SOURCE_CLASS) is not None or soup.find(class_=TARGET_CLASS) is not None


def _classes_of(node: Any) -> list[str]:
    classes = node.get("class") or []
    if isinstance(classes, str):
        return classes.split()
    return [str(value) for value in classes]


def _add_class(node: Any, class_name: str) -> None:
    classes = _classes_of(node)
    if class_name not in classes:
        classes.append(class_name)
    node["class"] = classes


def _build_target(
    soup: Any,
    tag_name: str,
    text: str,
    pair_id: str,
    language_tag: str | None,
    *,
    heading: bool,
    inline: bool = False,
) -> Any:
    target = soup.new_tag(tag_name)
    classes = [TARGET_CLASS]
    if inline:
        classes.append(INLINE_TARGET_CLASS)
    if heading:
        classes.append(HEADING_TARGET_CLASS)
    target["class"] = classes
    target[PAIR_ATTRIBUTE] = pair_id
    if language_tag:
        target["lang"] = language_tag
        target["xml:lang"] = language_tag
    for index, line in enumerate(text.split("\n")):
        if index:
            target.append(soup.new_tag("br"))
        if line:
            target.append(soup.new_string(line))
    return target


def _can_insert_sibling(node: Any) -> bool:
    parent = getattr(node, "parent", None)
    if parent is None:
        return False
    return str(getattr(parent, "name", "") or "").lower() in _FLOW_CONTAINERS


def _insert_translation(
    soup: Any,
    node: Any,
    text: str,
    pair_id: str,
    language_tag: str | None,
) -> None:
    name = str(getattr(node, "name", "") or "").lower()
    _add_class(node, SOURCE_CLASS)
    node[PAIR_ATTRIBUTE] = pair_id

    if name in _APPEND_TARGET_TAGS:
        node.append(
            _build_target(soup, "span", text, pair_id, language_tag, heading=False, inline=True)
        )
        return
    if name == "blockquote":
        node.append(_build_target(soup, "p", text, pair_id, language_tag, heading=False))
        return

    heading = name in _HEADING_TAGS
    if _can_insert_sibling(node):
        node.insert_after(_build_target(soup, "p", text, pair_id, language_tag, heading=heading))
        return
    # 特殊上下文：不在不允许 flow 内容的父节点里新增块级同级节点。
    node.append(
        _build_target(soup, "span", text, pair_id, language_tag, heading=heading, inline=True)
    )


def add_stylesheet_link(soup: Any, stylesheet_href: str) -> None:
    """Add this feature's stylesheet link to one document without duplicating it."""
    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        html = soup.find("html")
        if html is not None:
            html.insert(0, head)
        else:
            soup.insert(0, head)

    for link in head.find_all("link"):
        if str(link.get("href") or "") == stylesheet_href:
            return

    link = soup.new_tag("link")
    link["rel"] = "stylesheet"
    link["type"] = "text/css"
    link["href"] = stylesheet_href
    head.append(link)


def stylesheet_href_for_document(document_name: str, stylesheet_name: str) -> str:
    """Compute the EPUB-internal POSIX relative path from a document to the CSS."""
    document_dir = posixpath.dirname(str(document_name)) or "."
    return posixpath.relpath(stylesheet_name, start=document_dir)


def render_bilingual_document(
    soup: Any,
    document_name: str,
    records: Sequence[Mapping[str, object]],
    blocks: Sequence[Any],
    *,
    language_tag: str | None,
    stylesheet_name: str,
) -> BilingualDocumentPlan:
    """Insert translations into one parsed document and return its render plan."""
    if has_bilingual_markers(soup):
        raise BilingualEpubExportError(
            f"章节 {document_name} 已包含双语导出标记，请使用最初的原 EPUB 项目重新导出"
        )
    if len(records) != len(blocks):
        raise BilingualEpubExportError(
            f"章节 {document_name} 的定位结果与映射数量不一致，无法安全插入译文"
        )

    image_only = is_image_only_document(soup)
    original_ids = tuple(str(node.get("id")) for node in soup.find_all(True) if node.get("id"))

    targets: list[BilingualTargetEntry] = []
    preserved: list[str] = []
    untranslated: list[str] = []

    for node, record in zip(blocks, records, strict=False):
        pair_id = build_pair_id(record.get("line_number"))
        text = str(record.get("translated_text") or "").strip()
        # 不参与双语呈现的节点先计入 preserved，其余再区分已译/未译。
        if image_only or is_hidden_node(node):
            preserved.append(pair_id)
            continue
        if not text:
            untranslated.append(pair_id)
            continue
        source_text = str(node.get_text() or "").strip()
        _insert_translation(soup, node, text, pair_id, language_tag)
        targets.append(
            BilingualTargetEntry(
                pair_id=pair_id,
                text=text,
                source_text=source_text,
            )
        )

    if targets:
        add_stylesheet_link(soup, stylesheet_href_for_document(document_name, stylesheet_name))

    return BilingualDocumentPlan(
        document_name=document_name,
        targets=tuple(targets),
        preserved_pair_ids=tuple(preserved),
        untranslated_pair_ids=tuple(untranslated),
        original_ids=original_ids,
    )
