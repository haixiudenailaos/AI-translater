#!/usr/bin/env python3
"""
EPUB 图片重写模块

处理新图片 item、HTML 图片引用和 figcaption。
不混入正文翻译替换。

阶段 4（EPUB 拆分）从 epub_processor.py 提取。

职责：
- add_translated_images：将翻译后的图片添加到 EPUB book
- rewrite_image_references：替换文档中的 img/image 标签引用
- inject_figcaption：注入图片文字翻译注释
- match_and_get_new_path：匹配图片路径并计算新的相对路径
"""

import hashlib
import io
import posixpath
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from types import MethodType
from typing import Dict, Tuple
from urllib.parse import unquote, urlsplit

from ..utils.logger import get_logger
from .document_order import get_item_name, normalize_chapter_id

logger = get_logger(__name__)

_MEDIA_TYPE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def get_document_content(item) -> bytes:
    """Return the XHTML bytes stored in the archive without ebooklib templating.

    ``EpubHtml.get_content()`` rebuilds the document from ebooklib's chapter
    template. On books loaded with ``read_epub`` that drops the original head,
    stylesheet links, body classes and case-sensitive SVG attributes.
    """
    content = getattr(item, "content", None)
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, bytes):
        return content
    return item.get_content()


def _return_raw_document_content(item, default=None):
    content = getattr(item, "content", None)
    return content if content is not None else default


def set_document_content(item, content: bytes) -> None:
    """Make ebooklib write a complete XHTML document without rebuilding it."""
    item.content = content
    item.get_content = MethodType(_return_raw_document_content, item)


def _normalize_epub_path(path: str) -> str:
    """Normalize an EPUB-internal path without changing its root namespace."""
    value = unquote(str(path or "")).replace("\\", "/").strip()
    if not value:
        return ""
    normalized = posixpath.normpath(value)
    if normalized == ".":
        return ""
    return normalized.lstrip("/")


def _path_alias(path: str) -> str:
    """Return a comparison alias that only removes a known package prefix."""
    return normalize_chapter_id(_normalize_epub_path(path))


def _paths_equivalent(left: str, right: str) -> bool:
    left_norm = _normalize_epub_path(left)
    right_norm = _normalize_epub_path(right)
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm or _path_alias(left_norm) == _path_alias(right_norm)


def _resolve_resource_path(src: str, doc_dir: Path) -> str:
    """Resolve an XHTML resource reference in the same coordinate system as doc_dir."""
    parsed = urlsplit(str(src or ""))
    if parsed.scheme or parsed.netloc:
        return ""
    clean_src = _normalize_epub_path(parsed.path)
    if not clean_src:
        return ""
    current_dir = _normalize_epub_path(str(doc_dir))
    if str(parsed.path).replace("\\", "/").startswith("/"):
        return clean_src
    return _normalize_epub_path(posixpath.join(current_dir, clean_src))


def match_and_get_new_path(
    src: str,
    path_mapping: Dict[str, str],
    doc_dir: Path,
) -> Tuple[bool, str]:
    """匹配图片路径并返回新的相对路径。

    R2-BUG-016：禁止仅按 basename 匹配图片，避免同名图片互换译文。
    仅允许精确匹配和路径尾部匹配（至少包含一级目录）。

    EPUB 导出修复（见 docs/EPUB_EXPORT_MIXED_TEXT_AND_BLANK_PAGE_REPAIR.md 5.3.2）：
    对 src 先拆除 URL fragment 和 query 再匹配，避免 `a.jpg#fragment`
    或 `a.jpg?x=1` 无法匹配。

    Args:
        src: 原始 src 属性值
        path_mapping: {original_epub_path: new_epub_path}
        doc_dir: 当前文档所在目录

    Returns:
        (matched: bool, new_rel_path: str)
    """
    try:
        resolved_src = _resolve_resource_path(src, doc_dir)
        if not resolved_src:
            return False, ""

        # Only accept a unique full-path match. The old endswith comparison also
        # matched a bare "cover.jpg" and could replace it with another directory's
        # image of the same name.
        matches = [
            (orig_path, new_path)
            for orig_path, new_path in path_mapping.items()
            if _paths_equivalent(orig_path, resolved_src)
        ]
        if len(matches) > 1:
            logger.error("图片引用匹配不唯一，保留原图: src=%s, resolved=%s", src, resolved_src)
            return False, ""
        if matches:
            new_abs_path = _normalize_epub_path(matches[0][1])
            current_dir = _normalize_epub_path(str(doc_dir)) or "."
            rel_path = posixpath.relpath(new_abs_path, current_dir)
            return True, rel_path

        return False, ""
    except (OSError, AttributeError, ValueError) as e:
        logger.error("match_and_get_new_path 异常: %s: %s", type(e).__name__, e, exc_info=True)
        return False, ""


