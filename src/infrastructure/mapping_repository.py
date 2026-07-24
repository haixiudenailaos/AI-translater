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
import shutil
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Tuple
from uuid import uuid4

from ..utils.file_handler import write_json_atomic
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 映射文件格式版本
SCHEMA_VERSION = 1
MAPPING_MANIFEST_FILENAME = "mapping_manifest.json"
MAPPING_GENERATIONS_DIRNAME = ".mapping_generations"
_MAPPING_FILENAMES = (
    "content_mapping.json",
    "images.json",
    "format_info.json",
)

# B-1：兼容副本开关。新代码内部不依赖顶层 JSON，设为 False 可节省一次写盘。
# 外部集成若仍直接读取顶层文件，保持 True 直到完成迁移。
_LEGACY_COMPAT_COPIES_ENABLED: bool = True


class MappingGenerationError(RuntimeError):
    """Published mapping manifest is missing or points outside its generation root."""


# ── B-2: generation mark-and-sweep GC ──────────────────────────────────────


def _reachable_generation_names(manifest: dict) -> set[str]:
    """Extract generation directory names currently referenced by *manifest*.

    Values in ``manifest["files"]`` are posix relative paths of the form
    ``".mapping_generations/<hex>/filename.json"``.  The generation name is
    the directory component immediately under MAPPING_GENERATIONS_DIRNAME.
    """
    names: set[str] = set()
    gen_prefix = MAPPING_GENERATIONS_DIRNAME + "/"
    for rel_path in manifest.get("files", {}).values():
        if isinstance(rel_path, str) and rel_path.startswith(gen_prefix):
            rest = rel_path[len(gen_prefix):]
            name = rest.split("/")[0]
            if name:
                names.add(name)
    return names


def _gc_unreachable_generations(mapping_dir: Path, current_manifest: dict) -> int:
    """Delete generation directories not referenced by *current_manifest*.

    B-1 (审计 §P1-PERF-1)：每次成功发布后调用，确保不可达的旧 generation
    目录不会无限增长。GC 失败不中断保存流程——记录 warning 后继续。

    Returns:
        Number of directories removed.
    """
    gen_root = mapping_dir / MAPPING_GENERATIONS_DIRNAME
    if not gen_root.is_dir():
        return 0

    reachable = _reachable_generation_names(current_manifest)
    removed = 0
    for candidate in gen_root.iterdir():
        if not candidate.is_dir() or candidate.name in reachable:
            continue
        try:
            shutil.rmtree(candidate)
            removed += 1
        except OSError as exc:
            logger.warning(
                "generation GC: 删除目录失败，跳过 %s (%s)", candidate.name, exc
            )
    if removed:
        logger.debug("generation GC: 已回收 %d 个不可达目录", removed)
    return removed


def _manifest_path(mapping_dir: Path) -> Path:
    return Path(mapping_dir) / MAPPING_MANIFEST_FILENAME


def _load_manifest(mapping_dir: Path) -> dict | None:
    path = _manifest_path(mapping_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingGenerationError("映射 generation manifest 损坏，拒绝混合读取文件") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), dict):
        raise MappingGenerationError("映射 generation manifest 格式无效")
    return raw


def _resolve_generation_path(mapping_dir: Path, relative_path: str) -> Path:
    root = Path(mapping_dir).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise MappingGenerationError("映射 generation 文件逃逸出受控目录") from None
    return candidate


def resolve_mapping_file(mapping_dir: str | Path, filename: str) -> Path:
    """Return the published generation file, falling back to legacy top-level JSON.

    Once a manifest exists, missing or malformed generation members are a
    recoverable error rather than permission to mix it with legacy files.
    """
    if filename not in _MAPPING_FILENAMES:
        raise ValueError(f"不是受支持的映射文件: {filename}")
    root = Path(mapping_dir)
    manifest = _load_manifest(root)
    if manifest is None:
        return root / filename
    relative_path = manifest["files"].get(filename)
    if not isinstance(relative_path, str) or not relative_path:
        raise MappingGenerationError(f"generation 缺少 {filename}")
    path = _resolve_generation_path(root, relative_path)
    if not path.is_file():
        raise MappingGenerationError(f"已发布 generation 缺少 {filename}")
    return path


