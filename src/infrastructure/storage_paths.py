#!/usr/bin/env python3
"""统一数据目录解析与校验服务（STORAGE-2）。

对应 docs/USER_CUSTOMIZABLE_DATA_DIRECTORIES_IMPLEMENTATION_GUIDE.md §3.3 / §5
第一、二步：所有“缓存 / 翻译中间记录 / 译文备份 / 数据根目录”的最终路径
只由本模块计算，UI、翻译队列和导出器不得各自拼接字符串。

路径解析优先级（§3.3）::

    显式子目录配置 > data_root 下的默认子目录 > 平台默认 AppPaths 目录

调用约定：
- :meth:`StoragePathResolver.resolve` 只做纯解析，不触碰磁盘，可安全用于
  设置页实时预览。
- :meth:`StoragePathResolver.validate` 做语义校验（冲突、非法字符、可写性
  预检），返回 issue 列表而非抛异常，便于 UI 聚合展示。
- :meth:`StoragePathResolver.ensure_directories` 实际创建目录，失败抛出
  结构化 :class:`StoragePathError`。

默认模式（storage 配置全空）下，解析结果必须与历史版本完全一致：
``projects_dir = data_dir/projects``、``mappings_dir = workspace_dir/mappings``，
保证旧数据解释方式不变（§8 兼容性）。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..config.storage_config import normalize_storage_config

logger = logging.getLogger(__name__)

#: data_root 下各功能的默认子目录名（§3.1，可调整，集中在此避免散落）。
DEFAULT_CACHE_SUBDIR = "cache"
DEFAULT_RECORDS_SUBDIR = "translation_records"
DEFAULT_BACKUPS_SUBDIR = "translation_backups"
PROJECTS_SUBDIR = "projects"
MAPPINGS_SUBDIR = "mappings"

#: 磁盘剩余空间低于该值时给出 warning（不阻断）。
LOW_DISK_SPACE_BYTES = 512 * 1024 * 1024

#: Windows 文件名非法字符（驱动器冒号单独处理）。
_WINDOWS_FORBIDDEN_CHARS = '<>"|?*'


class StoragePathError(RuntimeError):
    """数据目录相关的结构化错误。

    Attributes:
        field: 出错的配置字段（如 ``data_root`` / ``cache_dir``）。
        path: 出错的路径（字符串形式，可为空）。
        reason: 可操作的中文原因描述。
    """

    def __init__(self, field: str, path: str, reason: str):
        self.field = field
        self.path = path
        self.reason = reason
        location = f"（{path}）" if path else ""
        super().__init__(f"{field}{location}: {reason}")


@dataclass(frozen=True)
class StoragePathIssue:
    """一条路径校验问题（§5 第二步：校验结果聚合返回，而非遇到即抛）。"""

    field: str
    path: str
    message: str
    severity: str = "error"  # "error" 阻断保存；"warning" 仅提示

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class ResolvedStoragePaths:
    """解析后的数据目录集合（不可变，§5 第一步第 5 点）。

    业务模块只读取本对象的属性，不直接读取 storage 配置 dict。
    """

    data_root: Path
    cache_dir: Path
    translation_records_dir: Path
    translation_backups_dir: Path
    projects_dir: Path
    mappings_dir: Path
    #: None 表示日志继续使用 AppPaths.log_dir（本版本日志不跟随 data_root）。
    logs_dir: Path | None
    #: True 表示全部使用平台默认目录（storage 配置未做任何自定义）。
    is_default: bool

    def managed_directories(self) -> List[Tuple[str, Path]]:
        """返回本应用负责创建/迁移的目录（字段名, 路径）列表。

        ``data_root`` 是父目录不列入；``logs_dir`` 为 None 时跳过。
        """
        dirs: List[Tuple[str, Path]] = [
            ("cache_dir", self.cache_dir),
            ("translation_records_dir", self.translation_records_dir),
            ("translation_backups_dir", self.translation_backups_dir),
            ("projects_dir", self.projects_dir),
            ("mappings_dir", self.mappings_dir),
        ]
        if self.logs_dir is not None:
            dirs.append(("logs_dir", self.logs_dir))
        return dirs

    def display_items(self) -> List[Tuple[str, str]]:
        """供设置页预览的（中文标签, 路径字符串）列表。"""
        items = [
            ("数据根目录", str(self.data_root)),
            ("缓存目录", str(self.cache_dir)),
            ("翻译中间记录目录", str(self.translation_records_dir)),
            ("译文备份目录", str(self.translation_backups_dir)),
            ("TXT 项目目录", str(self.projects_dir)),
            ("EPUB 映射目录", str(self.mappings_dir)),
        ]
        if self.logs_dir is not None:
            items.append(("日志目录", str(self.logs_dir)))
        return items


class StoragePathResolver:
    """storage 配置 → 最终路径的统一解析器。

    构造时注入平台默认 :class:`~src.app_paths.AppPaths`，解析结果中的
    “平台默认”分支全部来自该对象，保证与现有启动行为一致。
    """

    def __init__(self, app_paths):
        self._app_paths = app_paths

    # ── 解析 ────────────────────────────────────────

    def resolve(self, storage: Dict[str, Any] | None) -> ResolvedStoragePaths:
        """把 storage 配置段解析为最终路径（纯函数，不触碰磁盘）。

        用户输入的非空路径在这里完成规范化：展开环境变量、展开 ``~``、
        转绝对路径并 ``resolve()``。相对路径直接抛出
        :class:`StoragePathError`（§1：路径必须以绝对路径保存，避免
        不同工作目录启动产生不同结果）。
        """
        cfg = normalize_storage_config(storage)
        app = self._app_paths

        data_root_raw = cfg["data_root"]
        data_root = (
            self.normalize_user_path("data_root", data_root_raw)
            if data_root_raw
            else Path(app.data_dir).resolve()
        )
        custom_root = bool(data_root_raw)

        cache_dir = self._resolve_subdir(
            cfg,
            "cache_dir",
            fallback=data_root / DEFAULT_CACHE_SUBDIR if custom_root else Path(app.data_dir) / DEFAULT_CACHE_SUBDIR,
        )

        records_raw = cfg["translation_records_dir"]
        if records_raw:
            records_dir = self.normalize_user_path("translation_records_dir", records_raw)
        elif custom_root:
            records_dir = data_root / DEFAULT_RECORDS_SUBDIR
        else:
            # 默认模式：projects 历史存放在 data_dir/projects，逻辑容器即
            # data_dir 本身，保证 projects_dir 解析结果与旧版本一致。
            records_dir = Path(app.data_dir).resolve()
        records_dir = records_dir.resolve()

        backups_dir = self._resolve_subdir(
            cfg,
            "translation_backups_dir",
            fallback=(
                data_root / DEFAULT_BACKUPS_SUBDIR
                if custom_root
                else Path(app.data_dir) / DEFAULT_BACKUPS_SUBDIR
            ),
        )

        logs_raw = cfg["logs_dir"]
        logs_dir = (
            self.normalize_user_path("logs_dir", logs_raw).resolve() if logs_raw else None
        )

        projects_dir = (records_dir / PROJECTS_SUBDIR).resolve()
        if records_raw or custom_root:
            mappings_dir = (records_dir / MAPPINGS_SUBDIR).resolve()
        else:
            # 默认模式：EPUB 映射历史存放在 workspace_dir/mappings。
            mappings_dir = (Path(app.workspace_dir) / MAPPINGS_SUBDIR).resolve()

        is_default = not any(
            cfg[field]
            for field in (
                "data_root",
                "cache_dir",
                "translation_records_dir",
                "translation_backups_dir",
                "logs_dir",
            )
        )

        return ResolvedStoragePaths(
            data_root=data_root,
            cache_dir=cache_dir.resolve(),
            translation_records_dir=records_dir,
            translation_backups_dir=backups_dir.resolve(),
            projects_dir=projects_dir,
            mappings_dir=mappings_dir,
            logs_dir=logs_dir,
            is_default=is_default,
        )

    def _resolve_subdir(self, cfg: Dict[str, Any], field: str, *, fallback: Path) -> Path:
        """解析可覆盖子目录：显式值优先，空值跟随 fallback。"""
        raw = cfg[field]
        if raw:
            return self.normalize_user_path(field, raw)
        return Path(fallback)

    @staticmethod
    def normalize_user_path(field: str, raw_value: str) -> Path:
        """规范化用户输入的单个路径。

        - 展开环境变量（``%APPDATA%`` / ``$HOME``）和用户目录（``~``）；
        - 相对路径直接拒绝并给出可操作提示（§1 绝对路径原则）；
        - ``resolve()`` 消除 ``..``、符号链接和大小写歧义（§8）。

        Raises:
            StoragePathError: 路径为空、为相对路径或无法解析。
        """
        value = (raw_value or "").strip()
        if not value:
            raise StoragePathError(field, "", "路径为空")
        expanded = os.path.expandvars(os.path.expanduser(value))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            raise StoragePathError(
                field,
                value,
                "必须使用绝对路径（例如 D:/AITranslatorData），不允许相对路径",
            )
        try:
            return candidate.resolve()
        except OSError as exc:
            raise StoragePathError(field, value, f"路径无法解析: {exc}") from exc

    # ── 校验 ────────────────────────────────────────

    def validate(self, resolved: ResolvedStoragePaths) -> List[StoragePathIssue]:
        """校验解析结果，返回 issue 列表（空列表表示完全通过）。

        error 级别问题必须阻断保存；warning 级别仅提示（如磁盘空间不足、
        数据根目录选在盘符根目录、目录相互嵌套）。
        """
        issues: List[StoragePathIssue] = []

        for field, path in resolved.managed_directories():
            issues.extend(self._validate_single(field, path))
        issues.extend(self._validate_distinct(resolved))
        issues.extend(self._validate_nesting(resolved))
        issues.extend(self._validate_data_root(resolved.data_root))
        issues.extend(self._validate_disk_space(resolved.data_root))
        return issues

    def _validate_single(self, field: str, path: Path) -> List[StoragePathIssue]:
        issues: List[StoragePathIssue] = []
        forbidden = self._find_forbidden_chars(path)
        if forbidden:
            issues.append(
                StoragePathIssue(
                    field=field,
                    path=str(path),
                    message=(
                        f"路径包含 Windows 不允许的字符 {forbidden}，"
                        '请移除 < > " | ? * 等字符后重试'
                    ),
                )
            )
        try:
            if path.exists() and not path.is_dir():
                issues.append(
                    StoragePathIssue(
                        field=field,
                        path=str(path),
                        message="该路径已存在且是一个普通文件，请选择其他目录",
                    )
                )
                return issues
        except OSError as exc:
            issues.append(
                StoragePathIssue(field=field, path=str(path), message=f"无法访问该路径: {exc}")
            )
            return issues

        # 可写性预检：目录已存在则检查自身，否则检查最近的已存在祖先。
        probe = path if path.is_dir() else self._nearest_existing_ancestor(path)
        if probe is not None and not os.access(probe, os.W_OK | os.R_OK):
            issues.append(
                StoragePathIssue(
                    field=field,
                    path=str(path),
                    message="目录不可写，请检查磁盘权限或更换目录",
                )
            )
        return issues

    @staticmethod
    def _find_forbidden_chars(path: Path) -> str:
        """返回路径分量中出现的 Windows 非法字符（仅 Windows 平台检查）。"""
        if sys.platform != "win32":
            return ""
        hits: set[str] = set()
        for part in path.parts[1:]:  # parts[0] 是盘符/UNC 锚点
            for ch in part:
                if ch in _WINDOWS_FORBIDDEN_CHARS or ch == ":":
                    hits.add(ch)
        return " ".join(sorted(hits))

    @staticmethod
    def _nearest_existing_ancestor(path: Path) -> Path | None:
        current = path
        for _ in range(64):  # 防御性上限，避免异常路径导致死循环
            parent = current.parent
            if parent == current:
                return None
            try:
                if parent.exists():
                    return parent
            except OSError:
                return None
            current = parent
        return None

    @staticmethod
    def _validate_distinct(resolved: ResolvedStoragePaths) -> List[StoragePathIssue]:
        """缓存/中间记录/备份目录两两不能相同（§5 第二步）。"""
        issues: List[StoragePathIssue] = []
        pairs = [
            ("cache_dir", resolved.cache_dir),
            ("translation_records_dir", resolved.translation_records_dir),
            ("translation_backups_dir", resolved.translation_backups_dir),
        ]
        for idx in range(len(pairs)):
            for other in pairs[idx + 1 :]:
                field_a, path_a = pairs[idx]
                field_b, path_b = other
                if _same_path(path_a, path_b):
                    issues.append(
                        StoragePathIssue(
                            field=field_b,
                            path=str(path_b),
                            message=f"与 {field_a} 指向同一目录，三类数据必须使用不同目录",
                        )
                    )
        return issues

    @staticmethod
    def _validate_nesting(resolved: ResolvedStoragePaths) -> List[StoragePathIssue]:
        """缓存/备份嵌套在中间记录目录内时给出 warning（清理缓存误伤进度风险）。"""
        issues: List[StoragePathIssue] = []
        records = resolved.translation_records_dir
        for field, path in (
            ("cache_dir", resolved.cache_dir),
            ("translation_backups_dir", resolved.translation_backups_dir),
        ):
            if _is_relative_to(path, records) or _is_relative_to(records, path):
                issues.append(
                    StoragePathIssue(
                        field=field,
                        path=str(path),
                        message="与翻译中间记录目录相互嵌套，清理或迁移时可能相互影响，建议分开",
                        severity="warning",
                    )
                )
        return issues

    @staticmethod
    def _validate_data_root(data_root: Path) -> List[StoragePathIssue]:
        """数据根目录位于盘符/文件系统根目录时给出 warning（§8 风险提示）。"""
        try:
            anchor = Path(data_root.anchor)
        except (ValueError, OSError):
            return []
        if data_root == anchor:
            return [
                StoragePathIssue(
                    field="data_root",
                    path=str(data_root),
                    message="数据根目录直接位于磁盘根目录，建议新建一个专用文件夹",
                    severity="warning",
                )
            ]
        return []

    @staticmethod
    def _validate_disk_space(data_root: Path) -> List[StoragePathIssue]:
        """可选的剩余空间检查（§5 第二步），失败静默跳过。"""
        try:
            usage = shutil.disk_usage(data_root.anchor or data_root)
        except OSError:
            return []
        if usage.free < LOW_DISK_SPACE_BYTES:
            return [
                StoragePathIssue(
                    field="data_root",
                    path=str(data_root),
                    message=(
                        f"目标磁盘剩余空间不足 {_format_bytes(LOW_DISK_SPACE_BYTES)}，"
                        "迁移可能失败，请清理后重试"
                    ),
                    severity="warning",
                )
            ]
        return []

    # ── 建目录 ──────────────────────────────────────

    def ensure_directories(self, resolved: ResolvedStoragePaths) -> None:
        """创建所有受管目录，任一失败抛出 :class:`StoragePathError`。

        与 ``AppPaths.create`` 相同的“失败即抛”语义（P2-7）：不得返回
        指向无效路径的对象后继续运行。
        """
        failed: List[Tuple[str, Path, str]] = []
        targets = [("data_root", resolved.data_root)] + resolved.managed_directories()
        for field, path in targets:
            try:
                if path.exists() and not path.is_dir():
                    failed.append((field, path, "路径已存在且是普通文件"))
                    continue
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                failed.append((field, path, str(exc)))
        if failed:
            first_field, first_path, first_reason = failed[0]
            details = "; ".join(f"{p}: {msg}" for _, p, msg in failed)
            logger.warning("数据目录创建失败: %s", details)
            raise StoragePathError(first_field, str(first_path), first_reason)


def _same_path(a: Path, b: Path) -> bool:
    """跨平台的路径相等判断（Windows 忽略大小写）。"""
    try:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))
    except (TypeError, ValueError):
        return a == b


def _is_relative_to(path: Path, other: Path) -> bool:
    """``path`` 是否等于或位于 ``other`` 之内（兼容 Python < 3.9 语义）。"""
    norm_a = os.path.normcase(str(path))
    norm_b = os.path.normcase(str(other))
    if norm_a == norm_b:
        return True
    prefix = norm_b.rstrip(os.sep) + os.sep
    return norm_a.startswith(prefix)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{size}B"