def add_translated_images(
    book,
    image_map: Dict[str, str],
    mapping_dir: Path,
) -> Tuple[Dict[str, str], int]:
    """将翻译后的图片添加到 EPUB book。

    EPUB 导出修复（见 docs/EPUB_EXPORT_MIXED_TEXT_AND_BLANK_PAGE_REPAIR.md 5.3.1）：
    统一 result_map 的相对路径契约，兼容两种 provider 的输出：

    - 新格式: {orig_path: "translated_images/provider/x.png"}
      值为相对于 mapping_dir 的完整路径。
    - 旧格式（AI Provider）: {orig_path: "x.png"}
      值仅为文件名，文件位于 mapping_dir/images/。

    Args:
        book: ebooklib epub.Book 实例
        image_map: {original_epub_path: local_relative_path_or_filename}
        mapping_dir: mapping 根目录（不再固定为 images/ 子目录）

    Returns:
        (path_mapping, failed_count)
        - path_mapping: {original_epub_path: new_epub_path} 成功添加的图片
        - failed_count: 添加失败的图片数量（用于诊断和警告）
    """
    from ebooklib import epub

    path_mapping: Dict[str, str] = {}
    failed_images: list[tuple[str, str, str]] = []
    protected_cover_paths = _collect_cover_image_paths(book)

    for orig_path, new_path_value in image_map.items():
        if any(_paths_equivalent(orig_path, path) for path in protected_cover_paths):
            logger.info("封面图片保持原样，不应用批量插图翻译结果: %s", orig_path)
            continue

        # 解析本地文件路径（支持新旧两种 result_map 格式）
        local_file = _resolve_local_image_path(mapping_dir, new_path_value)
        if local_file is None or not local_file.exists():
            failed_images.append((orig_path, new_path_value, "本地文件不存在"))
            logger.error(
                "翻译图片本地文件缺失: orig=%s, expected=%s, resolved=%s",
                orig_path,
                new_path_value,
                local_file,
            )
            continue

        try:
            raw_img_content = local_file.read_bytes()
            img_content, detected_media_type, reason = _prepare_replacement_image(raw_img_content)
            if img_content is None or detected_media_type is None:
                failed_images.append((orig_path, new_path_value, reason))
                logger.error("翻译图片校验失败，保留原图: orig=%s, reason=%s", orig_path, reason)
                continue

            original_item = _find_unique_image_item(book, orig_path)

            # 规范化 EPUB 内资源路径
            # - 使用 POSIX 分隔符
            # - 保持原资源所在的路径坐标（不能擅自剥离 OEBPS/EPUB/OPS）
            # - 使用实际下载文件名（保留扩展名）
            original_epub_path = (
                get_item_name(original_item) if original_item is not None else orig_path
            )
            orig_p = Path(original_epub_path)
            media_type = detected_media_type
            new_filename = _filename_for_media_type(local_file.name, media_type)
            new_epub_path = _normalize_epub_image_path(orig_p.parent, new_filename)

            # UID 基于原始 EPUB 路径哈希，确保全书唯一
            # 旧实现使用 `img_{Path(new_filename).stem}`，
            # 不同目录同名文件会产生重复 UID，导致 manifest 冲突。
            new_id = _generate_image_uid(orig_path)

            img_item = epub.EpubImage(
                uid=new_id,
                file_name=new_epub_path,
                media_type=media_type,
                content=img_content,
            )
            book.add_item(img_item)
            path_mapping[orig_path] = new_epub_path
        except (AttributeError, TypeError, OSError) as e:
            failed_images.append((orig_path, new_path_value, str(e)))
            logger.error(
                "添加图片失败: orig=%s: %s: %s",
                orig_path,
                type(e).__name__,
                e,
                exc_info=True,
            )

    # 输出诊断记录：每张失败图片的原路径、期望路径和原因
    if failed_images:
        print(f"⚠ 警告：{len(failed_images)} 张翻译图片添加失败")
        for orig, expected, reason in failed_images:
            print(f"  - 原路径: {orig}, 期望文件: {expected}, 原因: {reason}")

    return path_mapping, len(failed_images)