def _new_generation_directory(mapping_dir: Path) -> tuple[str, Path]:
    generation = uuid4().hex
    directory = mapping_dir / MAPPING_GENERATIONS_DIRNAME / generation
    directory.mkdir(parents=True, exist_ok=False)
    return generation, directory


def _relative_generation_path(mapping_dir: Path, path: Path) -> str:
    return path.relative_to(mapping_dir).as_posix()


def _publish_manifest(mapping_dir: Path, generation: str, files: dict[str, str]) -> None:
    write_json_atomic(
        _manifest_path(mapping_dir),
        {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "published_at": datetime.datetime.now().isoformat(),
            "files": files,
        },
    )


def _write_legacy_compatibility_copy(path: Path, payload: dict) -> None:
    """Best-effort copy for older integrations that still open top-level JSON.

    The manifest is the commit point. A compatibility-copy failure must not
    invalidate a successfully published generation or make new readers fall
    back to potentially mixed files.

    B-1：受 _LEGACY_COMPAT_COPIES_ENABLED 控制。设为 False 后不再写顶层
    兼容副本，节省一次写盘和 fsync。迁移期间保持 True。
    """
    if not _LEGACY_COMPAT_COPIES_ENABLED:
        return
    try:
        write_json_atomic(path, payload)
    except OSError as exc:
        logger.warning("更新旧映射兼容副本失败（generation 已发布）: %s", exc)


def publish_mapping_bundle(
    mapping_dir: Path,
    content_payload: dict,
    images_payload: dict,
    format_payload: dict,
) -> dict[str, Path]:
    """Write a complete EPUB mapping generation and atomically publish it.

    All three members are durable before ``mapping_manifest.json`` is replaced.
    Readers that observe the manifest therefore see either the previous full
    generation or this full generation, never a partially written triple.

    B-1：成功发布后执行 generation GC，回收不可达旧目录。写入指标记录到 debug 日志。
    """
    t0 = perf_counter()
    root = Path(mapping_dir)
    root.mkdir(parents=True, exist_ok=True)
    generation, directory = _new_generation_directory(root)
    payloads = {
        "content_mapping.json": content_payload,
        "images.json": images_payload,
        "format_info.json": format_payload,
    }
    files: dict[str, str] = {}
    paths: dict[str, Path] = {}
    for filename, payload in payloads.items():
        path = directory / filename
        write_json_atomic(path, payload)
        paths[filename] = path
        files[filename] = _relative_generation_path(root, path)

    _publish_manifest(root, generation, files)

    # B-2：manifest 发布成功后 GC，避免回收尚未替换的旧 generation
    new_manifest = _load_manifest(root)
    if new_manifest is not None:
        _gc_unreachable_generations(root, new_manifest)

    for filename, payload in payloads.items():
        _write_legacy_compatibility_copy(root / filename, payload)

    elapsed = perf_counter() - t0
    logger.debug(
        "publish_mapping_bundle: generation=%s, elapsed=%.3fs, files=%d",
        generation[:8],
        elapsed,
        len(files),
    )
    return paths


