#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

import os
from pathlib import Path
from typing import Dict, Tuple, Optional

from ..utils.logger import get_logger
from .document_order import normalize_chapter_id

logger = get_logger(__name__)


def match_and_get_new_path(
    src: str,
    path_mapping: Dict[str, str],
    doc_dir: Path,
) -> Tuple[bool, str]:
    """匹配图片路径并返回新的相对路径。

    R2-BUG-016：禁止仅按 basename 匹配图片，避免同名图片互换译文。
    仅允许精确匹配和路径尾部匹配（至少包含一级目录）。

    Args:
        src: 原始 src 属性值
        path_mapping: {original_epub_path: new_epub_path}
        doc_dir: 当前文档所在目录

    Returns:
        (matched: bool, new_rel_path: str)
    """
    try:
        # 模拟绝对路径计算
        current_dir = str(doc_dir).replace("\\", "/")
        if current_dir == ".":
            current_dir = ""

        # 处理 ../
        src_parts = src.split("/")
        curr_parts = current_dir.split("/") if current_dir else []

        while src_parts and src_parts[0] == "..":
            src_parts.pop(0)
            if curr_parts:
                curr_parts.pop()

        abs_src = "/".join(curr_parts + src_parts)
        if abs_src.startswith("/"):
            abs_src = abs_src[1:]

        # 检查映射
        matched = False
        new_abs_path = None
        if abs_src in path_mapping:
            new_abs_path = path_mapping[abs_src]
            matched = True
        else:
            # R2-BUG-016：路径尾部匹配（不含纯 basename）
            src_norm = src.replace("\\", "/")
            for orig_path, new_path in path_mapping.items():
                orig_norm = orig_path.replace("\\", "/")
                if orig_norm.endswith(src_norm) or src_norm.endswith(orig_norm):
                    new_abs_path = new_path
                    matched = True
                    break

        if matched and new_abs_path:
            rel_path = os.path.relpath(new_abs_path, str(doc_dir))
            rel_path = rel_path.replace("\\", "/")
            return True, rel_path

        return False, ""
    except Exception as e:
        logger.error("match_and_get_new_path 异常: %s: %s", type(e).__name__, e, exc_info=True)
        return False, ""


def add_translated_images(
    book,
    image_map: Dict[str, str],
    local_images_dir: Path,
) -> Dict[str, str]:
    """将翻译后的图片添加到 EPUB book。

    Args:
        book: ebooklib epub.Book 实例
        image_map: {original_epub_path: local_new_filename}
        local_images_dir: 本地图片目录

    Returns:
        path_mapping: {original_epub_path: new_epub_path}
    """
    from ebooklib import epub
    import mimetypes

    path_mapping: Dict[str, str] = {}

    for orig_path, new_filename in image_map.items():
        local_file = local_images_dir / new_filename
        if not local_file.exists():
            print(f"⚠ 警告：新图片文件丢失: {local_file}")
            continue

        try:
            with open(local_file, "rb") as f:
                img_content = f.read()

            # 构建新图片在 EPUB 中的路径（保持在同一目录下）
            orig_p = Path(orig_path)
            new_epub_path = str(orig_p.parent / new_filename).replace("\\", "/")

            new_id = f"img_{Path(new_filename).stem}"
            img_item = epub.EpubImage(
                uid=new_id,
                file_name=new_epub_path,
                media_type=mimetypes.guess_type(new_filename)[0] or "image/jpeg",
                content=img_content,
            )
            book.add_item(img_item)
            path_mapping[orig_path] = new_epub_path
        except Exception as e:
            logger.error("添加图片 %s 失败: %s: %s", new_filename, type(e).__name__, e, exc_info=True)
            print(f"⚠ 添加图片 {new_filename} 失败: {e}")

    return path_mapping


def rewrite_image_references(book, path_mapping: Dict[str, str], skip_names: Optional[set] = None) -> None:
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
            content = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(content, "html.parser")
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
                item.set_content(str(soup).encode("utf-8"))
        except Exception as e:
            logger.error("处理图片引用失败 (%s): %s: %s", item.get_name(), type(e).__name__, e, exc_info=True)
            print(f"⚠ 处理图片引用失败 ({item.get_name()}): {e}")


def inject_figcaption(
    soup,
    image_text_map: Dict[str, Dict],
    doc_name: str,
) -> None:
    """注入图片文字翻译注释（figcaption）。

    R2-BUG-016：禁止仅按 basename 匹配图片，避免同名图片互换注释。
    优先完整路径匹配，降级到路径尾部匹配，再降级到相对路径解析匹配。

    Args:
        soup: BeautifulSoup 文档对象
        image_text_map: {image_path: {translated_text, original_text, ...}}
        doc_name: 当前文档名称（用于相对路径解析）
    """
    if not image_text_map:
        return

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue

        matched_key = None
        doc_name_norm = normalize_chapter_id(doc_name)
        for img_path in image_text_map:
            # 1. 完整路径匹配
            if img_path == src:
                matched_key = img_path
                break
            # 2. 路径尾部匹配
            img_path_norm = img_path.replace("\\", "/")
            src_norm = src.replace("\\", "/")
            if img_path_norm.endswith(src_norm) or src_norm.endswith(img_path_norm):
                matched_key = img_path
                break
            # 3. 解析相对路径后完整匹配
            try:
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(doc_name_norm), src_norm)
                ).replace("\\", "/")
                if resolved == img_path_norm:
                    matched_key = img_path
                    break
            except Exception:
                pass

        if matched_key and image_text_map[matched_key].get("translated_text"):
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