def _resolve_local_image_path(mapping_dir: Path, new_path_value: str | None) -> Path | None:
    """解析本地图片文件路径，兼容新旧两种 result_map 格式。

    新格式: new_path_value 是相对于 mapping_dir 的路径，
        如 "translated_images/provider/x.png"。
    旧格式（AI Provider）: new_path_value 仅是文件名（如 "x.png"），
        文件位于 mapping_dir/images/。

    Args:
        mapping_dir: mapping 根目录
        new_path_value: result_map 中的值

    Returns:
        本地文件路径（调用方需检查 exists），或 None 表示路径无效
    """
    if not new_path_value:
        return None

    # 规范化为 POSIX 路径，并拒绝绝对路径或目录穿越。
    value_norm = new_path_value.replace("\\", "/")
    value_path = Path(value_norm)
    if value_path.is_absolute() or ".." in value_path.parts:
        logger.error("图片结果路径越出 mapping_dir，已拒绝: %s", new_path_value)
        return None

    # 优先按相对路径解析新格式
    candidate = mapping_dir / value_norm
    if candidate.exists():
        return candidate

    # 兼容旧格式：值仅为文件名（无路径分隔符），文件位于 mapping_dir/images/
    if "/" not in value_norm:
        legacy = mapping_dir / "images" / value_norm
        if legacy.exists():
            return legacy
        return legacy

    # 最佳努力：返回相对路径候选（调用方检查 exists）
    return candidate


def _normalize_epub_image_path(parent_dir: Path, filename: str) -> str:
    """规范化 EPUB 内部图片路径。

    必须保留 parent_dir 的路径坐标。ebooklib 的 item 名称通常相对于 OPF，
    但某些文件仍带 OEBPS/EPUB/OPS；只改一边会让 XHTML 引用越出资源目录。
    """
    # 拼接父目录与文件名
    full = str(parent_dir / filename).replace("\\", "/")
    return _normalize_epub_path(full)


def _filename_for_media_type(filename: str, media_type: str) -> str:
    """Keep the filename extension consistent with the actual image bytes."""
    expected = _MEDIA_TYPE_EXTENSIONS.get(media_type)
    if not expected:
        return filename
    path = Path(filename)
    current = path.suffix.lower()
    if media_type == "image/jpeg" and current in {".jpg", ".jpeg"}:
        return filename
    if current == expected:
        return filename
    return f"{path.stem}{expected}"


def _detect_image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<?xml", b"<svg")) and b"<svg" in head:
        return "image/svg+xml"
    return None


def _prepare_replacement_image(data: bytes) -> tuple[bytes | None, str | None, str]:
    """Accept every decodable image and normalize uncommon raster formats.

    File extensions and a small set of magic-byte signatures are not reliable
    enough to decide whether an AI-generated image is usable. Pillow performs
    the actual decode. Formats that EPUB readers do not consistently support
    are converted to PNG instead of rejecting an otherwise valid translation.
    """
    if not data:
        return None, None, "结果图片为空"

    media_type = _detect_image_media_type(data)
    if media_type == "image/svg+xml":
        lowered = data.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            return None, None, "结果图片损坏: SVG 不允许声明外部实体"
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            return None, None, f"结果图片损坏: {exc}"
        if root.tag.rsplit("}", 1)[-1].lower() != "svg":
            return None, None, "结果图片损坏: SVG 根元素无效"
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1].lower()
            if local_name in {"script", "foreignobject"}:
                return None, None, "结果图片损坏: SVG 包含不安全内容"
            for attribute, value in element.attrib.items():
                attribute_name = attribute.rsplit("}", 1)[-1].lower()
                if attribute_name.startswith("on"):
                    return None, None, "结果图片损坏: SVG 包含事件处理器"
                if attribute_name == "href" and value and not value.startswith("#"):
                    return None, None, "结果图片损坏: SVG 包含外部资源"
        return data, media_type, ""

    try:
        from PIL import Image
    except ImportError:
        if media_type is None:
            return None, None, "无法识别结果图片格式"
        return data, media_type, ""

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            decoded_media_type = Image.MIME.get(str(image.format or "").upper())

            if width <= 0 or height <= 0:
                return None, None, "结果图片尺寸无效"

            if decoded_media_type in _MEDIA_TYPE_EXTENSIONS:
                return data, decoded_media_type, ""

            output = io.BytesIO()
            normalized = image
            if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
                normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            normalized.save(output, format="PNG")
            normalized_data = output.getvalue()
            logger.info(
                "翻译图片格式 %s 已转换为 PNG 以确保 EPUB 兼容",
                image.format or "unknown",
            )
            return normalized_data, "image/png", ""
    except Exception as exc:
        return None, None, f"结果图片损坏或无法解码: {exc}"


