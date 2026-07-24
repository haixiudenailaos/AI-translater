#!/usr/bin/env python3
"""
EPUB解析与映射生成模块
负责：
- 解析EPUB，提取格式信息、内容段落、图片（Base64）
- 生成mapping目录及content_mapping.json、images.json、format_info.json
- 提供译文更新与装载辅助函数
"""

import base64
import datetime
import hashlib
import importlib
import json
import logging
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Protocol,
    Sequence,
    Tuple,
    TypedDict,
    cast,
)
from zipfile import BadZipFile, ZipFile

from ..infrastructure.atomic_file import write_json_atomic as _write_json_atomic
from ..infrastructure.document_order import (
    get_item_media_type as _get_item_media_type_impl,
)
from ..infrastructure.document_order import (
    get_item_name as _get_item_name_impl,
)

# 阶段 4：委托到 infrastructure 层
from ..infrastructure.document_order import (
    iter_spine_documents as _iter_spine_documents_impl,
)
from ..infrastructure.document_order import (
    normalize_chapter_id as _normalize_chapter_id_impl,
)
from ..infrastructure.exporter import (
    compute_file_hash as _compute_file_hash_impl,
)
from ..infrastructure.exporter import (
    export_epub as _export_epub_impl,
)
from ..infrastructure.image_asset_store import IMAGE_MAPPING_SCHEMA_VERSION, save_image_binary
from ..infrastructure.image_rewriter import (
    match_and_get_new_path as _match_and_get_new_path_impl,
)
from ..infrastructure.mapping_repository import (
    load_content_mapping as _load_content_mapping_impl,
)
from ..infrastructure.mapping_repository import (
    publish_mapping_bundle,
    resolve_mapping_file,
)
from ..infrastructure.mapping_repository import (
    save_translations as _save_translations_impl,
)
from ..infrastructure.segment_extractor import (
    BLOCK_TAGS as _BLOCK_TAGS,
)
from ..infrastructure.segment_extractor import (
    compute_source_checksum as _compute_source_checksum_impl,
)
from ..utils.logger import get_logger

logger = get_logger(__name__)

_MAX_EPUB_MEMBERS = 10_000
_MAX_EPUB_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_EPUB_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_EPUB_COMPRESSION_RATIO = 1_000
# Batch-C: lightweight cache manifest to avoid parsing full images.json on cache hit
_IMPORT_CACHE_MANIFEST_FILE = "import_cache_manifest.json"


class EpubFormatInfo(TypedDict):
    """JSON-serializable metadata published together with an EPUB mapping."""

    metadata: dict[str, object]
    css_styles: dict[str, str]
    spine_order: list[str]
    toc_structure: list[dict[str, object]]
    manifest_items: dict[str, dict[str, object]]


class _EpubItem(Protocol):
    """Small structural subset used by the EPUB import pipeline."""

    def get_type(self) -> object: ...

    def get_content(self) -> bytes: ...


class _EpubBook(Protocol):
    spine: Sequence[object]
    toc: Iterable[object]

    def get_items(self) -> Iterable[_EpubItem]: ...

    def get_metadata(self, namespace: str, name: str) -> list[tuple[object, ...]]: ...

    def get_item_with_id(self, item_id: str) -> _EpubItem | None: ...


class _SoupNode(Protocol):
    name: str | None

    def find_all(self, *args: object, **kwargs: object) -> list["_SoupNode"]: ...

    def get_text(self) -> str: ...


def _runtime_dependency_symbol(module_name: str, attribute_name: str) -> object:
    """Load a runtime-only third-party symbol behind one typed boundary."""
    return getattr(importlib.import_module(module_name), attribute_name)


class EpubArchiveValidationError(ValueError):
    """The EPUB ZIP archive exceeds the application's resource boundary."""


class EpubImportCancelled(Exception):
    """Raised at EPUB import safe points after the user requests cancellation."""


class EpubImportPartialError(RuntimeError):
    """正文解析不完整时中止导入，禁止发布缺章映射。"""


def _raise_if_epub_import_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise EpubImportCancelled("EPUB 导入已取消")