def publish_mapping_file_update(
    mapping_dir: str | Path,
    filename: str,
    payload: dict,
) -> Path:
    """Publish one mapping member while retaining the other published members.

    Legacy mapping directories have no manifest and continue to use their
    top-level JSON file. Once a manifest exists, the new member is written to
    its own durable generation directory before a replacement manifest makes
    it visible. This prevents writers such as the image-asset migration from
    updating only a legacy compatibility copy.

    B-1：成功发布后执行 generation GC，回收不可达旧目录。写入指标记录到 debug 日志。
    """
    if filename not in _MAPPING_FILENAMES:
        raise ValueError(f"Unsupported mapping filename: {filename}")

    t0 = perf_counter()
    root = Path(mapping_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(root)
    if manifest is None:
        path = root / filename
        write_json_atomic(path, payload)
        return path

    # Validate every existing member before reusing its reference in a new
    # manifest. This prevents an individual update from re-publishing a
    # corrupt mixed generation.
    files = dict(manifest["files"])
    for existing_filename in _MAPPING_FILENAMES:
        if existing_filename != filename:
            resolve_mapping_file(root, existing_filename)
    generation, directory = _new_generation_directory(root)
    member_path = directory / filename
    write_json_atomic(member_path, payload)
    files[filename] = _relative_generation_path(root, member_path)
    _publish_manifest(root, generation, files)

    # B-2：manifest 发布成功后 GC
    new_manifest = _load_manifest(root)
    if new_manifest is not None:
        _gc_unreachable_generations(root, new_manifest)

    _write_legacy_compatibility_copy(root / filename, payload)

    elapsed = perf_counter() - t0
    logger.debug(
        "publish_mapping_file_update: %s, generation=%s, elapsed=%.3fs",
        filename,
        generation[:8],
        elapsed,
    )
    return member_path


def _publish_content_update(mapping_dir: Path, content_payload: dict) -> Path:
    """Publish a new content member while retaining the active image/format members."""
    return publish_mapping_file_update(mapping_dir, "content_mapping.json", content_payload)


def load_content_mapping(mapping_dir: str) -> Tuple[List[str], List[str]]:
    """加载 content_mapping.json，严格按行号顺序返回原文和译文列表。

    最可靠的对齐机制：
    - 使用 line_number 作为唯一标识符，从1开始
    - 不依赖 JSON 键的顺序（JSON 无序）
    - 不依赖外部索引，只依赖内部 line_number 字段
    - 返回格式：([原文], [译文])
    """
    md = resolve_mapping_file(mapping_dir, "content_mapping.json")
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
    - 第 i 个条目（line_number == i+1）直接对应 translated_lines[i]
    - 自动更新 translated_at 时间戳
    - 未翻译的行保持空字符串

    B-1 (审计 §P1-PERF-1)：改用 O(N) 直接索引替代原先的 O(N log N) sorted()。
    通过预先构建 {line_number: key} 字典，更新时按 line_number - 1 直接定位
    translated_lines 中的译文，无需排序。
    """
    root = Path(mapping_dir)
    md = resolve_mapping_file(root, "content_mapping.json")
    obj = json.loads(md.read_text(encoding="utf-8"))
    items: Dict[str, Dict] = obj.get("content_mappings", {})
    now = datetime.datetime.now().isoformat()

    # B-1：O(N) 直接定位：构建 line_number → key 映射，避免 sorted()
    line_to_key: Dict[int, str] = {}
    for key, item_data in items.items():
        ln = item_data.get("line_number")
        if ln is not None:
            line_to_key[int(ln)] = key

    for line_number, key in line_to_key.items():
        idx = line_number - 1
        translation = translated_lines[idx] if 0 <= idx < len(translated_lines) else ""
        old_translation = items[key].get("translated_text", "")
        items[key]["translated_text"] = translation
        if translation != old_translation:
            items[key]["translated_at"] = now

    obj["project_info"]["updated_at"] = now
    _publish_content_update(root, obj)


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

    content_file = resolve_mapping_file(mapping_dir, "content_mapping.json")
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
    """Save content mappings through the active generation when present."""
    payload = {
        "project_info": project_info,
        "content_mappings": content_mappings,
    }
    publish_mapping_file_update(mapping_dir, "content_mapping.json", payload)


def save_images_mapping(mapping_dir: Path, images_mapping: Dict[str, Dict]) -> None:
    """Save image mappings through the active generation when present."""
    payload = {"image_mappings": images_mapping}
    publish_mapping_file_update(mapping_dir, "images.json", payload)


def save_format_info(mapping_dir: Path, format_info: Dict) -> None:
    """Save format metadata through the active generation when present."""
    publish_mapping_file_update(mapping_dir, "format_info.json", format_info)
