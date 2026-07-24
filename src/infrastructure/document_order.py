#!/usr/bin/env python3
"""
EPUB spine 文档顺序模块

提供唯一的 spine 文档迭代入口。导入和导出必须共用此模块，
确保章节遍历顺序一致。

阶段 4（EPUB 拆分）从 epub_processor.py 提取。

职责：
- iter_spine_documents：按 spine 阅读顺序遍历文档项目
- normalize_chapter_id：章节 ID 归一化，保留完整相对路径
- get_item_name / get_item_media_type：ebooklib item 辅助方法
"""

from collections.abc import Mapping
from typing import Iterator

from ..utils.logger import get_logger

logger = get_logger(__name__)

# EPUB 容器根前缀（仅移除一级，不递归）
_CONTAINER_PREFIXES = ("oebps/", "epub/", "ops/")
_GET_TYPE_ATTRIBUTE = "get_type"


def _ebooklib_item_type(attribute_name: str) -> object:
    """Read a runtime ebooklib item constant behind one stub boundary."""
    import ebooklib

    return getattr(ebooklib, attribute_name)


def _get_item_type(item: object) -> object | None:
    """Return an item's ebooklib type without assuming a concrete item class."""
    getter = getattr(item, _GET_TYPE_ATTRIBUTE, None)
    return getter() if callable(getter) else None


def get_item_name(item) -> str:
    """安全地获取 EpubItem 的文件名

    兼容不同版本 ebooklib 的属性差异。
    """
    if hasattr(item, "file_name"):
        return item.file_name
    if hasattr(item, "href"):
        return item.href
    if hasattr(item, "get_name"):
        try:
            return item.get_name()
        except (AttributeError, KeyError):
            # ebooklib 不同版本 API 差异，最佳努力获取名称
            pass
    return ""


def get_item_media_type(item) -> str:
    """安全地获取 EpubItem 的 media_type

    兼容不同版本 ebooklib，缺失时从文件名扩展名推断。
    """
    if hasattr(item, "get_media_type"):
        try:
            return item.get_media_type()
        except (AttributeError, KeyError):
            # ebooklib 不同版本 API 差异，最佳努力获取媒体类型
            pass
    if hasattr(item, "media_type"):
        return item.media_type
    # 从 file_name 扩展名推断
    if hasattr(item, "file_name"):
        fname = item.file_name.lower()
        if fname.endswith((".html", ".xhtml", ".htm")):
            return "application/xhtml+xml"
        elif fname.endswith(".css"):
            return "text/css"
        elif fname.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        elif fname.endswith(".png"):
            return "image/png"
        elif fname.endswith(".gif"):
            return "image/gif"
        elif fname.endswith(".svg"):
            return "image/svg+xml"
        elif fname.endswith(".ncx"):
            return "application/x-dtbncx+xml"
    return ""


def normalize_chapter_id(name: str) -> str:
    """规范化章节 ID，保留完整相对路径以确保唯一性。

    R2-BUG-005 修复：
    - 只移除 EPUB 容器根前缀（OEBPS/、EPUB/、OPS/），保留实际章节目录。
    - 不再强制加 Text/ 前缀，避免 part1/chapter.xhtml 和 part2/chapter.xhtml
      被归一化为相同的 Text/chapter.xhtml 导致 locator 互相覆盖。
    - Windows 和 POSIX 分隔符归一化后得到相同 ID。
    - reimport 和 export 使用完全相同的章节 ID 规则。
    """
    if not name:
        return name
    # 统一路径分隔符
    n = name.replace("\\", "/")
    # 移除常见的 EPUB 容器根前缀（仅一级，不递归）
    low = n.lower()
    for prefix in _CONTAINER_PREFIXES:
        if low.startswith(prefix):
            n = n[len(prefix) :]
            break
    # 移除开头的 /
    n = n.lstrip("/")
    return n


def iter_spine_documents(book, item_by_id: Mapping[str, object] | None = None) -> Iterator:
    """按 spine 阅读顺序遍历文档项目。

    R2-BUG-001 修复：
    - 兼容字符串 idref、带 idref 属性的对象和文档对象三种 spine 形态。
      字符串形式（如 "chapter_1"）通过 book.get_item_with_id() 解析。
    - 明确处理 linear="no"：非线性条目不在主阅读流中，跳过并记录 debug。
    - 调用方应在 spine 非空但未产出任何文档时抛出异常，避免空内容映射。

    1. 遍历 book.spine
    2. 解析 idref 并取得对应文档对象（可复用调用方已建立的 item 索引）
    3. 跳过非线性（linear="no"）或不存在的项目
    """
    document_item_type = _ebooklib_item_type("ITEM_DOCUMENT")
    seen_ids = set()
    for item in book.spine:
        try:
            # 处理不同版本 spine 格式
            if isinstance(item, tuple):
                itemref = item[0] if len(item) > 0 else None
                linear = item[1] if len(item) > 1 else True
            else:
                itemref = item
                linear = True

            if not itemref:
                continue

            # R2-BUG-001：linear="no" 表示非线性条目，不在主阅读流中
            if linear in ("no", False):
                logger.debug("跳过非线性 spine 条目: %s", itemref)
                continue

            # 解析 idref 取得文档对象（兼容三种形态）
            doc_item = None
            if isinstance(itemref, str):
                if item_by_id is not None:
                    doc_item = item_by_id.get(itemref)
                else:
                    try:
                        doc_item = book.get_item_with_id(itemref)
                    except (KeyError, AttributeError):
                        doc_item = None
            elif hasattr(itemref, "idref"):
                item_id = itemref.idref
                if item_by_id is not None:
                    doc_item = item_by_id.get(item_id)
                else:
                    try:
                        doc_item = book.get_item_with_id(item_id)
                    except (KeyError, AttributeError):
                        doc_item = None
            elif hasattr(itemref, "get_type"):
                doc_item = itemref

            if doc_item is None:
                logger.warning("spine 条目无法解析为文档项目，已跳过: %s", itemref)
                continue

            # 仅处理文档类型
            if _get_item_type(doc_item) != document_item_type:
                continue

            # 去重（防止 spine 中重复 idref）
            item_name = get_item_name(doc_item)
            if item_name and item_name in seen_ids:
                continue
            seen_ids.add(item_name)

            yield doc_item
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("遍历 spine 时出错，已跳过: %s", exc)
            continue