def _validate_epub_archive(
    path: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    """Reject malformed, traversal-capable, or compression-bomb EPUB archives."""
    try:
        with ZipFile(path) as archive:
            _raise_if_epub_import_cancelled(cancel_requested)
            members = archive.infolist()
            if len(members) > _MAX_EPUB_MEMBERS:
                raise EpubArchiveValidationError("EPUB 条目数量超出安全上限")
            total_uncompressed = 0
            for member in members:
                _raise_if_epub_import_cancelled(cancel_requested)
                name = member.filename.replace("\\", "/")
                parts = [part for part in name.split("/") if part]
                if name.startswith("/") or ".." in parts:
                    raise EpubArchiveValidationError("EPUB 包含不安全的文件路径")
                if member.is_dir():
                    continue
                if member.file_size > _MAX_EPUB_MEMBER_BYTES:
                    raise EpubArchiveValidationError("EPUB 单个资源超出安全上限")
                total_uncompressed += member.file_size
                if total_uncompressed > _MAX_EPUB_TOTAL_BYTES:
                    raise EpubArchiveValidationError("EPUB 解压总大小超出安全上限")
                if (
                    member.file_size > 0
                    and member.compress_size > 0
                    and member.file_size / member.compress_size > _MAX_EPUB_COMPRESSION_RATIO
                ):
                    raise EpubArchiveValidationError("EPUB 资源压缩比异常")
    except BadZipFile as exc:
        raise EpubArchiveValidationError("文件不是有效的 EPUB/ZIP") from exc


class EPUBProcessor:
    # 阶段 4：BLOCK_TAGS 委托到 segment_extractor
    BLOCK_TAGS = _BLOCK_TAGS

    def __init__(self, app_paths=None, *, storage_paths=None):
        # BUG-001：通过 AppPaths 接收统一工作区目录，避免依赖当前工作目录
        if app_paths is not None:
            self._workspace_dir = Path(app_paths.workspace_dir)
        else:
            self._workspace_dir = Path.cwd() / "workspace"
        # STORAGE-3：EPUB 映射根目录统一来自 ResolvedStoragePaths.mappings_dir，
        # 不再自行拼接 workspace_dir/"mappings"。未注入 storage_paths 时
        # 保持旧行为（兼容旧测试与直接构造的调用方）。
        if storage_paths is not None:
            self._mappings_root = Path(storage_paths.mappings_dir)
        else:
            self._mappings_root = self._workspace_dir / "mappings"

    # ── BUG-003：稳定项目标识 ─────────────────────────

    @staticmethod
    def _compute_project_id(epub_path: Path) -> str:
        """计算稳定的 EPUB 项目 ID。

        格式：<安全文件名>-<源文件绝对路径哈希前 12 位>
        不同目录下的同名 EPUB 会生成不同 ID，避免跨书覆盖。
        """
        safe_stem = "".join(c for c in epub_path.stem if c.isalnum() or c in ("-", "_")) or "epub"
        try:
            abs_path_str = str(epub_path.resolve())
        except OSError:
            abs_path_str = str(epub_path.absolute())
        path_hash = hashlib.sha256(abs_path_str.encode("utf-8")).hexdigest()[:12]
        return f"{safe_stem}-{path_hash}"

    @staticmethod
    def _source_checksum(text: str) -> str:
        """计算原文段的短校验和，用于稳定定位符。"""
        return _compute_source_checksum_impl(text)

    @staticmethod
    def _compute_file_hash(path: Path) -> str:
        """计算文件内容的 SHA256 哈希（R2-BUG-006）。

        用于导出前验证源 EPUB 是否被修改。相比 size+mtime，
        内容哈希能可靠检测文件替换或内容编辑。
        """
        return _compute_file_hash_impl(path)

    # ── BUG-007：按 spine 处理阅读顺序 ─────────────────

    def iter_spine_documents(
        self, book, item_by_id: Mapping[str, object] | None = None
    ) -> Iterator:
        """按 spine 阅读顺序遍历文档项目（委托到 infrastructure.document_order）。

        R2-BUG-001 修复：
        - 兼容字符串 idref、带 idref 属性的对象和文档对象三种 spine 形态。
        - 明确处理 linear="no"：非线性条目不在主阅读流中，跳过并记录 debug。
        - 调用方应在 spine 非空但未产出任何文档时抛出异常，避免空内容映射。
        """
        yield from _iter_spine_documents_impl(book, item_by_id)

    @staticmethod
    def _normalize_chapter_id(name: str) -> str:
        """规范化章节 ID（委托到 infrastructure.document_order）。"""
        return _normalize_chapter_id_impl(name)

    @staticmethod
    def _get_item_media_type(item) -> str:
        """安全地获取 EpubItem 的 media_type（委托到 infrastructure.document_order）。"""
        return _get_item_media_type_impl(item)

    @staticmethod
    def _get_item_name(item) -> str:
        """安全地获取 EpubItem 的文件名（委托到 infrastructure.document_order）。"""
        return _get_item_name_impl(item)

    def import_epub(
        self,
        epub_path: str,
        extract_images: bool = True,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, str]:
        """解析EPUB并生成mapping目录与三个映射文件。

        ``cancel_requested`` is checked at archive, chapter, image, and write
        boundaries. Cancellation raises ``EpubImportCancelled`` so callers can
        distinguish it from malformed input.

        返回：{"mapping_dir": str, "content_file": str, "images_file": str, "format_file": str}
        """
        _raise_if_epub_import_cancelled(cancel_requested)
        try:
            # ebooklib and BeautifulSoup do not ship complete type information.
            # Keep their dynamic API boundary here instead of leaking it through
            # the mapping and persistence code below.
            read_epub = cast(
                Callable[[str], _EpubBook],
                _runtime_dependency_symbol("ebooklib.epub", "read_epub"),
            )
            beautiful_soup = cast(
                Callable[[str, str], _SoupNode],
                _runtime_dependency_symbol("bs4", "BeautifulSoup"),
            )
            item_style_type = _runtime_dependency_symbol("ebooklib", "ITEM_STYLE")
            item_image_type = _runtime_dependency_symbol("ebooklib", "ITEM_IMAGE")
        except (ImportError, AttributeError):
            raise Exception("需要安装ebooklib和beautifulsoup4库来支持EPUB文件解析")

        epub_path_obj = Path(epub_path)
        if not epub_path_obj.exists() or epub_path_obj.suffix.lower() != ".epub":
            raise Exception("文件不存在或不是EPUB格式")
        if progress_callback is not None:
            progress_callback("正在检查 EPUB 文件...")
        _validate_epub_archive(epub_path_obj, cancel_requested=cancel_requested)
        _raise_if_epub_import_cancelled(cancel_requested)

        # 为每个EPUB创建独立的映射子文件夹
        # BUG-003：使用稳定项目 ID（文件名+路径哈希），不同目录下同名 EPUB 生成不同工作区
        project_id = self._compute_project_id(epub_path_obj)

        # STORAGE-3：映射根目录由构造时注入（默认等于 workspace_dir/mappings）
        mapping_root = self._mappings_root
        mapping_dir = mapping_root / project_id
        mapping_dir.mkdir(parents=True, exist_ok=True)

        # PERF-6e：源文件未变化时跳过昂贵的 EPUB 重解析。
        # 用 size + mtime 快速验证；命中已有 mapping 直接返回，不重新
        # 解析 ebooklib、不重提取图片、不重建 format_info。
        # 翻译进度保存在 content_mapping.json 中，不受跳过影响。
        content_file = resolve_mapping_file(mapping_dir, "content_mapping.json")
        images_file = resolve_mapping_file(mapping_dir, "images.json")
        format_file = resolve_mapping_file(mapping_dir, "format_info.json")
        cache_manifest_file = mapping_dir / _IMPORT_CACHE_MANIFEST_FILE
        # Batch-C: read only the lightweight manifest (no images.json parse on hit)
        if (
            content_file.exists()
            and images_file.exists()
            and format_file.exists()
            and cache_manifest_file.exists()
        ):
            try:
                manifest = json.loads(cache_manifest_file.read_text(encoding="utf-8"))
                stat = epub_path_obj.stat()
                if (
                    manifest.get("source_file_size") == stat.st_size
                    and manifest.get("source_file_mtime") == stat.st_mtime
                    and manifest.get("schema_version") == IMAGE_MAPPING_SCHEMA_VERSION
                ):
                    logger.info(
                        "[import_epub] 源文件未变化（size=%s, mtime=%s），跳过重解析",
                        stat.st_size,
                        stat.st_mtime,
                    )
                    return {
                        "mapping_dir": str(mapping_dir),
                        "content_file": str(content_file),
                        "images_file": str(images_file),
                        "format_file": str(format_file),
                    }
            except Exception as exc:
                logger.debug("[import_epub] 缓存验证失败，回退到全量解析: %s", exc)

        _raise_if_epub_import_cancelled(cancel_requested)
        if progress_callback is not None:
            progress_callback("正在读取 EPUB 结构...")
        # 读取书籍
        book = read_epub(str(epub_path_obj))
        _raise_if_epub_import_cancelled(cancel_requested)

        # P2-PERF-9：EPUB items 在 manifest、CSS 和图片阶段都会被使用。
        # ebooklib 每次 ``get_items()`` 都会重新遍历内部容器；在大型 EPUB
        # 中先建立一次索引可避免三次完整扫描，同时保留各阶段原有处理顺序。
        all_items = tuple(book.get_items())
        item_by_id = {
            str(item_id): item for item in all_items if (item_id := getattr(item, "id", None))
        }

        # 数据容器
        content_mappings: Dict[str, Dict] = {}
        images_mapping: Dict[str, Dict] = {}
        format_info: EpubFormatInfo = {
            "metadata": {},
            "css_styles": {},
            "spine_order": [],
            "toc_structure": [],
            "manifest_items": {},
        }

        # 元数据
        try:
            # 常见DC元数据
            md: dict[str, object] = {}
            for tag in [
                "title",
                "creator",
                "language",
                "identifier",
                "publisher",
                "date",
                "description",
            ]:
                vals = book.get_metadata("DC", tag)
                if vals:
                    # 取第一个值
                    md[tag] = vals[0][0]
            format_info["metadata"] = md
        except (KeyError, AttributeError):
            pass  # 最佳努力：提取 EPUB 元数据，不同版本 ebooklib 字段差异

        # 清点manifest与spine
        try:
            # manifest
            for item in all_items:
                _raise_if_epub_import_cancelled(cancel_requested)
                try:
                    name = self._get_item_name(item)
                    if name:
                        format_info["manifest_items"][name] = {
                            "media_type": self._get_item_media_type(item),
                            "properties": getattr(item, "properties", None),
                        }
                except EpubImportCancelled:
                    raise
                except Exception as e:
                    print(f"⚠ 警告：解析manifest条目失败: {e}")
                    continue
        except EpubImportCancelled:
            raise
        except Exception as e:
            print(f"⚠ 警告：提取manifest时发生错误: {e}")

        # spine（顺序）- 健壮的多版本兼容处理（单独try以确保即使manifest失败也能提取spine）
        try:
            spine_order = []
            for item in book.spine:
                _raise_if_epub_import_cancelled(cancel_requested)
                try:
                    # 提取itemref（处理不同版本的spine格式）
                    if isinstance(item, tuple):
                        # 新版本：(itemref, linear)
                        itemref = item[0] if len(item) > 0 else None
                    else:
                        # 旧版本：直接是itemref
                        itemref = item

                    if not itemref:
                        continue

                    # 获取文件名（尝试多种属性）
                    name = self._get_item_name(itemref)
                    if not name and isinstance(itemref, str):
                        name = itemref

                    # 如果是对象，尝试从book.items中查找
                    item_id = getattr(itemref, "idref", None)
                    if not name and isinstance(item_id, str):
                        try:
                            item_obj = book.get_item_with_id(item_id)
                            if item_obj:
                                name = self._get_item_name(item_obj)
                        except (KeyError, AttributeError):
                            pass

                    if name:
                        normalized_name = self._normalize_chapter_id(name)
                        spine_order.append(normalized_name)
                except (KeyError, AttributeError, TypeError):
                    # 单个spine条目失败不影响其他条目的提取
                    continue

            # 【关键修复】只有在spine_order非空时才赋值，否则发出警告
            if spine_order:
                format_info["spine_order"] = spine_order
            else:
                # 如果spine提取失败，至少记录警告，并尝试从content_mappings推断
                print("⚠ 警告：无法从EPUB提取spine_order，将从文档内容推断章节顺序")
                format_info["spine_order"] = []  # 保持为空列表，后续会从mappings推断
        except EpubImportCancelled:
            raise
        except Exception as e:
            # spine提取异常也要记录，不能静默失败
            print(f"⚠ 警告：提取spine_order时发生错误: {e}")
            format_info["spine_order"] = []

        # TOC结构（简化保存：标题、href、层级）
        try:

            def _flatten_toc(toc, level=1):
                for entry in toc:
                    _raise_if_epub_import_cancelled(cancel_requested)
                    try:
                        title = entry.title if hasattr(entry, "title") else str(entry)
                        href = entry.href if hasattr(entry, "href") else None
                        format_info["toc_structure"].append(
                            {"title": title, "href": href, "level": level}
                        )
                        if hasattr(entry, "children") and entry.children:
                            _flatten_toc(entry.children, level + 1)
                    except EpubImportCancelled:
                        raise
                    except Exception:
                        continue

            _flatten_toc(book.toc, 1)
        except EpubImportCancelled:
            raise
        except Exception:
            pass

        # 提取CSS样式
        try:
            for item in all_items:
                _raise_if_epub_import_cancelled(cancel_requested)
                if item.get_type() == item_style_type:
                    name = self._get_item_name(item)
                    if name:
                        try:
                            format_info["css_styles"][name] = item.get_content().decode(
                                "utf-8", errors="ignore"
                            )
                        except Exception:
                            format_info["css_styles"][name] = base64.b64encode(
                                item.get_content()
                            ).decode("ascii")
        except EpubImportCancelled:
            raise
        except Exception:
            pass

        # 【关键修复】检查是否已存在旧的翻译数据，以便保留翻译进度
        # BUG-003：使用稳定定位符优先匹配，降级到原文匹配
        existing_translations = {}  # 按原文文本匹配（降级用）
        existing_by_locator = {}  # 按稳定定位符匹配（优先用）
        existing_by_chapter_seq = {}  # 按 chapter_id+block_index 匹配（次优先）
        content_file = resolve_mapping_file(mapping_dir, "content_mapping.json")
        if content_file.exists():
            try:
                old_data = json.loads(content_file.read_text(encoding="utf-8"))
                old_mappings = old_data.get("content_mappings", {})
                for _key, item in old_mappings.items():
                    _raise_if_epub_import_cancelled(cancel_requested)
                    original = item.get("original_text", "")
                    translated = item.get("translated_text", "")
                    translated_at = item.get("translated_at", "")
                    if not (original and translated):
                        continue
                    record = {"translated_text": translated, "translated_at": translated_at}

                    # 优先：稳定定位符（chapter_id + block_index + source_checksum）
                    chapter_id = item.get("chapter_id", "")
                    block_index = item.get("block_index")
                    checksum = item.get("source_checksum", "")
                    if chapter_id and block_index is not None and checksum:
                        locator = f"{chapter_id}|{block_index}|{checksum}"
                        existing_by_locator[locator] = record

                    # 次优先：chapter_id + block_index
                    # R2-BUG-004：必须附带 checksum 和原文，位置降级匹配时校验
                    if chapter_id and block_index is not None:
                        seq_key = f"{chapter_id}|{block_index}"
                        existing_by_chapter_seq[seq_key] = {
                            **record,
                            "source_checksum": checksum,
                            "original_text": original,
                        }

                    # 降级：原文文本（仅当全书中原文唯一时可靠）
                    existing_translations[original] = record

                print(f"✓ 检测到已有翻译数据，已保留 {len(existing_translations)} 条翻译记录")
            except EpubImportCancelled:
                raise
            except Exception as e:
                print(f"⚠ 警告：读取旧翻译数据失败: {e}")
                existing_translations = {}

        # 提取文档内容为段落映射（严格按全局行号顺序）
        # BUG-007：使用 iter_spine_documents 按 spine 阅读顺序遍历文档
        global_line_number = 1  # 全局行号，从1开始
        # 记录原文出现次数，用于判断唯一性（降级匹配安全性）
        text_occurrence_count: Dict[str, int] = {}
        # R2-BUG-005：检测章节 ID 冲突
        seen_chapter_ids: set = set()
        # R2-BUG-001：记录 spine 是否非空（用于遍历后校验）
        spine_non_empty = len(book.spine) > 0

        for chapter_index, doc_item in enumerate(
            self.iter_spine_documents(book, item_by_id), start=1
        ):
            _raise_if_epub_import_cancelled(cancel_requested)
            if progress_callback is not None:
                progress_callback(f"正在解析第 {chapter_index} 章...")
            # R2-BUG-005：检测章节 ID 冲突（在 try 块外执行，避免被宽泛 except 吞掉）
            base_name = self._normalize_chapter_id(self._get_item_name(doc_item))
            if base_name in seen_chapter_ids:
                raise Exception(
                    f"章节 ID 冲突: '{base_name}'，多个章节归一化后 ID 重复，"
                    f"无法生成唯一映射。请检查 EPUB 目录结构。"
                )
            seen_chapter_ids.add(base_name)

            try:
                html = doc_item.get_content().decode("utf-8", errors="ignore")
                soup = beautiful_soup(html, "html.parser")

                block_index_in_chapter = 0  # BUG-003：章节内块索引，用于稳定定位符

                # 按文档真实顺序遍历所有节点，筛选块级标签
                for node_index, node in enumerate(soup.find_all(True), start=1):
                    if node_index % 128 == 1:
                        _raise_if_epub_import_cancelled(cancel_requested)
                    try:
                        if node.name in self.BLOCK_TAGS:
                            # 检查是否为叶子块节点（避免重复提取嵌套内容）
                            has_block_children = any(
                                child.name in self.BLOCK_TAGS
                                for child in node.find_all(True, recursive=False)
                            )
                            if has_block_children:
                                continue

                            # 提取文本（递归获取所有文本，因为此时确认没有块级子标签）
                            text = (node.get_text() or "").strip()
                            if text:
                                # 使用全局行号作为键（采用6位数字填充）
                                cid = f"line_{global_line_number:06d}"

                                # BUG-003：计算稳定定位符
                                checksum = self._source_checksum(text)
                                locator = f"{base_name}|{block_index_in_chapter}|{checksum}"

                                # BUG-003：按优先级匹配旧译文
                                translated_text = ""
                                translated_at = ""
                                matched_record = None
                                # 1. 稳定定位符
                                if locator in existing_by_locator:
                                    matched_record = existing_by_locator[locator]
                                # 2. chapter_id + block_index（R2-BUG-004：位置降级必须校验原文）
                                elif (
                                    f"{base_name}|{block_index_in_chapter}"
                                    in existing_by_chapter_seq
                                ):
                                    candidate = existing_by_chapter_seq[
                                        f"{base_name}|{block_index_in_chapter}"
                                    ]
                                    # 校验 checksum 或规范化原文一致，防止原文变化后复用旧译文
                                    if (
                                        candidate.get("source_checksum") == checksum
                                        or candidate.get("original_text", "").strip()
                                        == text.strip()
                                    ):
                                        matched_record = candidate
                                    else:
                                        logger.warning(
                                            "位置降级匹配失败（原文已变化）: %s|%s, 旧 checksum=%s, 新 checksum=%s",
                                            base_name,
                                            block_index_in_chapter,
                                            candidate.get("source_checksum"),
                                            checksum,
                                        )
                                # 3. 原文匹配（降级，需后续验证唯一性）
                                elif text in existing_translations:
                                    text_occurrence_count[text] = (
                                        text_occurrence_count.get(text, 0) + 1
                                    )
                                    matched_record = existing_translations[text]

                                if matched_record:
                                    translated_text = matched_record["translated_text"]
                                    translated_at = matched_record["translated_at"]

                                content_mappings[cid] = {
                                    "original_text": text,
                                    "translated_text": translated_text,  # 保留已有翻译
                                    "line_number": global_line_number,
                                    "chapter_id": base_name,
                                    "block_index": block_index_in_chapter,  # BUG-003：章节内块索引
                                    "source_checksum": checksum,  # BUG-003：原文校验和
                                    "translated_at": translated_at,  # 保留翻译时间戳
                                }
                                global_line_number += 1
                                block_index_in_chapter += 1
                    except EpubImportCancelled:
                        raise
                    except Exception as exc:
                        raise EpubImportPartialError(
                            f"第 {chapter_index} 章的正文节点解析失败，已取消导入以避免缺失内容"
                        ) from exc
            except EpubImportCancelled:
                raise
            except EpubImportPartialError:
                raise
            except Exception as exc:
                raise EpubImportPartialError(
                    f"第 {chapter_index} 章解析失败，已取消导入以避免生成缺章映射"
                ) from exc

        # R2-BUG-001：spine 非空但未解析出任何正文时，导入必须失败
        # 不能生成"成功但空内容"的映射
        if spine_non_empty and not content_mappings:
            raise Exception(
                "EPUB spine 非空但未解析出任何正文内容，可能 spine 条目均为字符串 idref "
                "且无法通过 book.get_item_with_id 解析，或所有条目均为非线性。"
            )

        # BUG-003：降级匹配安全性——对非唯一原文的降级匹配清空译文并记录警告
        if existing_translations and text_occurrence_count:
            non_unique_texts = {t for t, c in text_occurrence_count.items() if c > 1}
            if non_unique_texts:
                cleared = 0
                for _cid, item in content_mappings.items():
                    original = item.get("original_text", "")
                    if original in non_unique_texts:
                        # 仅清空通过降级匹配（无定位符命中）的译文
                        # 定位符命中的译文不受影响（已通过 locator/seq 验证）
                        locator = f"{item.get('chapter_id', '')}|{item.get('block_index')}|{item.get('source_checksum', '')}"
                        seq_key = f"{item.get('chapter_id', '')}|{item.get('block_index')}"
                        if (
                            locator not in existing_by_locator
                            and seq_key not in existing_by_chapter_seq
                            and item.get("translated_text")
                        ):
                            item["translated_text"] = ""
                            cleared += 1
                if cleared:
                    logger.warning(
                        "发现 %d 条非唯一原文，已清空 %d 条降级匹配译文以防串书",
                        len(non_unique_texts),
                        cleared,
                    )

        # 图片Base64映射
        if extract_images:
            try:
                logger.debug("[import_epub] ====== 开始提取EPUB图片 ======")
                print("📷 开始提取EPUB图片...")
                image_count = 0

                # P2-PERF-9：复用导入开始时建立的一次 item 索引。
                total_items = len(all_items)
                logger.debug("[import_epub] EPUB共有 %d 个item", total_items)

                for idx, item in enumerate(all_items):
                    _raise_if_epub_import_cancelled(cancel_requested)
                    if progress_callback is not None and idx % 16 == 0:
                        progress_callback(f"正在提取图片（{idx + 1}/{total_items}）...")
                    try:
                        item_type = item.get_type()
                        media_type = self._get_item_media_type(item)
                        item_name = self._get_item_name(item)

                        logger.debug(
                            "[import_epub] 检查item %d/%d: name=%s, type=%s, media_type=%s",
                            idx + 1,
                            total_items,
                            item_name,
                            item_type,
                            media_type,
                        )

                        # 检查是否是图片
                        is_image = False
                        if item_type == item_image_type:
                            logger.debug(
                                "[import_epub] item_type == ebooklib.ITEM_IMAGE，判定为图片"
                            )
                            is_image = True
                        elif media_type and media_type.startswith("image/"):
                            logger.debug(
                                "[import_epub] media_type.startswith('image/')，判定为图片"
                            )
                            is_image = True

                        if is_image:
                            name = self._get_item_name(item)
                            if not name:
                                logger.warning("[import_epub] 图片item缺少name，跳过")
                                continue
                            data = item.get_content()
                            mime = media_type or "image/png"
                            # 保留 V1.5 的 Base64 原图作为透明加密环境下的可靠回退，
                            # 同时继续写入二进制资产供正常环境按需使用。
                            images_mapping[name] = save_image_binary(
                                mapping_dir, image_count, name, data, mime
                            )
                            logger.debug(
                                "[import_epub] 图片信息: name=%s, size=%d bytes, mime=%s",
                                name,
                                len(data),
                                mime,
                            )
                            image_count += 1
                            print(f"  ✓ 提取图片: {name} ({len(data)} bytes)")
                        else:
                            logger.debug("[import_epub] 非图片item，跳过: name=%s", item_name)
                    except EpubImportCancelled:
                        raise
                    except Exception as e:
                        logger.error(
                            "[import_epub] 提取单个图片失败: %s: %s",
                            type(e).__name__,
                            e,
                            exc_info=True,
                        )
                        print(f"⚠ 警告：提取单个图片失败: {e}")
                        continue

                logger.debug("[import_epub] ====== 图片提取完成 ======")
                logger.debug("[import_epub] 成功提取 %d 张图片", image_count)
                logger.debug("[import_epub] images_mapping包含 %d 个条目", len(images_mapping))
                print(f"✅ 图片提取完成，共 {image_count} 张图片")
            except EpubImportCancelled:
                raise
            except Exception as e:
                logger.error(
                    "[import_epub] 提取图片时发生错误: %s: %s", type(e).__name__, e, exc_info=True
                )
                print(f"⚠ 警告：提取图片时发生错误: {e}")

        # 写入文件
        logger.debug("[import_epub] ====== 开始写入文件 ======")
        content_file = mapping_dir / "content_mapping.json"
        images_file = mapping_dir / "images.json"
        format_file = mapping_dir / "format_info.json"
        logger.debug("[import_epub] 输出文件:")
        logger.debug("[import_epub]   - content_file: %s", content_file)
        logger.debug("[import_epub]   - images_file: %s", images_file)
        logger.debug("[import_epub]   - format_file: %s", format_file)

        project_info = {
            "project_id": project_id,  # BUG-003：稳定项目 ID
            "original_file": str(epub_path_obj),
            "source_file_size": epub_path_obj.stat().st_size if epub_path_obj.exists() else 0,
            "source_file_mtime": epub_path_obj.stat().st_mtime if epub_path_obj.exists() else 0,
            # R2-BUG-006：保存内容哈希，导出前验证源文件未变化
            "source_content_hash": self._compute_file_hash(epub_path_obj)
            if epub_path_obj.exists()
            else "",
            "created_at": datetime.datetime.now().isoformat(),
            "updated_at": datetime.datetime.now().isoformat(),
        }

        content_payload = {"project_info": project_info, "content_mappings": content_mappings}
        logger.debug("[import_epub] content_payload: %d 个content_mappings", len(content_mappings))

        images_payload = {
            "schema_version": IMAGE_MAPPING_SCHEMA_VERSION,
            "image_mappings": images_mapping,
        }
        logger.debug("[import_epub] images_payload: %d 个image_mappings", len(images_mapping))

        # 【关键修复】如果spine_order为空，从content_mappings推断章节顺序
        if not format_info.get("spine_order"):
            logger.warning("[import_epub] spine_order为空，正在从content_mappings推断章节顺序...")
            print("⚠ spine_order为空，正在从content_mappings推断章节顺序...")
            # 提取所有章节ID并排序
            chapters = set()
            for item in content_mappings.values():
                chapter = item.get("chapter_id", "")
                if chapter:
                    chapters.add(chapter)

            # 按文件名自然顺序排序（通常与p-0001, p-0002...的命名规则匹配）
            inferred_spine = sorted(chapters)
            format_info["spine_order"] = inferred_spine
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[import_epub] 已推断 %d 个章节: %s", len(inferred_spine), inferred_spine
                )
            print(f"✓ 已推断 {len(inferred_spine)} 个章节（按文件名排序）")
            print("⚠ 建议：使用 tools/fix_spine_order.py 从原EPUB提取精确的spine顺序")

        _raise_if_epub_import_cancelled(cancel_requested)
        if progress_callback is not None:
            progress_callback("正在保存导入映射...")
        # 所有 generation 成员先写完，最后原子发布 manifest。顶层 JSON 只是
        # 兼容副本；新的读取端只接受 manifest 指向的完整 generation。
        logger.debug("[import_epub] 发布完整 mapping generation...")
        publish_mapping_bundle(mapping_dir, content_payload, images_payload, format_info)
        content_file = mapping_dir / "content_mapping.json"
        images_file = mapping_dir / "images.json"
        format_file = mapping_dir / "format_info.json"
        logger.debug("[import_epub] ====== mapping generation 发布完成 ======")
        # Batch-C: write lightweight manifest for fast cache hit (avoids parsing images.json)
        try:
            _write_json_atomic(
                mapping_dir / _IMPORT_CACHE_MANIFEST_FILE,
                {
                    "source_file_size": project_info["source_file_size"],
                    "source_file_mtime": project_info["source_file_mtime"],
                    "schema_version": IMAGE_MAPPING_SCHEMA_VERSION,
                    "image_count": len(images_mapping),
                },
            )
        except OSError as _cache_exc:
            logger.warning(
                "[import_epub] 写入 cache manifest 失败，下次导入将重解析: %s", _cache_exc
            )

        result = {
            "mapping_dir": str(mapping_dir),
            "content_file": str(content_file),
            "images_file": str(images_file),
            "format_file": str(format_file),
        }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[import_epub] 返回结果: %s", result)
        return result

    def load_content_mapping(self, mapping_dir: str) -> Tuple[List[str], List[str]]:
        """加载content_mapping，严格按行号顺序返回原文和译文列表（委托到 infrastructure.mapping_repository）。

        最可靠的对齐机制：
        - 使用 line_number 作为唯一标识符，从1开始
        - 不依赖 JSON 键的顺序（JSON 无序）
        - 不依赖外部索引，只依赖内部 line_number 字段
        - 返回格式：([原文], [译文])
        """
        return _load_content_mapping_impl(mapping_dir)

    def save_translations(self, mapping_dir: str, translated_lines: List[str]) -> None:
        """将译文列表按行号严格对齐保存到content_mapping.json（委托到 infrastructure.mapping_repository）。

        - 不修改原有的 line_number（保持绝对稳定）
        - 按 line_number 排序后，第 i 个条目对应 translated_lines[i]
        - 自动更新 translated_at 时间戳
        - 未翻译的行保持空字符串
        """
        _save_translations_impl(mapping_dir, translated_lines)

    def _match_and_get_new_path(
        self, src: str, path_mapping: Dict[str, str], doc_dir: Path
    ) -> tuple:
        """匹配图片路径并返回新的相对路径（委托到 infrastructure.image_rewriter）。

        Returns:
            (matched: bool, new_rel_path: str)
        """
        return _match_and_get_new_path_impl(src, path_mapping, doc_dir)

    def export_epub(
        self,
        mapping_dir: str,
        output_path: str,
        image_map: Dict[str, str] | None = None,
        image_text_map: Dict[str, Dict[str, object]] | None = None,
    ) -> str:
        """根据mapping重建并导出EPUB（委托到 infrastructure.exporter）。

        改进说明：
        1. 严格按line_number全局顺序读取译文
        2. 确保第N行译文替换第N个原文段落
        3. 没有译文的段落保留原文
        4. 修复图片前后的多余空白问题
        5. 修正特殊格式标题识别问题
        6. 支持插图翻译替换（新增）
        7. 支持图片文字翻译注释（figcaption）

        返回输出文件路径
        """
        return _export_epub_impl(mapping_dir, output_path, image_map, image_text_map)
