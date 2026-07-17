#!/usr/bin/env python3
"""
EPUB 映射仓库模块

读写映射格式，原子保存，格式版本管理。

阶段 4（EPUB 拆分）从 epub_processor.py 提取。

职责：
- load_content_mapping：加载 content_mapping.json，按行号排序返回
- save_translations：保存译文到 content_mapping.json
- load_old_translations：加载旧翻译数据（用于 reimport 时保留进度）
- 原子写入（委托 file_handler.write_json_atomic）
"""

import datetime
import json
from pathlib import Path
from typing import Dict, List, Tuple

from ..utils.file_handler import write_json_atomic
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 映射文件格式版本
SCHEMA_VERSION = 1


def load_content_mapping(mapping_dir: str) -> Tuple[List[str], List[str]]:
    """加载 content_mapping.json，严格按行号顺序返回原文和译文列表。

    最可靠的对齐机制：
    - 使用 line_number 作为唯一标识符，从1开始
    - 不依赖 JSON 键的顺序（JSON 无序）
    - 不依赖外部索引，只依赖内部 line_number 字段
    - 返回格式：([原文], [译文])
    """
    md = Path(mapping_dir) / "content_mapping.json"
    data = json.loads(md.read_text(encoding="utf-8"))
    items: Dict[str, Dict] = data.get("content_mappings", {})

    # 收集所有条目，必须有有效的 line_number
    entries = []
    for k, v in items.items():
        line_num = v.get("line_number")
        if line_num is None:
            raise Exception(f"条目 {k} 缺少 line_number 字段，数据损坏")
        entries.append(
            {
                "key": k,
                "line_number": int(line_num),
                "original_text": v.get("original_text", ""),
                "translated_text": v.get("translated_text", ""),
            }
        )

    # 严格按 line_number 排序
    entries.sort(key=lambda x: x["line_number"])

    # 验证 line_number 连续性
    for i, entry in enumerate(entries):
        expected_line = i + 1
        actual_line = entry["line_number"]
        if actual_line != expected_line:
            print(f"⚠ 警告：line_number 不连续！位置 {i}: 期望 {expected_line}, 实际 {actual_line}")

    originals = [e["original_text"] for e in entries]
    translations = [e["translated_text"] for e in entries]
    return originals, translations


def save_translations(mapping_dir: str, translated_lines: List[str]) -> None:
    """将译文列表按行号严格对齐保存到 content_mapping.json。

    - 不修改原有的 line_number（保持绝对稳定）
    - 按 line_number 排序后，第 i 个条目对应 translated_lines[i]
    - 自动更新 translated_at 时间戳
    - 未翻译的行保持空字符串
    """
    md = Path(mapping_dir) / "content_mapping.json"
    obj = json.loads(md.read_text(encoding="utf-8"))
    items = obj.get("content_mappings", {})
    now = datetime.datetime.now().isoformat()

    # 按 line_number 排序所有条目
    sorted_items = sorted(items.items(), key=lambda x: x[1].get("line_number", 999999))

    for idx, (key, item_data) in enumerate(sorted_items):
        translation = translated_lines[idx] if idx < len(translated_lines) else ""
        old_translation = item_data.get("translated_text", "")
        items[key]["translated_text"] = translation
        if translation != old_translation:
            items[key]["translated_at"] = now

    obj["project_info"]["updated_at"] = now
    write_json_atomic(md, obj)


def load_old_translations(mapping_dir: Path) -> Tuple[Dict, Dict, Dict]:
    """加载旧翻译数据，用于 reimport 时保留翻译进度。

    Returns:
        (existing_translations, existing_by_locator, existing_by_chapter_seq)
        - existing_translations: 按原文文本索引
        - existing_by_locator: 按稳定定位符索引
        - existing_by_chapter_seq: 按章节+块索引索引（含 checksum 和原文用于校验）
    """
    existing_translations: Dict[str, dict] = {}
    existing_by_locator: Dict[str, dict] = {}
    existing_by_chapter_seq: Dict[str, dict] = {}

    content_file = mapping_dir / "content_mapping.json"
    if not content_file.exists():
        return existing_translations, existing_by_locator, existing_by_chapter_seq

    try:
        old_data = json.loads(content_file.read_text(encoding="utf-8"))
        old_mappings = old_data.get("content_mappings", {})
        for _key, item in old_mappings.items():
            original = item.get("original_text", "")
            translated = item.get("translated_text", "")
            translated_at = item.get("translated_at", "")
            if not (original and translated):
                continue
            record = {"translated_text": translated, "translated_at": translated_at}

            # 优先：稳定定位符
            chapter_id = item.get("chapter_id", "")
            block_index = item.get("block_index")
            checksum = item.get("source_checksum", "")
            if chapter_id and block_index is not None and checksum:
                locator = f"{chapter_id}|{block_index}|{checksum}"
                existing_by_locator[locator] = record

            # 次优先：chapter_id + block_index（R2-BUG-004：附带 checksum 和原文）
            if chapter_id and block_index is not None:
                seq_key = f"{chapter_id}|{block_index}"
                existing_by_chapter_seq[seq_key] = {
                    **record,
                    "source_checksum": checksum,
                    "original_text": original,
                }

            # 降级：原文文本
            existing_translations[original] = record

        print(f"✓ 检测到已有翻译数据，已保留 {len(existing_translations)} 条翻译记录")
    except (OSError, json.JSONDecodeError, KeyError, AttributeError) as e:
        print(f"⚠ 警告：读取旧翻译数据失败: {e}")

    return existing_translations, existing_by_locator, existing_by_chapter_seq


def save_content_mapping(
    mapping_dir: Path,
    content_mappings: Dict[str, Dict],
    project_info: Dict,
) -> None:
    """保存 content_mapping.json（原子写入）。"""
    content_file = mapping_dir / "content_mapping.json"
    payload = {
        "project_info": project_info,
        "content_mappings": content_mappings,
    }
    write_json_atomic(content_file, payload)


def save_images_mapping(mapping_dir: Path, images_mapping: Dict[str, Dict]) -> None:
    """保存 images.json（原子写入）。"""
    images_file = mapping_dir / "images.json"
    payload = {"image_mappings": images_mapping}
    write_json_atomic(images_file, payload)


def save_format_info(mapping_dir: Path, format_info: Dict) -> None:
    """保存 format_info.json（原子写入）。"""
    format_file = mapping_dir / "format_info.json"
    write_json_atomic(format_file, format_info)
