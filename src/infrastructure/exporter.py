#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
from pathlib import Path
from typing import Dict, Optional

from ..domain.errors import EpubFingerprintMismatchError
from ..utils.logger import get_logger
from .document_order import iter_spine_documents, normalize_chapter_id, get_item_name
from .segment_extractor import BLOCK_TAGS, is_leaf_block
from .image_rewriter import (
    add_translated_images,
    rewrite_image_references,
    inject_figcaption,
    match_and_get_new_path,
)
from .mapping_repository import load_content_mapping

logger = get_logger(__name__)


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
    image_map: Optional[Dict[str, str]] = None,
    image_text_map: Optional[Dict[str, Dict]] = None,
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
        from ebooklib import epub
        import ebooklib
        from bs4 import BeautifulSoup, NavigableString
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

    # PERF-007：收集 spine 文档列表，避免多次遍历生成器
    spine_docs = list(iter_spine_documents(book))
    spine_names = {get_item_name(item) for item in spine_docs}

    # 处理图片替换
    path_mapping = None
    if image_map:
        print(f"正在处理 {len(image_map)} 张图片的替换...")
        local_images_dir = mapping_dir_p / "images"
        path_mapping = add_translated_images(book, image_map, local_images_dir)
        if path_mapping:
            # PERF-007：非 spine 文档单独处理图片引用，spine 文档在导出循环中合并处理
            rewrite_image_references(book, path_mapping, skip_names=spine_names)

    # 构造按 line_number 排序的译文列表
    sorted_items = sorted(items.items(), key=lambda x: x[1].get("line_number", 999999))

    translations = []
    originals = []
    for k, v in sorted_items:
        translation = v.get("translated_text", "")
        original = v.get("original_text", "")
        if translation.strip() and translation.strip() != original.strip():
            translations.append(translation.strip())
            originals.append(original.strip())
        else:
            translations.append("")
            originals.append(original.strip())

    # 全局行号计数器
    global_line_index = 0

    # PERF-007：单次 DOM 解析，合并正文替换、图片引用替换、figcaption 和空白清理
    for item in spine_docs:
        try:
            doc_name = get_item_name(item)
            html = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            # 按文档顺序遍历所有块级标签（与导入时一致）
            for node in soup.find_all(True):
                if node.name in BLOCK_TAGS:
                    if not is_leaf_block(node):
                        continue

                    # 跳过仅包含图片的容器
                    if node.find("img"):
                        text_content = "".join(
                            str(s) for s in node.find_all(string=True, recursive=True)
                        ).strip()
                        if not text_content or len(text_content) < 2:
                            continue

                    has_text = bool((node.get_text() or "").strip())
                    if has_text:
                        if global_line_index < len(translations):
                            translation = translations[global_line_index]
                            if translation:
                                _replace_text_preserving_inline(soup, node, translation)
                        global_line_index += 1

            # PERF-007：图片引用替换（合并到同一 DOM 解析，避免双重解析）
            if path_mapping:
                doc_path = Path(doc_name)
                doc_dir = doc_path.parent
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
                            if image_tag.has_attr("xlink:href"):
                                image_tag["xlink:href"] = new_rel_path
                            else:
                                image_tag["href"] = new_rel_path

            # 清理图片前后的多余空白
            for img in soup.find_all("img"):
                parent = img.parent
                if parent:
                    for child in list(parent.children):
                        if isinstance(child, NavigableString):
                            if not str(child).strip():
                                child.extract()

            # 注入图片文字翻译注释
            if image_text_map:
                inject_figcaption(soup, image_text_map, doc_name)

            new_html = str(soup)
            try:
                item.set_content(new_html.encode("utf-8"))
            except Exception:
                try:
                    item._content = new_html.encode("utf-8")
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠ 警告：处理文档时出错: {e}")
            continue

    # 写出 EPUB
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return str(out_path)


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
                "源 EPUB 大小已变化（旧=%s, 新=%s），无内容哈希无法精确校验，"
                "导出可能产生位置错位", stored_size, src_stat.st_size,
            )
        elif stored_mtime and stored_mtime != src_stat.st_mtime:
            logger.info("源 EPUB 修改时间已变化但大小一致，继续导出")


def _replace_text_preserving_inline(soup, node, translation: str) -> None:
    """R2-BUG-007：保留内联格式替换文本。

    只替换直接子文本节点，保留 em/strong/a/ruby/span 等内联标签及其属性。
    译文放在第一个直接子文本节点位置，其余直接子文本节点清除。
    """
    from bs4 import NavigableString

    direct_strings = [
        c for c in node.children
        if isinstance(c, NavigableString)
    ]
    if direct_strings:
        direct_strings[0].replace_with(soup.new_string(translation))
        for s in direct_strings[1:]:
            s.extract()
    else:
        node.insert(0, soup.new_string(translation))