def _find_unique_image_item(book, image_path: str):
    matches = []
    try:
        items = list(book.get_items())
    except (AttributeError, TypeError):
        return None
    for item in items:
        name = get_item_name(item)
        if name and _paths_equivalent(name, image_path):
            matches.append(item)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.error("EPUB 中原始图片路径不唯一: %s", image_path)
    return None


def _collect_cover_image_paths(book) -> set[str]:
    """Collect EPUB2/EPUB3 cover image paths so batch translation cannot replace them."""
    try:
        items = list(book.get_items())
    except (AttributeError, TypeError):
        return set()

    cover_ids: set[str] = set()
    try:
        metadata = book.get_metadata("OPF", "cover") or []
        for value, attrs in metadata:
            attrs = attrs or {}
            cover_id = attrs.get("content") or value
            if cover_id:
                cover_ids.add(str(cover_id))
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    result: set[str] = set()
    for item in items:
        name = get_item_name(item)
        if not name:
            continue
        properties = getattr(item, "properties", []) or []
        if isinstance(properties, str):
            properties = properties.split()
        item_id = getattr(item, "id", None) or getattr(item, "uid", None) or ""
        if "cover-image" in properties or str(item_id) in cover_ids:
            result.add(_normalize_epub_path(name))

    return result


def _validate_replacement_geometry(original_item, replacement: bytes) -> tuple[bool, str]:
    """Compatibility wrapper: only reject empty or undecodable images."""
    del original_item
    content, media_type, reason = _prepare_replacement_image(replacement)
    return content is not None and media_type is not None, reason


def _generate_image_uid(orig_path: str) -> str:
    """基于原始 EPUB 路径生成全书唯一的图片 UID。

    EPUB 导出修复（见 docs/EPUB_EXPORT_MIXED_TEXT_AND_BLANK_PAGE_REPAIR.md 5.3.2）：
    旧实现使用 `img_{Path(new_filename).stem}`，不同目录同名文件
    会产生重复 UID，导致 manifest 冲突。改用原始 EPUB 路径哈希。
    """
    path_hash = hashlib.sha256(orig_path.encode("utf-8")).hexdigest()[:12]
    return f"img_{path_hash}"


def rewrite_image_references(
    book, path_mapping: Dict[str, str], skip_names: set | None = None
) -> None:
    """替换文档中的图片引用（img 标签和 SVG image 标签）。

    Args:
        book: ebooklib epub.Book 实例
        path_mapping: {original_epub_path: new_epub_path}
        skip_names: PERF-007：需跳过的文档名集合（已在导出循环中合并处理，
                    避免 spine 文档被双重 DOM 解析）。
    """
    import ebooklib
    from bs4 import BeautifulSoup

    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        doc_name = item.get_name()
        if skip_names and doc_name in skip_names:
            continue  # PERF-007：已在导出循环中处理
        try:
            content = get_document_content(item).decode("utf-8", errors="ignore")
            soup = BeautifulSoup(content, "xml")
            modified = False

            doc_path = Path(doc_name)
            doc_dir = doc_path.parent

            # 替换 <img> src
            for img in soup.find_all("img"):
                src = img.get("src")
                if src:
                    matched, new_rel_path = match_and_get_new_path(src, path_mapping, doc_dir)
                    if matched:
                        img["src"] = new_rel_path
                        modified = True

            # 替换 SVG 中的 <image> 标签
            for image in soup.find_all("image"):
                href = image.get("xlink:href") or image.get("href")
                if href:
                    matched, new_rel_path = match_and_get_new_path(href, path_mapping, doc_dir)
                    if matched:
                        if image.has_attr("xlink:href"):
                            image["xlink:href"] = new_rel_path
                        else:
                            image["href"] = new_rel_path
                        modified = True

            if modified:
                set_document_content(item, str(soup).encode("utf-8"))
        except (AttributeError, TypeError) as e:
            logger.error(
                "处理图片引用失败 (%s): %s: %s", item.get_name(), type(e).__name__, e, exc_info=True
            )
            print(f"⚠ 处理图片引用失败 ({item.get_name()}): {e}")


