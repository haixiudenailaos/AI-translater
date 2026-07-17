#!/usr/bin/env python3
"""
EPUB 段落提取模块

定义块级标签选择规则，从文档生成段落映射和稳定定位器，
并支持根据定位器重新匹配旧译文。

阶段 4（EPUB 拆分）从 epub_processor.py 提取。

职责：
- BLOCK_TAGS：块级标签集合（导入和导出共用，不能有两份）
- extract_segments：从 BeautifulSoup 文档提取段落映射
- compute_source_checksum：原文段校验和
- match_existing_translation：按优先级匹配旧译文
"""

import hashlib
from typing import Dict, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 块级标签集合：导入和导出共用，不能有两份定义
BLOCK_TAGS = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "caption", "figcaption"}
)


def compute_source_checksum(text: str) -> str:
    """计算原文段的短校验和，用于稳定定位符。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def is_leaf_block(node) -> bool:
    """判断节点是否为叶子块节点（无块级子标签）。

    避免重复提取嵌套内容。
    """
    has_block_children = any(
        child.name in BLOCK_TAGS for child in node.find_all(True, recursive=False)
    )
    return not has_block_children


def extract_segments_from_document(
    soup,
    chapter_id: str,
    start_line_number: int,
) -> Tuple[Dict[str, Dict], int, int]:
    """从单个文档提取段落映射。

    与 import_epub 中的提取逻辑完全一致，导出时也使用相同的遍历规则。

    Args:
        soup: BeautifulSoup 解析的文档
        chapter_id: 归一化后的章节 ID
        start_line_number: 全局起始行号（从1开始）

    Returns:
        (segments, next_line_number, block_count)
        - segments: {cid: {original_text, line_number, chapter_id, block_index, source_checksum}}
        - next_line_number: 下一个文档的起始行号
        - block_count: 本文档提取的段落数
    """
    segments: Dict[str, Dict] = {}
    global_line_number = start_line_number
    block_index_in_chapter = 0

    for node in soup.find_all(True):
        if node.name in BLOCK_TAGS:
            if not is_leaf_block(node):
                continue

            text = (node.get_text() or "").strip()
            if text:
                cid = f"line_{global_line_number:06d}"
                checksum = compute_source_checksum(text)

                segments[cid] = {
                    "original_text": text,
                    "line_number": global_line_number,
                    "chapter_id": chapter_id,
                    "block_index": block_index_in_chapter,
                    "source_checksum": checksum,
                }
                global_line_number += 1
                block_index_in_chapter += 1

    return segments, global_line_number, block_index_in_chapter


def match_existing_translation(
    chapter_id: str,
    block_index: int,
    checksum: str,
    original_text: str,
    existing_by_locator: Dict[str, dict],
    existing_by_chapter_seq: Dict[str, dict],
    existing_translations: Dict[str, dict],
    text_occurrence_count: Dict[str, int],
) -> Tuple[str, str]:
    """按优先级匹配旧译文。

    优先级：
    1. 稳定定位符（chapter_id + block_index + source_checksum）
    2. chapter_id + block_index（R2-BUG-004：位置降级必须校验原文）
    3. 原文文本匹配（降级，需后续验证唯一性）

    Args:
        chapter_id: 章节ID
        block_index: 章节内块索引
        checksum: 原文校验和
        original_text: 原文文本
        existing_by_locator: 按定位符索引的旧译文
        existing_by_chapter_seq: 按章节+块索引的旧译文
        existing_translations: 按原文文本索引的旧译文
        text_occurrence_count: 原文出现次数计数（用于唯一性判断）

    Returns:
        (translated_text, translated_at)
    """
    locator = f"{chapter_id}|{block_index}|{checksum}"

    # 1. 稳定定位符
    if locator in existing_by_locator:
        record = existing_by_locator[locator]
        return record["translated_text"], record["translated_at"]

    # 2. chapter_id + block_index（R2-BUG-004：位置降级必须校验原文）
    seq_key = f"{chapter_id}|{block_index}"
    if seq_key in existing_by_chapter_seq:
        candidate = existing_by_chapter_seq[seq_key]
        if (
            candidate.get("source_checksum") == checksum
            or candidate.get("original_text", "").strip() == original_text.strip()
        ):
            return candidate["translated_text"], candidate["translated_at"]
        else:
            logger.warning(
                "位置降级匹配失败（原文已变化）: %s|%s, 旧 checksum=%s, 新 checksum=%s",
                chapter_id,
                block_index,
                candidate.get("source_checksum"),
                checksum,
            )

    # 3. 原文匹配（降级）
    if original_text in existing_translations:
        text_occurrence_count[original_text] = text_occurrence_count.get(original_text, 0) + 1
        record = existing_translations[original_text]
        return record["translated_text"], record["translated_at"]

    return "", ""