def inject_figcaption(
    soup,
    image_text_map: Dict[str, Dict],
    doc_name: str,
) -> None:
    """注入图片文字翻译注释（figcaption）。

    R2-BUG-016：禁止仅按 basename 匹配图片，避免同名图片互换注释。
    与图片资源重写共用相同的文档相对路径解析和唯一匹配规则。

    Args:
        soup: BeautifulSoup 文档对象
        image_text_map: {image_path: {translated_text, original_text, ...}}
        doc_name: 当前文档名称（用于相对路径解析）
    """
    if not image_text_map:
        return

    # 独立插图页通常按整页高度排版。向这类页面追加 figcaption 会让内容
    # 超出单页，阅读器会把溢出区域分页成插图前后的空白页。
    if _is_image_only_document(soup):
        logger.debug("纯图片页保持原始 DOM，不注入图片文字注释: %s", doc_name)
        return

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue

        resolved_src = _resolve_resource_path(src, Path(doc_name).parent)
        if not resolved_src:
            continue

        matches = [
            img_path for img_path in image_text_map if _paths_equivalent(img_path, resolved_src)
        ]
        if len(matches) != 1:
            if len(matches) > 1:
                logger.error("图片注释匹配不唯一，已跳过: %s", src)
            continue
        matched_key = matches[0]

        if matched_key and image_text_map[matched_key].get("translated_text"):
            sibling = img.find_next_sibling()
            if sibling is not None and sibling.name == "figcaption":
                continue
            trans_info = image_text_map[matched_key]
            translated = trans_info["translated_text"]
            original = trans_info.get("original_text", "")
            caption_text = f"[翻译] {translated}"
            if original:
                caption_text = f"[原文: {original}] {translated}"
            figcaption = soup.new_tag("figcaption")
            figcaption.string = caption_text
            figcaption["style"] = (
                "font-size: 0.85em; color: #666; "
                "text-align: center; margin-top: 4px; "
                "font-style: italic;"
            )
            img.insert_after(figcaption)


def _is_image_only_document(soup) -> bool:
    """Return whether the body is a fixed/full-page image without visible text."""
    body = soup.find("body")
    if body is None:
        return False
    has_image = body.find("img") is not None or body.find("image") is not None
    return has_image and not _has_visible_body_text(body)


_NON_VISIBLE_TEXT_CONTAINERS = {
    "desc",
    "metadata",
    "noscript",
    "script",
    "style",
    "template",
    "title",
}
_HIDDEN_CLASS_NAMES = {
    "display-none",
    "hidden",
    "sr-only",
    "visually-hidden",
}
_ZERO_WIDTH_CHARACTERS = "\u200b\u200c\u200d\ufeff"
_ZERO_WIDTH_TRANSLATION = str.maketrans("", "", _ZERO_WIDTH_CHARACTERS)


def _has_visible_body_text(body) -> bool:
    """Return whether body contains text that is intended to be rendered.

    Full-page SVG illustrations commonly contain ``title`` or ``desc`` nodes for
    accessibility. Some publishers also include hidden running titles. Neither
    should turn an illustration page into a text page and enable figcaption
    injection, which can overflow the fixed page and create blank pages.
    """
    for text_node in body.find_all(string=True):
        text = str(text_node).translate(_ZERO_WIDTH_TRANSLATION)
        if not text.strip():
            continue

        element = getattr(text_node, "parent", None)
        hidden = False
        while element is not None and element is not body:
            name = str(getattr(element, "name", "") or "").lower()
            if name in _NON_VISIBLE_TEXT_CONTAINERS:
                hidden = True
                break

            attrs = getattr(element, "attrs", {}) or {}
            classes = attrs.get("class", [])
            if isinstance(classes, str):
                classes = classes.split()
            if _HIDDEN_CLASS_NAMES.intersection(str(value).lower() for value in classes):
                hidden = True
                break
            if element.has_attr("hidden") or str(attrs.get("aria-hidden", "")).lower() == "true":
                hidden = True
                break

            inline_style = "".join(str(attrs.get("style", "")).lower().split())
            if "display:none" in inline_style or "visibility:hidden" in inline_style:
                hidden = True
                break
            element = getattr(element, "parent", None)

        if not hidden:
            return True

    return False
